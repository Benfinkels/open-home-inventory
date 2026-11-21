import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List

import yaml

from .receipts.parser_registry import ReceiptParserRegistry, parse_receipt_text
from .receipts.parsers.base import ParsedReceipt
from .receipts.scraper import ReceiptScraper
from .reconciliation.appraisal import AppraisalEngine
from .reconciliation.matcher import InventoryMatcher
from .storage.csv_exporter import export_inventory_csv
from .storage.database import InventoryDatabase, sha256_file
from .storage.markdown_exporter import export_inventory_markdown
from .vision.badge_reader import BadgeReader
from .vision.detector import DetectedItem, VisionDetector
from .vision.optical_crop import OpticalCropEngine, sanitize_slug

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("inventory-ai")

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def load_config(config_path: str = "./config/default_config.yaml") -> Dict[str, Any]:
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def run_demo():
    """Runs a complete end-to-end demonstration using synthetic mock data."""
    from examples.generate_sample_data import generate_demo

    generate_demo()


def _discover_images(target: str) -> List[tuple[str, str]]:
    """Return sorted list of (image_path, inferred_room) tuples."""
    root_path = Path(target)
    if root_path.is_file():
        return [(str(root_path), "General")]
    found: List[tuple[str, str]] = []
    for root, _, files in os.walk(root_path):
        rel = Path(root).relative_to(root_path)
        room = rel.parts[0].replace("_", " ").replace("-", " ").title() if rel.parts else "General"
        for fname in sorted(files):
            if fname.lower().endswith(IMAGE_EXTS):
                found.append((os.path.join(root, fname), room))
    found.sort(key=lambda x: x[0])
    return found


def scan_photos_pipeline(
    image_dir: str,
    detector: VisionDetector,
    db: InventoryDatabase,
    crops_dir: str = "./data/optical_crops",
    overlays_dir: str = "./data/overlays",
    run_badge_ocr: bool = True,
    save_overlays: bool = False,
    room_override: str | None = None,
    rescan: bool = False,
    dedupe: bool = False,
    limit: int | None = None,
    padding_px: int = 40,
) -> Dict[str, Any]:
    """Run the 2-stage scan (Stage-1 detection -> 1:1 optical crop -> Stage-2 BadgeReader OCR)."""
    crop_engine = OpticalCropEngine(output_dir=crops_dir, padding_px=padding_px)
    badge_reader = BadgeReader(detector=detector) if run_badge_ocr else None

    discovered = _discover_images(image_dir)
    if limit is not None and limit > 0:
        discovered = discovered[:limit]

    stats: Dict[str, Any] = {
        "discovered": len(discovered),
        "scanned": 0,
        "skipped_cached": 0,
        "failed": 0,
        "items_saved": 0,
        "badges_enriched": 0,
        "duplicates_merged": 0,
        "errors": [],
    }

    for img_path, inferred_room in discovered:
        room = room_override or inferred_room
        img_hash = sha256_file(img_path)

        if not rescan and db.is_image_scanned(img_hash):
            stats["skipped_cached"] += 1
            logger.info(f"Skipping already-scanned photo: {os.path.basename(img_path)}")
            continue

        logger.info(f"Scanning [{room}] {img_path}...")
        try:
            items = detector.analyze_image(img_path, room=room)
        except Exception as e:
            stats["failed"] += 1
            err_msg = f"{os.path.basename(img_path)}: {e}"
            stats["errors"].append(err_msg)
            db.record_scanned_image(img_hash, img_path, room=room, status="error", item_count=0, error_message=str(e))
            logger.error(f"Vision detection failed on {img_path}: {e}")
            continue

        enriched_items: List[DetectedItem] = []
        overlay_path = ""
        for idx, it in enumerate(items):
            crop_p = None
            if it.box:
                slug = f"{it.brand}_{it.item_name}_{idx}"
                crop_p = crop_engine.crop_item(img_path, it.box, slug)
                if crop_p:
                    it.source_image = crop_p
                    if badge_reader is not None:
                        try:
                            it, _ = badge_reader.enrich_item(it, crop_p)
                            if it.badge_verified:
                                stats["badges_enriched"] += 1
                        except Exception as ocr_err:
                            logger.warning(f"Stage-2 badge OCR failed on {crop_p}: {ocr_err}")
            enriched_items.append(it)

        if save_overlays and enriched_items:
            stem = sanitize_slug(os.path.splitext(os.path.basename(img_path))[0])
            ov_out = os.path.join(overlays_dir, f"overlay_{stem}.jpg")
            overlay_path = crop_engine.render_overlay(img_path, enriched_items, ov_out) or ""

        for it in enriched_items:
            db.save_item(
                {
                    "item_name": it.item_name,
                    "category": it.category,
                    "brand": it.brand,
                    "model_or_identifiers": it.model or "",
                    "room": it.room or room,
                    "condition": it.condition,
                    "estimated_replacement_value_usd": it.estimated_replacement_value_usd,
                    "receipt_verified": False,
                    "serial_number": it.serial_number or "",
                    "source_image": it.source_image,
                    "original_image": img_path,
                    "overlay_image": overlay_path,
                    "image_hash": img_hash,
                    "badge_verified": it.badge_verified,
                    "description": it.description,
                }
            )
            stats["items_saved"] += 1

        db.record_scanned_image(img_hash, img_path, room=room, status="ok", item_count=len(enriched_items))
        stats["scanned"] += 1

    if dedupe:
        stats["duplicates_merged"] = db.deduplicate_items()

    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Open Home Inventory: Multimodal Vision & Proof-of-Purchase Pipeline"
    )
    subparsers = parser.add_subparsers(dest="command", help="Pipeline commands")

    # Demo
    subparsers.add_parser("demo", help="Run full pipeline on synthetic mock data")

    # Scan Photos
    scan_p = subparsers.add_parser("scan-photos", help="Scan directory (or single file) of photos for valuable assets")
    scan_p.add_argument("image_dir", type=str, help="Directory or image file path")
    scan_p.add_argument("--backend", type=str, default=None, choices=["ollama", "openai", "openai-compat", "vllm", "gemini", "mock"])
    scan_p.add_argument("--endpoint", type=str, default=None, help="Vision endpoint URL (e.g. http://192.0.2.10:11434)")
    scan_p.add_argument("--model", type=str, default=None, help="Vision model ID (e.g. gemma4:e2b-it-q4_K_M)")
    scan_p.add_argument("--room", type=str, default=None, help="Override room name (default: inferred from subfolder)")
    scan_p.add_argument("--db-path", type=str, default=None, help="Override SQLite database path")
    scan_p.add_argument("--crops-dir", type=str, default=None, help="Override optical crops directory")
    scan_p.add_argument("--no-ocr", action="store_true", help="Skip Stage-2 BadgeReader OCR on optical crops")
    scan_p.add_argument("--save-overlays", action="store_true", help="Save annotated bounding-box overlay images")
    scan_p.add_argument("--rescan", action="store_true", help="Re-scan images even if their SHA-256 hash is already cached")
    scan_p.add_argument("--dedupe", action="store_true", help="Merge cross-photo duplicate detections after scanning")
    scan_p.add_argument("--limit", type=int, default=None, help="Process at most N photos")

    # Scrape Receipts
    scrape_p = subparsers.add_parser("scrape-receipts", help="Scrape order receipts from email or file")
    scrape_p.add_argument("--query", type=str, default="receipt OR order OR invoice", help="Search query")
    scrape_p.add_argument("--max", type=int, default=25, help="Maximum emails to retrieve")
    scrape_p.add_argument("--file", type=str, default=None, help="Path to text file containing receipts")
    scrape_p.add_argument("--db-path", type=str, default=None, help="Override SQLite database path")

    # Reconcile
    recon_p = subparsers.add_parser("reconcile", help="Reconcile visual detections against parsed receipts")
    recon_p.add_argument("--threshold", type=float, default=0.65, help="Fuzzy matching threshold (0.0 to 1.0)")
    recon_p.add_argument("--db-path", type=str, default=None, help="Override SQLite database path")
    recon_p.add_argument("--dedupe", action="store_true", help="Deduplicate items before reconciliation")

    # Serve
    serve_p = subparsers.add_parser("serve", help="Launch local interactive web dashboard")
    serve_p.add_argument("--port", type=int, default=8000, help="Web server port")
    serve_p.add_argument("--host", type=str, default="127.0.0.1", help="Web server host")

    args = parser.parse_args()

    if args.command == "demo" or not args.command:
        run_demo()

    elif args.command == "serve":
        from web.server import start_server

        start_server(host=args.host, port=args.port)

    elif args.command == "scan-photos":
        cfg = load_config()
        v_cfg = cfg.get("vision", {})
        s_cfg = cfg.get("storage", {})
        crop_cfg = v_cfg.get("optical_crop", {})

        db = InventoryDatabase(args.db_path or s_cfg.get("db_path", "./data/inventory.db"))
        detector = VisionDetector(
            backend=args.backend or v_cfg.get("backend", "ollama"),
            endpoint=args.endpoint or v_cfg.get("endpoint", "http://localhost:11434"),
            model=args.model or v_cfg.get("model", "gemma4:e2b-it-q4_K_M"),
            temperature=float(v_cfg.get("temperature", 0.1)),
            timeout=int(v_cfg.get("timeout_seconds", 90)),
            keep_alive=str(v_cfg.get("keep_alive", "15m")),
        )

        res = scan_photos_pipeline(
            image_dir=args.image_dir,
            detector=detector,
            db=db,
            crops_dir=args.crops_dir or s_cfg.get("crops_dir", "./data/optical_crops"),
            overlays_dir=s_cfg.get("overlays_dir", "./data/overlays"),
            run_badge_ocr=not args.no_ocr,
            save_overlays=args.save_overlays,
            room_override=args.room,
            rescan=args.rescan,
            dedupe=args.dedupe,
            limit=args.limit,
            padding_px=int(crop_cfg.get("padding_px", 40)),
        )
        print(
            f"[✓] Scan complete: {res['scanned']} scanned, {res['skipped_cached']} cached/skipped, "
            f"{res['failed']} failed | {res['items_saved']} items saved ({res['badges_enriched']} badge-enriched, "
            f"{res['duplicates_merged']} duplicates merged)."
        )

    elif args.command == "scrape-receipts":
        cfg = load_config()
        r_cfg = cfg.get("receipts", {})
        s_cfg = cfg.get("storage", {})
        db = InventoryDatabase(args.db_path or s_cfg.get("db_path", "./data/inventory.db"))

        if args.file and os.path.exists(args.file):
            print(f"[*] Ingesting receipt file: {args.file}")
            with open(args.file, "r", encoding="utf-8") as f:
                content = f.read()
            blocks = content.split("=== END_OF_RECEIPT ===")
            receipts = []
            for b in blocks:
                cleaned = b.strip()
                if len(cleaned) > 20:
                    parsed = parse_receipt_text(cleaned)
                    if parsed:
                        db.save_receipt(parsed)
                        receipts.append(parsed)
                        print(f"      -> Ingested {parsed.merchant} #{parsed.order_number} (${parsed.total_usd:,.2f})")
            print(f"[✓] Ingested and stored {len(receipts)} receipts into database.")
        else:
            provider = r_cfg.get("provider", "mock")
            scraper = ReceiptScraper(provider=provider)
            messages = scraper.fetch_receipts(query=args.query, max_results=args.max)
            registry = ReceiptParserRegistry()
            saved_count = 0
            for m in messages:
                rec = registry.parse_email(m.message_id, m.date, m.sender, m.subject, m.body)
                if rec:
                    db.save_receipt(rec)
                    saved_count += 1
                    print(f"      -> Ingested {rec.merchant} #{rec.order_number} (${rec.total_usd:,.2f})")
            print(f"[✓] Retrieved and stored {saved_count} receipts into database.")

    elif args.command == "reconcile":
        cfg = load_config()
        s_cfg = cfg.get("storage", {})
        db_path = args.db_path or s_cfg.get("db_path", "./data/inventory.db")
        db = InventoryDatabase(db_path)

        if args.dedupe:
            merged = db.deduplicate_items()
            if merged:
                print(f"[*] Deduplicated {merged} duplicate cross-photo detections.")

        db_items = db.get_all_items()
        db_receipts_raw = db.get_all_receipts()

        if not db_items:
            print("[!] No cataloged assets in database. Run 'scan-photos' or 'demo' first.")
            return

        items = [
            DetectedItem(
                item_name=raw["item_name"],
                category=raw.get("category") or "General",
                brand=raw.get("brand"),
                model=raw.get("model_or_identifiers") or None,
                room=raw.get("room") or "General",
                condition=raw.get("condition") or "Good",
                serial_number=raw.get("serial_number") or None,
                estimated_replacement_value_usd=raw.get("estimated_replacement_value_usd") or 0.0,
                description=raw.get("description") or "",
                source_image=raw.get("source_image") or "",
                original_image=raw.get("original_image") or "",
                badge_verified=bool(raw.get("badge_verified")),
            )
            for raw in db_items
        ]

        receipts = []
        for r_raw in db_receipts_raw:
            try:
                r_json = json.loads(r_raw.get("raw_json", "{}"))
                receipts.append(ParsedReceipt(**r_json))
            except Exception:
                continue

        print(f"[*] Reconciling {len(items)} cataloged assets against {len(receipts)} digital receipts...")
        matcher = InventoryMatcher(fuzzy_threshold=args.threshold)
        matches = matcher.match_batch(items, receipts)

        appraiser = AppraisalEngine()
        reconciled_records, stats = appraiser.evaluate_portfolio(matches)
        db.bulk_save_items(reconciled_records)

        export_dir = Path("./data/exports")
        export_dir.mkdir(parents=True, exist_ok=True)
        csv_file = export_dir / "inventory_insurance_schedule.csv"
        report_file = export_dir / "inventory_appraisal_report.md"

        export_inventory_csv(reconciled_records, str(csv_file))
        export_inventory_markdown(reconciled_records, stats, str(report_file))

        print("\n" + "=" * 65)
        print("  CROSS-MODAL RECONCILIATION COMPLETE")
        print(f"  Total Cataloged Assets:       {stats.total_items}")
        print(f"  Total Replacement Value:      ${stats.total_replacement_value_usd:,.2f} USD")
        print(f"  Receipt-Verified Assets:      {stats.verified_items_count} ({stats.verification_percentage:.1f}%)")
        print(f"  Verified Valuation Backed:    ${stats.verified_valuation_usd:,.2f} USD")
        print(f"  High-Value Missing Evidence:  {len(stats.missing_evidence_items)} items (>= $500)")
        print(f"  Database Updated:             {db_path}")
        print(f"  CSV Schedule Exported:        {csv_file}")
        print(f"  Appraisal Report Exported:    {report_file}")
        print("=" * 65)


if __name__ == "__main__":
    main()
