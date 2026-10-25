import argparse
import json
import logging
import os
import sys
import yaml
from typing import Dict, Any

from .vision.detector import VisionDetector
from .vision.optical_crop import OpticalCropEngine
from .receipts.scraper import ReceiptScraper
from .receipts.parser_registry import ReceiptParserRegistry
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
        with open(config_path, "r") as f:
            return yaml.safe_load(f)
    return {}


def run_demo():
    """Runs a complete end-to-end demonstration using synthetic, privacy-safe mock data."""
    print("\n" + "=" * 70)
    print("  OPEN HOME INVENTORY - END-TO-END DEMONSTRATION PIPELINE")
    print("=" * 70 + "\n")

    cfg = load_config()
    db_path = cfg.get("storage", {}).get("db_path", "./data/inventory.db")
    db = InventoryDatabase(db_path)

    # 1. Vision Detection (Mock/Local)
    print("[1/4] Running Multimodal Vision Detection...")
    detector = VisionDetector(backend="mock")
    items = detector.analyze_image("./examples/desk_photo.jpg")
    items.extend(detector.analyze_image("./examples/bike_photo.jpg"))
    print(f"      -> Detected {len(items)} physical assets across room photos.")

    # 2. Receipt Ingestion
    print("\n[2/4] Scraper: Ingesting digital purchase receipts...")
    scraper = ReceiptScraper(provider="mock")
    messages = scraper.fetch_receipts()
    registry = ReceiptParserRegistry()

    receipts = []
    for msg in messages:
        parsed = registry.parse_email(msg.message_id, msg.date, msg.sender, msg.subject, msg.body)
        if parsed:
            receipts.append(parsed)
            print(f"      -> Parsed {parsed.merchant} order #{parsed.order_number} ({len(parsed.items)} items)")

    # 3. Cross-Modal Reconciliation & Appraisal
    print("\n[3/4] Reconciling visual assets with purchase proof & serial numbers...")
    matcher = InventoryMatcher(fuzzy_threshold=0.60)
    matches = matcher.match_batch(items, receipts)

    appraiser = AppraisalEngine()
    reconciled_records, stats = appraiser.evaluate_portfolio(matches)

    # 4. Storage & Export
    print("\n[4/4] Generating Insurance Audit Deliverables...")
    db.bulk_save_items(reconciled_records)

    json_path = cfg.get("storage", {}).get("json_output", "./data/master_inventory.json")
    csv_path = cfg.get("storage", {}).get("csv_output", "./data/master_inventory.csv")
    md_path = cfg.get("storage", {}).get("summary_output", "./reports/INVENTORY_SUMMARY.md")

    db.export_to_json(json_path)
    export_inventory_csv(reconciled_records, csv_path)
    export_inventory_markdown(reconciled_records, stats, md_path)

    print("\n" + "-" * 70)
    print(f"  PORTFOLIO AUDIT COMPLETE")
    print(f"  - Total Assets Cataloged : {stats.total_items}")
    print(f"  - Total Replacement Value: ${stats.total_replacement_value_usd:,.2f} USD")
    print(f"  - Receipt-Verified Value : ${stats.verified_valuation_usd:,.2f} USD ({stats.verification_percentage}%)")
    print(f"  - Deliverables Produced  : JSON, CSV, Markdown Summary")
    print("-" * 70 + "\n")


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
    scrape_p = subparsers.add_parser("scrape-receipts", help="Scrape order receipts from email")
    scrape_p.add_argument("--query", type=str, default="receipt OR order OR invoice", help="Search query")
    scrape_p.add_argument("--max", type=int, default=25, help="Maximum emails to retrieve")

    # Reconcile
    subparsers.add_parser("reconcile", help="Reconcile visual detections against parsed receipts")

    # Serve
    serve_p = subparsers.add_parser("serve", help="Launch local interactive web dashboard")
    serve_p.add_argument("--port", type=int, default=8080, help="Web server port")
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
        detector = VisionDetector(
            backend=v_cfg.get("backend", "ollama"),
            endpoint=v_cfg.get("endpoint", "http://localhost:11434"),
            model=v_cfg.get("model", "minicpm-v:latest")
        )
        all_items = []
        for root, _, files in os.walk(args.image_dir):
            for file in files:
                if file.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                    p = os.path.join(root, file)
                    logger.info(f"Scanning {p}...")
                    all_items.extend(detector.analyze_image(p))
        print(f"Extracted {len(all_items)} candidate items.")
    elif args.command == "scrape-receipts":
        cfg = load_config()
        r_cfg = cfg.get("receipts", {})
        scraper = ReceiptScraper(provider=r_cfg.get("provider", "gog"))
        messages = scraper.fetch_receipts(query=args.query, max_results=args.max)
        registry = ReceiptParserRegistry()
        for m in messages:
            rec = registry.parse_email(m.message_id, m.date, m.sender, m.subject, m.body)
            if rec:
                print(f"Found receipt from {rec.merchant}: #{rec.order_number} (${rec.total_usd:.2f})")
    elif args.command == "reconcile":
        run_demo()


if __name__ == "__main__":
    main()
