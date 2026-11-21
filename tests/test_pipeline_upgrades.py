"""Tests for hardened 2-stage vision pipeline, EXIF handling, hash resume, dedup, and 1-to-1 matching."""

import json
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from src.cli import scan_photos_pipeline
from src.receipts.parsers.base import ParsedItem, ParsedReceipt
from src.reconciliation.appraisal import AppraisalEngine
from src.reconciliation.matcher import InventoryMatcher
from src.storage.database import InventoryDatabase
from src.vision.badge_reader import BadgeReader, redact_serial
from src.vision.detector import BoundingBox, DetectedItem, VisionDetector
from src.vision.optical_crop import OpticalCropEngine


def test_no_silent_mock_fallback_on_network_error(tmp_path):
    img_p = tmp_path / "bike.jpg"
    Image.new("RGB", (400, 300), color=(40, 80, 120)).save(img_p)

    det = VisionDetector(backend="ollama", endpoint="http://127.0.0.1:1", timeout=1)
    with patch("src.vision.detector.requests.post", side_effect=ConnectionError("refused")):
        with pytest.raises(ConnectionError):
            det.analyze_image(str(img_p))

    reader = BadgeReader(detector=det)
    with patch("src.vision.detector.requests.post", side_effect=ConnectionError("refused")):
        with pytest.raises(ConnectionError):
            reader.read_badge(str(img_p))


def test_ollama_payload_enforces_keep_alive_think_false_and_json_format(tmp_path):
    img_p = tmp_path / "room.jpg"
    Image.new("RGB", (640, 480), color=(20, 30, 40)).save(img_p)

    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "response": json.dumps(
            {
                "items": [
                    {
                        "item_name": "OLED Monitor",
                        "category": "Electronics",
                        "brand": "Dell",
                        "model": "AW3423DW",
                        "condition": "Excellent",
                        "estimated_replacement_value_usd": 899.99,
                        "description": "Curved ultrawide",
                        "box_2d": [100, 100, 700, 800],
                    }
                ]
            }
        )
    }

    det = VisionDetector(backend="ollama", endpoint="http://127.0.0.1:11434", model="gemma4:e2b-it-q4_K_M")
    with patch("src.vision.detector.requests.post", return_value=mock_resp) as post_mock:
        items = det.analyze_image(str(img_p), room="Office")

    assert len(items) == 1
    assert items[0].room == "Office"
    sent_payload = post_mock.call_args.kwargs["json"]
    assert sent_payload["keep_alive"] == "15m"
    assert sent_payload["think"] is False
    assert sent_payload["format"] == "json"
    assert sent_payload["options"]["num_predict"] == 1024
    assert sent_payload["options"]["repeat_penalty"] >= 1.05


def test_iou_nms_drops_bookshelf_repetition_loop_boxes(tmp_path):
    img_p = tmp_path / "shelf.jpg"
    Image.new("RGB", (800, 600), color=(30, 30, 30)).save(img_p)

    # Simulate a small SLM repeating the same bounding box 8 times on a bookshelf
    repeated = [
        {
            "item_name": f"Book Copy {i}",
            "category": "Books",
            "brand": "Unknown",
            "box_2d": [200, 300, 500, 400],
        }
        for i in range(8)
    ] + [
        {
            "item_name": "Desk Lamp",
            "category": "Furniture",
            "brand": "Unknown",
            "box_2d": [100, 700, 450, 900],
        }
    ]
    det = VisionDetector(backend="mock")
    parsed = det._parse_json_response(json.dumps({"items": repeated}), str(img_p), room="Office")
    assert len(parsed) == 2
    assert parsed[0].item_name == "Book Copy 0"
    assert parsed[1].item_name == "Desk Lamp"


def test_optical_crop_exif_transpose_and_strips_exif(tmp_path):
    # Create a 600x300 raw image with EXIF Orientation=6 (90 deg CW -> upright 300x600)
    # Paint the top half of the UPRIGHT image bright red (which is the LEFT half in raw 600x300? Wait:
    # Orientation 6 rotates 90 deg CW (270 CCW): raw (x=0..299) becomes upright TOP (y=0..299)).
    raw = Image.new("RGB", (600, 300), color=(0, 0, 200))
    for x in range(300):
        for y in range(300):
            raw.putpixel((x, y), (220, 20, 20))

    exif = raw.getexif()
    exif[274] = 6  # Orientation: Rotate 90 CW
    exif[271] = "FakeCameraMake"
    img_p = tmp_path / "portrait_exif.jpg"
    raw.save(img_p, format="JPEG", exif=exif)

    engine = OpticalCropEngine(output_dir=str(tmp_path / "crops"), padding_px=0)
    # Crop TOP half of upright image (ymin=0, ymax=450) -> should be RED, not blue!
    crop_p = engine.crop_item(str(img_p), BoundingBox(ymin=0, xmin=100, ymax=450, xmax=900), "top_half")
    assert crop_p is not None
    with Image.open(crop_p) as cropped:
        assert len(cropped.getexif()) == 0, "EXIF metadata was not stripped from crop!"
        r, g, b = cropped.getpixel((cropped.width // 2, cropped.height // 2))
        assert r > 180 and b < 60, f"Expected red upright top half after exif_transpose, got {(r, g, b)}"


def test_scan_pipeline_wires_badge_reader_resume_room_overlay_and_dedupe(tmp_path):
    photos = tmp_path / "photos"
    (photos / "garage").mkdir(parents=True)
    (photos / "office").mkdir(parents=True)

    p1 = photos / "garage" / "bike_angle1.jpg"
    p2 = photos / "garage" / "bike_angle2.jpg"
    p3 = photos / "office" / "desk_setup.jpg"
    Image.new("RGB", (800, 600), color=(30, 60, 30)).save(p1)
    # Distinct pixel content so p2 has a different sha256 hash than p1 (simulating 2nd angle)
    Image.new("RGB", (800, 600), color=(35, 65, 35)).save(p2)
    Image.new("RGB", (800, 600), color=(30, 30, 60)).save(p3)

    db = InventoryDatabase(str(tmp_path / "inv.db"))
    det = VisionDetector(backend="mock")

    # First pass: scans 3 photos, wires BadgeReader OCR, saves overlays, merges duplicate bike angles
    s1 = scan_photos_pipeline(
        image_dir=str(photos),
        detector=det,
        db=db,
        crops_dir=str(tmp_path / "crops"),
        overlays_dir=str(tmp_path / "overlays"),
        run_badge_ocr=True,
        save_overlays=True,
        dedupe=True,
    )
    assert s1["scanned"] == 3
    assert s1["skipped_cached"] == 0
    assert s1["badges_enriched"] >= 3
    assert s1["duplicates_merged"] == 1  # Two angles of the same Specialized bike merged into 1!

    items = db.get_all_items()
    # 1 deduplicated bike + 2 desk items = 3 total
    assert len(items) == 3
    bike = next(it for it in items if "bike" in it["item_name"].lower())
    assert bike["room"] == "Garage"
    assert bike["serial_number"] == "WSBC-MOCK-001928"
    assert bike["badge_verified"] == 1
    assert bike["overlay_image"]

    # Second pass without --rescan: all 3 skipped via SHA-256 hash cache
    s2 = scan_photos_pipeline(
        image_dir=str(photos),
        detector=det,
        db=db,
        crops_dir=str(tmp_path / "crops"),
        overlays_dir=str(tmp_path / "overlays"),
        rescan=False,
    )
    assert s2["scanned"] == 0
    assert s2["skipped_cached"] == 3


def test_one_to_one_receipt_matching_prevents_double_verification():
    item_a = DetectedItem(item_name="Dell UltraSharp U2723QE Monitor", category="Electronics", brand="Dell", model="U2723QE", estimated_replacement_value_usd=600.0)
    item_b = DetectedItem(item_name="Dell UltraSharp U2723QE Monitor", category="Electronics", brand="Dell", model="U2723QE", estimated_replacement_value_usd=600.0)

    # Single receipt with quantity=1 -> must verify ONLY ONE of the two monitors
    rec_qty1 = ParsedReceipt(
        merchant="Amazon",
        order_number="AMZ-1",
        purchase_date="2024-01-10",
        total_usd=579.99,
        items=[ParsedItem(name="Dell UltraSharp U2723QE Monitor", price_usd=579.99, quantity=1)],
    )
    matcher = InventoryMatcher(fuzzy_threshold=0.65)
    m1 = matcher.match_batch([item_a, item_b], [rec_qty1])
    assert sum(1 for m in m1 if m.receipt is not None) == 1

    # Receipt with quantity=2 -> verifies BOTH monitors
    rec_qty2 = ParsedReceipt(
        merchant="Amazon",
        order_number="AMZ-2",
        purchase_date="2024-01-10",
        total_usd=1159.98,
        items=[ParsedItem(name="Dell UltraSharp U2723QE Monitor", price_usd=579.99, quantity=2)],
    )
    m2 = matcher.match_batch([item_a, item_b], [rec_qty2])
    assert sum(1 for m in m2 if m.receipt is not None) == 2


def test_appraisal_missing_evidence_and_serial_redaction():
    item = DetectedItem(item_name="Gravel Bike", category="Bicycles", brand="Bianchi", model="Impulso", estimated_replacement_value_usd=2400.0)
    matcher = InventoryMatcher()
    matches = matcher.match_batch([item], [])
    recs, stats = AppraisalEngine(high_value_threshold_usd=500.0).evaluate_portfolio(matches)
    assert recs[0]["serial_number"] is None  # Must NOT copy model name into serial_number
    assert len(stats.missing_evidence_items) == 1
    assert set(stats.missing_evidence_items[0]["missing"]) == {"receipt", "serial_number"}

    assert redact_serial("WSBC014373007V") == "WSB-REDACTED-07V"
    assert redact_serial("12345") == "REDACTED"


def test_benchmark_artifact_integrity_and_privacy_hygiene():
    from pathlib import Path
    import re

    eval_json = Path(__file__).resolve().parent.parent / "results" / "two_stage_vision_eval.json"
    assert eval_json.exists()
    raw = eval_json.read_text(encoding="utf-8")
    assert "@" not in raw
    assert not re.search(r"\b(?:192\.168|10\.\d{1,3})\.\d{1,3}\.\d{1,3}\b", raw)

    data = json.loads(raw)
    assert data["photos_tested"] == 45
    assert data["s1_success_photos"] == 45
    assert data["exif_stripped_crops"] == data["crops_created"] > 0
    assert data["stage2_optical_crop_ocr"]["items_enriched_by_stage2"] >= 100
