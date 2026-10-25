import csv
import logging
import os
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

CSV_HEADERS = [
    "item_name",
    "category",
    "brand",
    "model_or_identifiers",
    "condition",
    "estimated_replacement_value_usd",
    "receipt_verified",
    "purchase_price_usd",
    "purchase_date",
    "merchant",
    "order_number",
    "serial_number",
    "receipt_evidence",
    "source_image",
    "description"
]


def export_inventory_csv(items: List[Dict[str, Any]], csv_path: str):
    """Exports structured inventory items into an insurance-compliant CSV file."""
    os.makedirs(os.path.dirname(os.path.abspath(csv_path)), exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
        writer.writeheader()
        for it in items:
            row = dict(it)
            row["receipt_verified"] = "TRUE" if row.get("receipt_verified") else "FALSE"
            writer.writerow(row)
    logger.info(f"Exported {len(items)} items to CSV: {csv_path}")
