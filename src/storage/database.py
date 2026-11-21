import hashlib
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

GENERIC_BRANDS = frozenset({"", "unknown", "generic", "generic brand", "n/a", "none"})


def sha256_file(path: str, chunk_size: int = 65536) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def _norm_key(val: Optional[str]) -> str:
    if not val:
        return ""
    return re.sub(r"[^a-z0-9]+", "", str(val).lower())


class InventoryDatabase:
    """
    SQLite persistence layer for cataloged assets, scanned-image hash cache,
    optical crop references, and linked receipt proof-of-purchase records.
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
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS items (
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
            cursor.execute(
                """
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
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS scanned_images (
                    image_hash TEXT PRIMARY KEY,
                    file_path TEXT,
                    room TEXT,
                    status TEXT,
                    item_count INTEGER DEFAULT 0,
                    error_message TEXT,
                    scanned_at TEXT
                )
                """
            )
            # Safe schema migration for older SQLite databases
            existing_cols = {r[1] for r in cursor.execute("PRAGMA table_info(items)").fetchall()}
            migrations = {
                "room": "TEXT DEFAULT 'General'",
                "original_image": "TEXT",
                "overlay_image": "TEXT",
                "image_hash": "TEXT",
                "badge_verified": "INTEGER DEFAULT 0",
            }
            for col, col_type in migrations.items():
                if col not in existing_cols:
                    cursor.execute(f"ALTER TABLE items ADD COLUMN {col} {col_type}")
            conn.commit()

    def is_image_scanned(self, image_hash: str) -> bool:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT status FROM scanned_images WHERE image_hash = ? AND status = 'ok'",
                (image_hash,),
            ).fetchone()
            return row is not None

    def record_scanned_image(
        self,
        image_hash: str,
        file_path: str,
        room: str = "General",
        status: str = "ok",
        item_count: int = 0,
        error_message: str = "",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO scanned_images (
                    image_hash, file_path, room, status, item_count, error_message, scanned_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (image_hash, file_path, room, status, item_count, error_message, now),
            )
            conn.commit()

    def get_scanned_images(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM scanned_images ORDER BY scanned_at DESC").fetchall()
            return [dict(r) for r in rows]

    def save_item(self, item: Dict[str, Any]) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO items (
                    item_name, category, brand, model_or_identifiers, room, condition,
                    estimated_replacement_value_usd, receipt_verified, purchase_price_usd,
                    purchase_date, merchant, order_number, serial_number, receipt_evidence,
                    source_image, original_image, overlay_image, image_hash, badge_verified,
                    description, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.get("item_name"),
                    item.get("category"),
                    item.get("brand"),
                    item.get("model_or_identifiers"),
                    item.get("room") or "General",
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
                    item.get("original_image"),
                    item.get("overlay_image"),
                    item.get("image_hash"),
                    1 if item.get("badge_verified") else 0,
                    item.get("description"),
                    now,
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def bulk_save_items(self, items: List[Dict[str, Any]]) -> int:
        count = 0
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM items")
            now = datetime.now(timezone.utc).isoformat()
            for item in items:
                cursor.execute(
                    """
                    INSERT INTO items (
                        item_name, category, brand, model_or_identifiers, room, condition,
                        estimated_replacement_value_usd, receipt_verified, purchase_price_usd,
                        purchase_date, merchant, order_number, serial_number, receipt_evidence,
                        source_image, original_image, overlay_image, image_hash, badge_verified,
                        description, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.get("item_name"),
                        item.get("category"),
                        item.get("brand"),
                        item.get("model_or_identifiers"),
                        item.get("room") or "General",
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
                        item.get("original_image"),
                        item.get("overlay_image"),
                        item.get("image_hash"),
                        1 if item.get("badge_verified") else 0,
                        item.get("description"),
                        now,
                    ),
                )
                count += 1
            conn.commit()
        return count

    def deduplicate_items(self) -> int:
        """
        Deterministically merge cross-photo duplicate detections:
        1. Same non-empty normalized serial_number (across any room).
        2. Same (room, normalized brand, normalized model) when brand is specific and model is present.
        Returns the number of duplicate rows removed.
        """
        items = self.get_all_items()
        if len(items) <= 1:
            return 0

        merged: List[Dict[str, Any]] = []
        serial_index: Dict[str, int] = {}
        model_index: Dict[tuple[str, str, str], int] = {}

        def _merge_into(dst: Dict[str, Any], src: Dict[str, Any]) -> None:
            dst["estimated_replacement_value_usd"] = max(
                float(dst.get("estimated_replacement_value_usd") or 0.0),
                float(src.get("estimated_replacement_value_usd") or 0.0),
            )
            for fld in ("serial_number", "model_or_identifiers", "purchase_date", "merchant", "order_number", "receipt_evidence", "source_image", "overlay_image"):
                if not dst.get(fld) and src.get(fld):
                    dst[fld] = src[fld]
            if src.get("receipt_verified"):
                dst["receipt_verified"] = 1
                if src.get("purchase_price_usd") and not dst.get("purchase_price_usd"):
                    dst["purchase_price_usd"] = src["purchase_price_usd"]
            if src.get("badge_verified"):
                dst["badge_verified"] = 1
            src_desc = (src.get("description") or "").strip()
            dst_desc = (dst.get("description") or "").strip()
            if src_desc and src_desc not in dst_desc:
                dst["description"] = f"{dst_desc} | {src_desc}".strip(" |")

        for it in items:
            sn_k = _norm_key(it.get("serial_number"))
            brand_raw = (it.get("brand") or "").strip().lower()
            brand_k = _norm_key(brand_raw) if brand_raw not in GENERIC_BRANDS else ""
            model_k = _norm_key(it.get("model_or_identifiers"))
            room_k = _norm_key(it.get("room") or "General")

            target_idx: Optional[int] = None
            if sn_k and len(sn_k) >= 4 and sn_k in serial_index:
                target_idx = serial_index[sn_k]
            elif brand_k and model_k and len(model_k) >= 3 and (room_k, brand_k, model_k) in model_index:
                target_idx = model_index[(room_k, brand_k, model_k)]

            if target_idx is not None:
                _merge_into(merged[target_idx], it)
                if sn_k and len(sn_k) >= 4:
                    serial_index[sn_k] = target_idx
                if brand_k and model_k and len(model_k) >= 3:
                    model_index[(room_k, brand_k, model_k)] = target_idx
            else:
                new_idx = len(merged)
                merged.append(dict(it))
                if sn_k and len(sn_k) >= 4:
                    serial_index[sn_k] = new_idx
                if brand_k and model_k and len(model_k) >= 3:
                    model_index[(room_k, brand_k, model_k)] = new_idx

        removed = len(items) - len(merged)
        if removed > 0:
            self.bulk_save_items(merged)
        return removed

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

            cursor.execute(
                """
                INSERT OR REPLACE INTO receipts (
                    merchant, order_number, purchase_date, total_usd,
                    source_message_id, notes, raw_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r_dict.get("merchant"),
                    r_dict.get("order_number"),
                    r_dict.get("purchase_date"),
                    r_dict.get("total_usd"),
                    r_dict.get("source_message_id"),
                    r_dict.get("notes"),
                    json.dumps(r_dict),
                    now,
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def get_all_receipts(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM receipts ORDER BY purchase_date DESC")
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
