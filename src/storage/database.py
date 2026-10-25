import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)


class InventoryDatabase:
    """
    SQLite persistence layer for cataloged assets, optical crop references,
    and linked receipt proof-of-purchase records.
    """

    def __init__(self, db_path: str = "./data/inventory.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._init_tables()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_tables(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_name TEXT NOT NULL,
                    category TEXT,
                    brand TEXT,
                    model_or_identifiers TEXT,
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
                    description TEXT,
                    updated_at TEXT
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    merchant TEXT,
                    order_number TEXT UNIQUE,
                    purchase_date TEXT,
                    total_usd REAL,
                    source_message_id TEXT,
                    notes TEXT,
                    raw_json TEXT,
                    created_at TEXT
                )
            """)
            conn.commit()

    def save_item(self, item: Dict[str, Any]) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO items (
                    item_name, category, brand, model_or_identifiers, condition,
                    estimated_replacement_value_usd, receipt_verified, purchase_price_usd,
                    purchase_date, merchant, order_number, serial_number, receipt_evidence,
                    source_image, description, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                item.get("item_name"),
                item.get("category"),
                item.get("brand"),
                item.get("model_or_identifiers"),
                item.get("condition"),
                item.get("estimated_replacement_value_usd"),
                1 if item.get("receipt_verified") else 0,
                item.get("purchase_price_usd"),
                item.get("purchase_date"),
                item.get("merchant"),
                item.get("order_number"),
                item.get("serial_number"),
                item.get("receipt_evidence"),
                item.get("source_image"),
                item.get("description"),
                now
            ))
            conn.commit()
            return cursor.lastrowid

    def bulk_save_items(self, items: List[Dict[str, Any]]) -> int:
        count = 0
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Clear existing for full re-sync
            cursor.execute("DELETE FROM items")
            now = datetime.now(timezone.utc).isoformat()
            for item in items:
                cursor.execute("""
                    INSERT INTO items (
                        item_name, category, brand, model_or_identifiers, condition,
                        estimated_replacement_value_usd, receipt_verified, purchase_price_usd,
                        purchase_date, merchant, order_number, serial_number, receipt_evidence,
                        source_image, description, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    item.get("item_name"),
                    item.get("category"),
                    item.get("brand"),
                    item.get("model_or_identifiers"),
                    item.get("condition"),
                    item.get("estimated_replacement_value_usd"),
                    1 if item.get("receipt_verified") else 0,
                    item.get("purchase_price_usd"),
                    item.get("purchase_date"),
                    item.get("merchant"),
                    item.get("order_number"),
                    item.get("serial_number"),
                    item.get("receipt_evidence"),
                    item.get("source_image"),
                    item.get("description"),
                    now
                ))
                count += 1
            conn.commit()
        return count

    def get_all_items(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM items ORDER BY estimated_replacement_value_usd DESC")
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def export_to_json(self, json_path: str):
        items = self.get_all_items()
        os.makedirs(os.path.dirname(os.path.abspath(json_path)), exist_ok=True)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2)
        logger.info(f"Exported {len(items)} items to {json_path}")

    def save_receipt(self, receipt: Any) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if hasattr(receipt, "model_dump"):
                r_dict = receipt.model_dump()
            elif isinstance(receipt, dict):
                r_dict = receipt
            else:
                r_dict = {}

            cursor.execute("""
                INSERT OR REPLACE INTO receipts (
                    merchant, order_number, purchase_date, total_usd,
                    source_message_id, notes, raw_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                r_dict.get("merchant"),
                r_dict.get("order_number"),
                r_dict.get("purchase_date"),
                r_dict.get("total_usd"),
                r_dict.get("source_message_id"),
                r_dict.get("notes"),
                json.dumps(r_dict),
                now
            ))
            conn.commit()
            return cursor.lastrowid

    def get_all_receipts(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM receipts ORDER BY purchase_date DESC")
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

