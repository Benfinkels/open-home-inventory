"""
Unit tests for Storage and Exporter Subsystems: SQLite database CRUD,
insurance CSV format generation, and executive markdown appraisal reports.
"""

import os
import pytest
from src.storage.database import InventoryDatabase
from src.storage.csv_exporter import export_inventory_csv
from src.storage.markdown_exporter import export_inventory_markdown
from src.reconciliation.appraisal import PortfolioAuditStats


def test_sqlite_inventory_crud(tmp_path):
    db_file = tmp_path / "test_inventory.db"
    db = InventoryDatabase(str(db_file))

    item = {
        "item_name": "Studio Display 27-inch 5K",
        "category": "Electronics & Computers",
        "brand": "Apple",
        "model_or_identifiers": "MK0U3LL/A",
        "condition": "Excellent",
        "estimated_replacement_value_usd": 1599.00,
        "receipt_verified": True,
        "purchase_price_usd": 1599.00,
        "purchase_date": "2023-05-12",
        "merchant": "Apple Store",
        "order_number": "W918274610",
        "serial_number": "C02XYZ123ABC",
        "receipt_evidence": "Apple Store Order #W918274610",
        "source_image": "apple_display.jpg",
        "description": "5K Retina studio monitor"
    }

    item_id = db.save_item(item)
    assert item_id > 0

    items = db.get_all_items()
    assert len(items) == 1
    assert items[0]["item_name"] == "Studio Display 27-inch 5K"
    assert items[0]["receipt_verified"] == 1
    assert items[0]["serial_number"] == "C02XYZ123ABC"


def test_exporters(tmp_path):
    items = [
        {
            "item_name": "Aeron Chair Size B",
            "category": "Office & Furniture",
            "brand": "Herman Miller",
            "model_or_identifiers": "Aeron-B-Graphite",
            "condition": "Excellent",
            "estimated_replacement_value_usd": 1250.00,
            "receipt_verified": True,
            "purchase_price_usd": 1195.00,
            "purchase_date": "2023-03-10",
            "merchant": "Design Within Reach",
            "order_number": "DWR-88412",
            "serial_number": "HM-AERON-88412-B",
            "receipt_evidence": "DWR Order #DWR-88412",
            "source_image": "office_chair.jpg",
            "description": "Ergonomic mesh workstation chair"
        }
    ]

    csv_path = tmp_path / "test_schedule.csv"
    export_inventory_csv(items, str(csv_path))
    assert os.path.exists(csv_path)

    with open(csv_path, "r", encoding="utf-8") as f:
        csv_content = f.read()
    assert "Aeron Chair Size B" in csv_content
    assert "HM-AERON-88412-B" in csv_content
    assert "TRUE" in csv_content

    md_path = tmp_path / "test_report.md"
    stats = PortfolioAuditStats(
        total_items=1,
        total_replacement_value_usd=1250.00,
        verified_items_count=1,
        verified_valuation_usd=1250.00,
        verification_percentage=100.0,
        categories={"Office & Furniture": {"count": 1, "value": 1250.00, "verified_count": 1, "verified_val": 1250.00}}
    )
    export_inventory_markdown(items, stats, str(md_path))
    assert os.path.exists(md_path)

    with open(md_path, "r", encoding="utf-8") as f:
        md_content = f.read()
    assert "Executive Portfolio Summary" in md_content
    assert "Aeron Chair Size B" in md_content
    assert "$1,250.00 USD" in md_content
