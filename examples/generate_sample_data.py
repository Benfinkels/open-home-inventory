"""
Generate Synthetic Sample Data for Open Home Inventory.
Loads synthetic inventory assets and parses sample receipts to populate
a ready-to-test local database without requiring any camera or private email access.
"""

import os
import sys
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.vision.detector import DetectedItem
from src.receipts.parser_registry import parse_receipt_text
from src.reconciliation.matcher import InventoryMatcher
from src.reconciliation.appraisal import AppraisalEngine
from src.storage.database import InventoryDatabase
from src.storage.csv_exporter import export_inventory_csv
from src.storage.markdown_exporter import export_inventory_markdown

SAMPLE_INVENTORY = BASE_DIR / "sample_inventory.json"
SAMPLE_RECEIPTS = BASE_DIR / "sample_receipt.txt"
DB_PATH = PROJECT_ROOT / "data" / "inventory.db"
EXPORT_DIR = PROJECT_ROOT / "data" / "exports"


def generate_demo():
    print("=" * 65)
    print(" Open Home Inventory: Generating Synthetic Demo Data")
    print("=" * 65)

    # 1. Load synthetic inventory items
    if not SAMPLE_INVENTORY.exists():
        raise FileNotFoundError(f"Missing {SAMPLE_INVENTORY}")

    with open(SAMPLE_INVENTORY, "r", encoding="utf-8") as f:
        raw_items = json.load(f)

    items = [DetectedItem(**raw) for raw in raw_items]
    print(f"[*] Loaded {len(items)} synthetic visual items.")

    # 2. Parse synthetic receipts
    receipts = []
    if SAMPLE_RECEIPTS.exists():
        with open(SAMPLE_RECEIPTS, "r", encoding="utf-8") as f:
            content = f.read()

        blocks = content.split("=== END_OF_RECEIPT ===")
        for block in blocks:
            cleaned = block.strip()
            if not cleaned or len(cleaned) < 20:
                continue
            parsed = parse_receipt_text(cleaned)
            if parsed and parsed.items:
                receipts.append(parsed)
                print(f"[+] Parsed receipt: {parsed.merchant} ({len(parsed.items)} items, total: ${parsed.total_usd:,.2f})")

    # 3. Cross-modal reconciliation
    matcher = InventoryMatcher(fuzzy_threshold=0.65)
    matches = matcher.match_batch(items, receipts)

    appraisal = AppraisalEngine()
    reconciled_records, stats = appraisal.evaluate_portfolio(matches)

    verified_count = sum(1 for it in reconciled_records if it.get("receipt_verified"))
    print(f"[*] Reconciled {verified_count}/{len(reconciled_records)} items with verified purchase records.")

    # 4. Save to SQLite database
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = InventoryDatabase(str(DB_PATH))
    db.bulk_save_items(reconciled_records)

    for rec in receipts:
        db.save_receipt(rec)

    print(f"[✓] Saved records into database: {DB_PATH}")

    # 5. Export insurance deliverables
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    csv_file = EXPORT_DIR / "inventory_insurance_schedule.csv"
    report_file = EXPORT_DIR / "inventory_appraisal_report.md"

    export_inventory_csv(reconciled_records, str(csv_file))
    export_inventory_markdown(reconciled_records, stats, str(report_file))

    print(f"[✓] Exported CSV Schedule: {csv_file}")
    print(f"[✓] Exported Markdown Audit Report: {report_file}")

    # 6. Print portfolio summary
    print("-" * 65)
    print(f" Total Cataloged Items:       {stats.total_items}")
    print(f" Total Replacement Valuation: ${stats.total_replacement_value_usd:,.2f} USD")
    print(f" Verified Proof of Purchase:  {stats.verified_items_count} items ({stats.verification_percentage:.1f}%)")
    print(f" Verified Valuation Backed:   ${stats.verified_valuation_usd:,.2f} USD")
    print("=" * 65)
    print("\nDemo initialization complete! Run 'python3 -m src.cli serve' to launch the dashboard.")


if __name__ == "__main__":
    generate_demo()
