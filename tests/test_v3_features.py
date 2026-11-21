"""Tests for Open Home Inventory v3: editable ledger, merge, receipts, $0 escalation, Claim Pack ZIP."""

from email.message import EmailMessage
import io
import json
from pathlib import Path
import sqlite3
from unittest.mock import MagicMock, patch
import zipfile

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from src.receipts.ingestor import LocalReceiptIngestor
from src.receipts.parser_registry import normalize_merchant, strip_html
from src.receipts.parsers.base import ParsedItem, ParsedReceipt
from src.reconciliation.appraisal import estimate_acv
from src.storage.database import InventoryDatabase
from src.vision.escalator import (
    escalate_high_value_items,
    export_candidates_bundle,
    format_escalation_digest,
    import_proposals_bundle,
    send_openclaw_notification,
    send_webhook_notification,
)
import web.server as web_server


def _make_jpg(path: Path, color=(40, 120, 80)) -> Path:
    img = Image.new("RGB", (120, 90), color=color)
    img.save(path, format="JPEG", quality=85)
    return path


def test_legacy_migration_and_user_edit_protection(tmp_path):
    db_file = tmp_path / "legacy.db"
    # Create minimal v2 schema without v3 columns/tables
    conn = sqlite3.connect(db_file)
    conn.execute(
        """
        CREATE TABLE items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_name TEXT NOT NULL,
            category TEXT,
            brand TEXT,
            model_or_identifiers TEXT,
            room TEXT DEFAULT 'General',
            condition TEXT,
            estimated_replacement_value_usd REAL,
            receipt_verified INTEGER DEFAULT 0,
            purchase_price_usd REAL,
            purchase_date TEXT,
            merchant TEXT,
            order_number TEXT,
            serial_number TEXT,
            receipt_evidence TEXT,
            source_image TEXT,
            original_image TEXT,
            overlay_image TEXT,
            image_hash TEXT,
            badge_verified INTEGER DEFAULT 0,
            description TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO items (item_name, brand, estimated_replacement_value_usd, source_image) VALUES (?, ?, ?, ?)",
        ("Mountain Bike", "Unknown", 1200.0, "/tmp/bike_crop.jpg"),
    )
    conn.commit()
    conn.close()

    db = InventoryDatabase(str(db_file))
    item = db.get_item(1)
    assert item is not None
    assert item["review_status"] == "unreviewed"
    assert len(item["photos"]) == 1
    assert item["photos"][0]["photo_role"] == "optical_crop"

    # User locks brand manually
    db.update_item(1, {"brand": "Santa Cruz"}, record_user_edit=True)
    prop_id = db.create_proposal(
        item_id=1,
        provider="ollama",
        model="qwen3.5:9b",
        proposed_brand="Specialized",
        proposed_model="Hightower C",
        proposed_value_usd=4200.0,
    )
    applied = db.apply_proposal(prop_id)
    assert applied["brand"] == "Santa Cruz"  # user edit protected!
    assert applied["model_or_identifiers"] == "Hightower C"
    assert applied["estimated_replacement_value_usd"] == 4200.0


def test_merge_items_preserves_all_photos_and_receipt(tmp_path):
    db = InventoryDatabase(str(tmp_path / "inv.db"))
    id1 = db.save_item(
        {
            "item_name": "OLED Monitor (Front)",
            "brand": "Unknown",
            "estimated_replacement_value_usd": 750.0,
            "source_image": str(tmp_path / "m1.jpg"),
        }
    )
    id2 = db.save_item(
        {
            "item_name": "OLED Monitor (Rear Label)",
            "brand": "Alienware",
            "serial_number": "SN-OLED-991",
            "estimated_replacement_value_usd": 900.0,
            "source_image": str(tmp_path / "m2.jpg"),
            "badge_verified": True,
        }
    )
    merged = db.merge_items(id1, [id2])
    assert merged is not None
    assert merged["brand"] == "Alienware"
    assert merged["serial_number"] == "SN-OLED-991"
    assert merged["estimated_replacement_value_usd"] == 900.0
    assert len(merged["photos"]) == 2
    assert db.get_item(id2) is None


def test_acv_estimation_floor_and_condition():
    # Very old laptop hits 25% salvage floor
    assert estimate_acv(2000.0, "Electronics | Laptops", purchase_date="2015-01-01") == 500.0
    # Undated item uses condition multiplier
    assert estimate_acv(1000.0, "Furniture", condition="Excellent") == 850.0
    assert estimate_acv(0.0, "Electronics") == 0.0


def test_receipt_normalization_html_strip_and_eml_ingest(tmp_path):
    assert normalize_merchant("Amazon.com Services LLC") == "Amazon"
    assert normalize_merchant("Micro Center <NoReply@microcenter.com>") == "Micro Center"
    assert "Order Total" in strip_html("<html><body><style>p{color:red}</style><p>Order Total: $499.00</p></body></html>")

    eml_path = tmp_path / "order.eml"
    msg = EmailMessage()
    msg["From"] = "Amazon.com <auto-confirm@amazon.com>"
    msg["To"] = "user@example.com"
    msg["Subject"] = "Your Amazon.com order of Sony Headphones"
    msg["Date"] = "Tue, 10 Sep 2024 12:00:00 -0400"
    msg.set_content(
        "<html><body><h1>Order #112-5555555-1234567</h1>"
        "<p>1 of: Sony WH-1000XM5 Wireless Headphones</p>"
        "<p>Grand Total: $348.00</p></body></html>",
        subtype="html",
    )
    eml_path.write_bytes(msg.as_bytes())

    ingestor = LocalReceiptIngestor()
    receipts = ingestor.ingest_path(str(tmp_path))
    assert len(receipts) == 1
    assert receipts[0].merchant == "Amazon"
    assert receipts[0].total_usd == pytest.approx(348.0)


def test_escalation_dry_run_mock_agent_handoff_and_minimax_guard(tmp_path):
    db = InventoryDatabase(str(tmp_path / "esc.db"))
    crop = _make_jpg(tmp_path / "bike.jpg")
    item_id = db.save_item(
        {
            "item_name": "Mountain Bike",
            "category": "Sports & Recreation | Bicycles",
            "brand": "Unknown",
            "estimated_replacement_value_usd": 1400.0,
            "source_image": str(crop),
        }
    )

    dry = escalate_high_value_items(db, backend="ollama", model="qwen3.5:9b", dry_run=True)
    assert dry["candidates"] == 1
    assert dry["estimated_total_cost_usd"] == 0.0

    # Guardrail: MiniMax-M2.7 is text-only and must raise clear error before sending image payloads
    mm_res = escalate_high_value_items(db, backend="openai-compat", model="MiniMax-M2.7", endpoint="https://api.minimax.io")
    assert mm_res["errors"]
    assert "text-only" in mm_res["errors"][0]

    # Mock escalation creates pending proposal
    run = escalate_high_value_items(db, backend="mock", model="mock-vlm")
    assert len(run["proposals_created"]) == 1

    # $0 Antigravity agent handoff round-trip
    bundle = export_candidates_bundle(db, min_value=500.0)
    assert bundle["count"] == 1
    assert bundle["candidates"][0]["item_id"] == item_id

    imported_ids = import_proposals_bundle(
        db,
        [
            {
                "item_id": item_id,
                "brand": "Specialized",
                "model_name": "Stumpjumper Comp Alloy S3",
                "estimated_replacement_value_usd": 3400.0,
                "reasoning": "Verified frame geometry via Antigravity vision",
            }
        ],
    )
    assert len(imported_ids) == 1
    pending = db.list_proposals(status="pending")
    assert len(pending) == 1
    assert pending[0]["proposed_model"] == "Stumpjumper Comp Alloy S3"

    digest = format_escalation_digest(pending, missing_evidence_count=1)
    assert "Stumpjumper Comp Alloy S3" in digest

    with patch("src.vision.escalator.requests.post") as mock_post:
        mock_post.return_value = MagicMock(raise_for_status=lambda: None)
        assert send_webhook_notification("https://discord.com/api/webhooks/123/abc", digest)
        assert "content" in mock_post.call_args.kwargs["json"]

    with patch("src.vision.escalator.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        assert send_openclaw_notification(digest, cmd="openclaw agent --message-file -")
        assert mock_run.call_args.kwargs["input"] == digest


def test_fastapi_v3_endpoints_and_claim_pack_zip(tmp_path, monkeypatch):
    db_file = tmp_path / "web.db"
    crops_dir = tmp_path / "crops"
    crops_dir.mkdir()
    monkeypatch.setattr(web_server, "DB_PATH", db_file)
    monkeypatch.setattr(web_server, "CROPS_DIR", crops_dir)
    monkeypatch.setattr(web_server, "PROJECT_ROOT", tmp_path)

    db = InventoryDatabase(str(db_file))
    crop1 = _make_jpg(crops_dir / "bike_1.jpg")
    crop2 = _make_jpg(crops_dir / "bike_2.jpg", color=(80, 40, 120))
    id1 = db.save_item(
        {
            "item_name": "Road Bike",
            "category": "Bicycles",
            "brand": "Unknown",
            "room": "Garage",
            "estimated_replacement_value_usd": 1800.0,
            "source_image": str(crop1),
        }
    )
    id2 = db.save_item(
        {
            "item_name": "Road Bike (Rear)",
            "category": "Bicycles",
            "brand": "Specialized",
            "room": "Garage",
            "estimated_replacement_value_usd": 1900.0,
            "source_image": str(crop2),
        }
    )
    rec_id = db.save_receipt(
        ParsedReceipt(
            merchant="Sample Bike Shop",
            order_number="ORD-999",
            purchase_date="2025-05-01",
            total_usd=2100.0,
            items=[ParsedItem(name="Specialized Road Bike", price_usd=2100.0, serial_number="WSBC-SYN-777")],
            source_message_id="msg-1",
        )
    )

    client = TestClient(web_server.app)

    # Dashboard renders cleanly
    r_home = client.get("/")
    assert r_home.status_code == 200
    assert "Open Home Inventory" in r_home.text

    # Merge multi-angle
    r_merge = client.post("/api/items/merge", json={"primary_id": id1, "merge_ids": [id2]})
    assert r_merge.status_code == 200
    assert len(r_merge.json()["photos"]) == 2

    # Edit item
    r_patch = client.patch(f"/api/items/{id1}", json={"sub_location": "Wall Rack", "review_status": "confirmed"})
    assert r_patch.status_code == 200
    assert r_patch.json()["sub_location"] == "Wall Rack"

    # Upload close-up badge photo (run_ocr=false for fast offline test)
    img_buf = io.BytesIO()
    Image.new("RGB", (200, 150), color=(20, 20, 20)).save(img_buf, format="JPEG")
    img_buf.seek(0)
    r_up = client.post(
        f"/api/items/{id1}/badge-photo?run_ocr=false",
        files={"file": ("serial.jpg", img_buf.getvalue(), "image/jpeg")},
    )
    assert r_up.status_code == 200
    assert len(r_up.json()["item"]["photos"]) == 3

    # Top-3 receipt candidates + manual 1-to-1 link
    r_cands = client.get(f"/api/items/{id1}/receipt-candidates")
    assert r_cands.status_code == 200
    assert r_cands.json()[0]["receipt_id"] == rec_id

    r_link = client.post(
        f"/api/items/{id1}/receipt-link",
        json={"receipt_id": rec_id, "price_usd": 2100.0, "serial_number": "WSBC-SYN-777"},
    )
    assert r_link.status_code == 200
    assert r_link.json()["receipt_verified"] == 1
    assert r_link.json()["receipt_match_type"] == "manual"

    # Proposal accept via REST API
    prop_id = db.create_proposal(id1, "agent", "antigravity", proposed_model="Allez Sprint")
    r_acc = client.post(f"/api/proposals/{prop_id}/accept")
    assert r_acc.status_code == 200
    assert r_acc.json()["model_or_identifiers"] == "Allez Sprint"

    # Claim Pack .zip export
    r_zip = client.get("/api/export/claim-pack")
    assert r_zip.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r_zip.content)) as zf:
        names = set(zf.namelist())
        assert "inventory_insurance_schedule.csv" in names
        assert "inventory_appraisal_report.md" in names
        assert "receipts.json" in names
        assert any(n.startswith("photos/") for n in names)
