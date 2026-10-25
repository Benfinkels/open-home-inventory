import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Dict, Any

import yaml

from .vision.detector import VisionDetector, DetectedItem
from .vision.optical_crop import OpticalCropEngine
from .receipts.scraper import ReceiptScraper
from .receipts.parser_registry import ReceiptParserRegistry, parse_receipt_text
from .receipts.parsers.base import ParsedReceipt
from .reconciliation.matcher import InventoryMatcher
from .reconciliation.appraisal import AppraisalEngine
from .storage.database import InventoryDatabase
from .storage.csv_exporter import export_inventory_csv
from .storage.markdown_exporter import export_inventory_markdown
from .storage.drive_sync import GoogleDriveSync

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("inventory-ai")


def load_config(config_path: str = "./config/default_config.yaml") -> Dict[str, Any]:
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    return {}


def run_demo():
    """Runs a complete end-to-end demonstration using synthetic mock data."""
    from examples.generate_sample_data import generate_demo
    generate_demo()


def main():
    parser = argparse.ArgumentParser(
        description="Open Home Inventory: Multimodal Vision & Proof-of-Purchase Pipeline"
    )
    subparsers = parser.add_subparsers(dest="command", help="Pipeline commands")

    # Demo
    subparsers.add_parser("demo", help="Run full pipeline on synthetic mock data")

    # Scan Photos
    scan_p = subparsers.add_parser("scan-photos", help="Scan directory of photos for valuable assets")
    scan_p.add_argument("image_dir", type=str, help="Directory containing room photos")

    # Scrape Receipts
    scrape_p = subparsers.add_parser("scrape-receipts", help="Scrape order receipts from email or file")
    scrape_p.add_argument("--query", type=str, default="receipt OR order OR invoice", help="Search query")
    scrape_p.add_argument("--max", type=int, default=25, help="Maximum emails to retrieve")
    scrape_p.add_argument("--file", type=str, default=None, help="Path to text file containing receipts")

    # Reconcile
    recon_p = subparsers.add_parser("reconcile", help="Reconcile visual detections against parsed receipts")
    recon_p.add_argument("--threshold", type=float, default=0.65, help="Fuzzy matching threshold (0.0 to 1.0)")

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
        db = InventoryDatabase(s_cfg.get("db_path", "./data/inventory.db"))
        crops_dir = s_cfg.get("crops_dir", "./data/optical_crops")
        crop_engine = OpticalCropEngine(output_dir=crops_dir)

        detector = VisionDetector(
            backend=v_cfg.get("backend", "ollama"),
            endpoint=v_cfg.get("endpoint", "http://localhost:11434"),
            model=v_cfg.get("model", "minicpm-v:latest"),
        )

        all_items = []
        for root, _, files in os.walk(args.image_dir):
            for file in files:
                if file.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                    p = os.path.join(root, file)
                    logger.info(f"Scanning image {p}...")
                    try:
                        items = detector.analyze_image(p)
                        for it in items:
                            if it.box:
                                slug = f"{it.brand}_{it.item_name}".replace(" ", "_")
                                crop_p = crop_engine.crop_item(p, it.box, slug)
                                if crop_p:
                                    it.source_image = crop_p

                            rec_dict = {
                                "item_name": it.item_name,
                                "category": it.category,
                                "brand": it.brand,
                                "model_or_identifiers": it.model or "",
                                "condition": it.condition,
                                "estimated_replacement_value_usd": it.estimated_replacement_value_usd,
                                "receipt_verified": False,
                                "serial_number": it.serial_number or "",
                                "source_image": it.source_image,
                                "description": it.description,
                            }
                            db.save_item(rec_dict)
                        all_items.extend(items)
                    except Exception as e:
                        logger.error(f"Error scanning {p}: {e}")

        print(f"[✓] Extracted and saved {len(all_items)} cataloged assets into database.")

    elif args.command == "scrape-receipts":
        cfg = load_config()
        r_cfg = cfg.get("receipts", {})
        s_cfg = cfg.get("storage", {})
        db = InventoryDatabase(s_cfg.get("db_path", "./data/inventory.db"))

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
        db_path = s_cfg.get("db_path", "./data/inventory.db")
        db = InventoryDatabase(db_path)

        db_items = db.get_all_items()
        db_receipts_raw = db.get_all_receipts()

        if not db_items:
            print("[!] No cataloged assets in database. Run 'scan-photos' or 'demo' first.")
            return

        items = []
        for raw in db_items:
            items.append(DetectedItem(
                item_name=raw["item_name"],
                category=raw.get("category") or "General",
                brand=raw.get("brand"),
                model=raw.get("model_or_identifiers"),
                serial_number=raw.get("serial_number"),
                estimated_replacement_value_usd=raw.get("estimated_replacement_value_usd") or 0.0,
                description=raw.get("description") or "",
                source_image=raw.get("source_image") or "",
            ))

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
        print(f"  CROSS-MODAL RECONCILIATION COMPLETE")
        print(f"  Total Cataloged Assets:       {stats.total_items}")
        print(f"  Total Replacement Value:      ${stats.total_replacement_value_usd:,.2f} USD")
        print(f"  Receipt-Verified Assets:      {stats.verified_items_count} ({stats.verification_percentage:.1f}%)")
        print(f"  Verified Valuation Backed:    ${stats.verified_valuation_usd:,.2f} USD")
        print(f"  Database Updated:             {db_path}")
        print(f"  CSV Schedule Exported:        {csv_file}")
        print(f"  Appraisal Report Exported:    {report_file}")
        print("=" * 65)


if __name__ == "__main__":
    main()
