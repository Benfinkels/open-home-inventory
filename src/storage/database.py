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

EDITABLE_ITEM_FIELDS = frozenset({
    "item_name",
    "category",
    "brand",
    "model_or_identifiers",
    "room",
    "sub_location",
    "condition",
    "quantity",
    "estimated_replacement_value_usd",
    "estimated_acv_usd",
    "serial_number",
    "purchase_price_usd",
    "purchase_date",
    "merchant",
    "order_number",
    "receipt_evidence",
    "receipt_verified",
    "badge_verified",
    "review_status",
    "description",
})

VALID_REVIEW_STATUSES = frozenset({"unreviewed", "confirmed", "needs_review", "archived"})


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


def _parse_edited_fields(raw: Any) -> List[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return [str(x) for x in parsed] if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


class InventoryDatabase:
    """
    SQLite persistence layer for cataloged assets, multi-angle item photos,
    escalation diff proposals, scanned-image hash cache, and receipt records.
    """

    def __init__(self, db_path: str = "./data/inventory.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._init_tables()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
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
                    sub_location TEXT DEFAULT '',
                    condition TEXT,
                    quantity INTEGER DEFAULT 1,
                    estimated_replacement_value_usd REAL,
                    estimated_acv_usd REAL,
                    receipt_verified INTEGER DEFAULT 0,
                    receipt_match_type TEXT DEFAULT 'auto',
                    receipt_id INTEGER,
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
                    review_status TEXT DEFAULT 'unreviewed',
                    user_edited_fields TEXT DEFAULT '[]',
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
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS item_photos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    photo_role TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    sha256 TEXT DEFAULT '',
                    created_at TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS escalation_proposals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    proposed_brand TEXT,
                    proposed_model TEXT,
                    proposed_serial TEXT,
                    proposed_value_usd REAL,
                    reasoning TEXT DEFAULT '',
                    est_cost_usd REAL DEFAULT 0.0,
                    status TEXT DEFAULT 'pending',
                    created_at TEXT,
                    resolved_at TEXT
                )
                """
            )
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_item_photos_item ON item_photos(item_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_proposals_item_status ON escalation_proposals(item_id, status)")

            # Safe schema migration for older SQLite databases
            existing_cols = {r[1] for r in cursor.execute("PRAGMA table_info(items)").fetchall()}
            migrations = {
                "room": "TEXT DEFAULT 'General'",
                "sub_location": "TEXT DEFAULT ''",
                "quantity": "INTEGER DEFAULT 1",
                "estimated_acv_usd": "REAL",
                "receipt_match_type": "TEXT DEFAULT 'auto'",
                "receipt_id": "INTEGER",
                "original_image": "TEXT",
                "overlay_image": "TEXT",
                "image_hash": "TEXT",
                "badge_verified": "INTEGER DEFAULT 0",
                "review_status": "TEXT DEFAULT 'unreviewed'",
                "user_edited_fields": "TEXT DEFAULT '[]'",
            }
            for col, col_type in migrations.items():
                if col not in existing_cols:
                    cursor.execute(f"ALTER TABLE items ADD COLUMN {col} {col_type}")

            # One-time idempotent backfill of item_photos for legacy items
            now = datetime.now(timezone.utc).isoformat()
            orphan_items = cursor.execute(
                """
                SELECT i.id, i.source_image, i.overlay_image
                FROM items i
                LEFT JOIN item_photos p ON p.item_id = i.id
                WHERE p.id IS NULL AND (
                    (i.source_image IS NOT NULL AND i.source_image != '')
                    OR (i.overlay_image IS NOT NULL AND i.overlay_image != '')
                )
                """
            ).fetchall()
            for row in orphan_items:
                if row["source_image"]:
                    cursor.execute(
                        "INSERT INTO item_photos (item_id, photo_role, file_path, sha256, created_at) VALUES (?, 'optical_crop', ?, '', ?)",
                        (row["id"], row["source_image"], now),
                    )
                if row["overlay_image"]:
                    cursor.execute(
                        "INSERT INTO item_photos (item_id, photo_role, file_path, sha256, created_at) VALUES (?, 'room_overlay', ?, '', ?)",
                        (row["id"], row["overlay_image"], now),
                    )
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

    @staticmethod
    def _insert_item_cursor(cursor: sqlite3.Cursor, item: Dict[str, Any], now: str) -> int:
        edited = item.get("user_edited_fields")
        edited_json = json.dumps(edited) if isinstance(edited, list) else (edited or "[]")
        cursor.execute(
            """
            INSERT INTO items (
                item_name, category, brand, model_or_identifiers, room, sub_location, condition,
                quantity, estimated_replacement_value_usd, estimated_acv_usd,
                receipt_verified, receipt_match_type, receipt_id,
                purchase_price_usd, purchase_date, merchant, order_number, serial_number,
                receipt_evidence, source_image, original_image, overlay_image, image_hash,
                badge_verified, review_status, user_edited_fields, description, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.get("item_name"),
                item.get("category"),
                item.get("brand"),
                item.get("model_or_identifiers"),
                item.get("room") or "General",
                item.get("sub_location") or "",
                item.get("condition"),
                int(item.get("quantity") or 1),
                item.get("estimated_replacement_value_usd"),
                item.get("estimated_acv_usd"),
                1 if item.get("receipt_verified") else 0,
                item.get("receipt_match_type") or "auto",
                item.get("receipt_id"),
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
                item.get("review_status") or "unreviewed",
                edited_json,
                item.get("description"),
                now,
            ),
        )
        item_id = int(cursor.lastrowid)
        if item.get("source_image"):
            cursor.execute(
                "INSERT INTO item_photos (item_id, photo_role, file_path, sha256, created_at) VALUES (?, 'optical_crop', ?, '', ?)",
                (item_id, item["source_image"], now),
            )
        if item.get("overlay_image"):
            cursor.execute(
                "INSERT INTO item_photos (item_id, photo_role, file_path, sha256, created_at) VALUES (?, 'room_overlay', ?, '', ?)",
                (item_id, item["overlay_image"], now),
            )
        return item_id

    def save_item(self, item: Dict[str, Any]) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            item_id = self._insert_item_cursor(conn.cursor(), item, now)
            conn.commit()
            return item_id

    def bulk_save_items(self, items: List[Dict[str, Any]]) -> int:
        count = 0
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM items")
            now = datetime.now(timezone.utc).isoformat()
            for item in items:
                self._insert_item_cursor(cursor, item, now)
                count += 1
            conn.commit()
        return count

    def get_item(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            if not row:
                return None
            item = dict(row)
            item["user_edited_fields"] = _parse_edited_fields(item.get("user_edited_fields"))
            item["photos"] = [
                dict(p)
                for p in conn.execute(
                    "SELECT * FROM item_photos WHERE item_id = ? ORDER BY id ASC", (item_id,)
                ).fetchall()
            ]
            item["pending_proposals"] = [
                dict(pr)
                for pr in conn.execute(
                    "SELECT * FROM escalation_proposals WHERE item_id = ? AND status = 'pending' ORDER BY id DESC",
                    (item_id,),
                ).fetchall()
            ]
            return item

    def update_item(
        self,
        item_id: int,
        updates: Dict[str, Any],
        record_user_edit: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Partially update allowed item fields and optionally lock them in user_edited_fields."""
        clean: Dict[str, Any] = {}
        for k, v in updates.items():
            if k not in EDITABLE_ITEM_FIELDS:
                continue
            if k == "review_status" and v not in VALID_REVIEW_STATUSES:
                raise ValueError(f"Invalid review_status: {v}")
            if k in ("receipt_verified", "badge_verified"):
                clean[k] = 1 if v else 0
            elif k == "quantity":
                clean[k] = max(1, int(v))
            elif k in ("estimated_replacement_value_usd", "estimated_acv_usd", "purchase_price_usd"):
                clean[k] = float(v) if v not in (None, "") else None
            else:
                clean[k] = v

        with self._get_connection() as conn:
            existing = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            if not existing:
                return None
            if not clean:
                return self.get_item(item_id)

            edited = set(_parse_edited_fields(existing["user_edited_fields"]))
            if record_user_edit:
                edited.update(k for k in clean if k != "review_status")
            clean["user_edited_fields"] = json.dumps(sorted(edited))
            clean["updated_at"] = datetime.now(timezone.utc).isoformat()

            set_clause = ", ".join(f"{k} = ?" for k in clean)
            values = list(clean.values()) + [item_id]
            conn.execute(f"UPDATE items SET {set_clause} WHERE id = ?", values)
            conn.commit()
        return self.get_item(item_id)

    def delete_item(self, item_id: int, archive: bool = False) -> bool:
        with self._get_connection() as conn:
            if archive:
                now = datetime.now(timezone.utc).isoformat()
                cur = conn.execute(
                    "UPDATE items SET review_status = 'archived', updated_at = ? WHERE id = ?",
                    (now, item_id),
                )
            else:
                cur = conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
            conn.commit()
            return cur.rowcount > 0

    def bulk_delete_items(self, item_ids: List[int], archive: bool = False) -> int:
        if not item_ids:
            return 0
        placeholders = ",".join("?" for _ in item_ids)
        with self._get_connection() as conn:
            if archive:
                now = datetime.now(timezone.utc).isoformat()
                cur = conn.execute(
                    f"UPDATE items SET review_status = 'archived', updated_at = ? WHERE id IN ({placeholders})",
                    [now, *item_ids],
                )
            else:
                cur = conn.execute(f"DELETE FROM items WHERE id IN ({placeholders})", list(item_ids))
            conn.commit()
            return cur.rowcount

    def merge_items(self, primary_id: int, merge_ids: List[int]) -> Optional[Dict[str, Any]]:
        """Atomically merge multi-angle duplicate detections into primary_id, preserving all photos."""
        clean_merge = [int(i) for i in merge_ids if int(i) != int(primary_id)]
        if not clean_merge:
            return self.get_item(primary_id)

        with self._get_connection() as conn:
            cur = conn.cursor()
            primary_row = cur.execute("SELECT * FROM items WHERE id = ?", (primary_id,)).fetchone()
            if not primary_row:
                return None
            primary = dict(primary_row)

            placeholders = ",".join("?" for _ in clean_merge)
            others = [
                dict(r)
                for r in cur.execute(
                    f"SELECT * FROM items WHERE id IN ({placeholders})", clean_merge
                ).fetchall()
            ]
            if not others:
                return self.get_item(primary_id)

            for src in others:
                primary["estimated_replacement_value_usd"] = max(
                    float(primary.get("estimated_replacement_value_usd") or 0.0),
                    float(src.get("estimated_replacement_value_usd") or 0.0),
                )
                if (primary.get("brand") or "").strip().lower() in GENERIC_BRANDS and (
                    src.get("brand") or ""
                ).strip().lower() not in GENERIC_BRANDS:
                    primary["brand"] = src["brand"]
                for fld in (
                    "serial_number",
                    "model_or_identifiers",
                    "purchase_date",
                    "merchant",
                    "order_number",
                    "receipt_evidence",
                    "source_image",
                    "overlay_image",
                    "sub_location",
                ):
                    if not primary.get(fld) and src.get(fld):
                        primary[fld] = src[fld]
                if src.get("receipt_verified") and not primary.get("receipt_verified"):
                    primary["receipt_verified"] = 1
                    primary["receipt_id"] = src.get("receipt_id") or primary.get("receipt_id")
                    primary["receipt_match_type"] = src.get("receipt_match_type") or primary.get("receipt_match_type")
                    if src.get("purchase_price_usd") and not primary.get("purchase_price_usd"):
                        primary["purchase_price_usd"] = src["purchase_price_usd"]
                if src.get("badge_verified"):
                    primary["badge_verified"] = 1
                src_desc = (src.get("description") or "").strip()
                dst_desc = (primary.get("description") or "").strip()
                if src_desc and src_desc not in dst_desc:
                    primary["description"] = f"{dst_desc} | {src_desc}".strip(" |")

            # Re-parent all photos & pending proposals before deleting merged rows
            cur.execute(
                f"UPDATE item_photos SET item_id = ? WHERE item_id IN ({placeholders})",
                [primary_id, *clean_merge],
            )
            cur.execute(
                f"UPDATE escalation_proposals SET item_id = ? WHERE item_id IN ({placeholders})",
                [primary_id, *clean_merge],
            )
            now = datetime.now(timezone.utc).isoformat()
            cur.execute(
                """
                UPDATE items SET
                    brand = ?, model_or_identifiers = ?, serial_number = ?, sub_location = ?,
                    estimated_replacement_value_usd = ?, receipt_verified = ?, receipt_id = ?,
                    receipt_match_type = ?, purchase_price_usd = ?, purchase_date = ?,
                    merchant = ?, order_number = ?, receipt_evidence = ?, source_image = ?,
                    overlay_image = ?, badge_verified = ?, description = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    primary.get("brand"),
                    primary.get("model_or_identifiers"),
                    primary.get("serial_number"),
                    primary.get("sub_location"),
                    primary.get("estimated_replacement_value_usd"),
                    1 if primary.get("receipt_verified") else 0,
                    primary.get("receipt_id"),
                    primary.get("receipt_match_type") or "auto",
                    primary.get("purchase_price_usd"),
                    primary.get("purchase_date"),
                    primary.get("merchant"),
                    primary.get("order_number"),
                    primary.get("receipt_evidence"),
                    primary.get("source_image"),
                    primary.get("overlay_image"),
                    1 if primary.get("badge_verified") else 0,
                    primary.get("description"),
                    now,
                    primary_id,
                ),
            )
            cur.execute(f"DELETE FROM items WHERE id IN ({placeholders})", clean_merge)
            conn.commit()
        return self.get_item(primary_id)

    def add_item_photo(self, item_id: int, photo_role: str, file_path: str, sha256: str = "") -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cur = conn.execute(
                "INSERT INTO item_photos (item_id, photo_role, file_path, sha256, created_at) VALUES (?, ?, ?, ?, ?)",
                (item_id, photo_role, file_path, sha256, now),
            )
            conn.commit()
            return int(cur.lastrowid)

    def get_item_photos(self, item_id: int) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM item_photos WHERE item_id = ? ORDER BY id ASC", (item_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def create_proposal(
        self,
        item_id: int,
        provider: str,
        model: str,
        proposed_brand: Optional[str] = None,
        proposed_model: Optional[str] = None,
        proposed_serial: Optional[str] = None,
        proposed_value_usd: Optional[float] = None,
        reasoning: str = "",
        est_cost_usd: float = 0.0,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            # Supersede any prior pending proposal for this item so the queue stays clean
            conn.execute(
                "UPDATE escalation_proposals SET status = 'superseded', resolved_at = ? WHERE item_id = ? AND status = 'pending'",
                (now, item_id),
            )
            cur = conn.execute(
                """
                INSERT INTO escalation_proposals (
                    item_id, provider, model, proposed_brand, proposed_model,
                    proposed_serial, proposed_value_usd, reasoning, est_cost_usd,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    item_id,
                    provider,
                    model,
                    proposed_brand,
                    proposed_model,
                    proposed_serial,
                    proposed_value_usd,
                    reasoning,
                    float(est_cost_usd or 0.0),
                    now,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def list_proposals(self, status: Optional[str] = "pending") -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            sql = """
                SELECT p.*, i.item_name, i.brand AS current_brand,
                       i.model_or_identifiers AS current_model,
                       i.serial_number AS current_serial,
                       i.estimated_replacement_value_usd AS current_value_usd,
                       i.room, i.source_image
                FROM escalation_proposals p
                JOIN items i ON i.id = p.item_id
            """
            params: List[Any] = []
            if status and status != "all":
                sql += " WHERE p.status = ?"
                params.append(status)
            sql += " ORDER BY p.id DESC"
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def apply_proposal(self, proposal_id: int, force: bool = False) -> Optional[Dict[str, Any]]:
        """Apply a pending escalation proposal to its item, respecting user_edited_fields unless force=True."""
        with self._get_connection() as conn:
            prop = conn.execute(
                "SELECT * FROM escalation_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if not prop:
                return None
            item = conn.execute("SELECT * FROM items WHERE id = ?", (prop["item_id"],)).fetchone()
            if not item:
                return None

            locked = set() if force else set(_parse_edited_fields(item["user_edited_fields"]))
            updates: Dict[str, Any] = {}
            if prop["proposed_brand"] and "brand" not in locked:
                updates["brand"] = prop["proposed_brand"]
            if prop["proposed_model"] and "model_or_identifiers" not in locked:
                updates["model_or_identifiers"] = prop["proposed_model"]
            if prop["proposed_serial"] and "serial_number" not in locked:
                updates["serial_number"] = prop["proposed_serial"]
            if prop["proposed_value_usd"] is not None and "estimated_replacement_value_usd" not in locked:
                updates["estimated_replacement_value_usd"] = float(prop["proposed_value_usd"])
            if updates:
                updates["badge_verified"] = 1

            now = datetime.now(timezone.utc).isoformat()
            if updates:
                updates["updated_at"] = now
                set_clause = ", ".join(f"{k} = ?" for k in updates)
                conn.execute(
                    f"UPDATE items SET {set_clause} WHERE id = ?",
                    [*updates.values(), prop["item_id"]],
                )
            conn.execute(
                "UPDATE escalation_proposals SET status = 'accepted', resolved_at = ? WHERE id = ?",
                (now, proposal_id),
            )
            conn.commit()
        return self.get_item(int(prop["item_id"]))

    def reject_proposal(self, proposal_id: int) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cur = conn.execute(
                "UPDATE escalation_proposals SET status = 'rejected', resolved_at = ? WHERE id = ?",
                (now, proposal_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def link_receipt(
        self,
        item_id: int,
        receipt_id: int,
        price_usd: Optional[float] = None,
        serial_number: Optional[str] = None,
        match_type: str = "manual",
    ) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            rec = conn.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
            item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            if not rec or not item:
                return None

            evidence = f"{rec['merchant']} Order #{rec['order_number']} ({rec['purchase_date']})"
            paid = price_usd if (price_usd is not None and price_usd > 0) else rec["total_usd"]
            sn = serial_number or item["serial_number"]
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                UPDATE items SET
                    receipt_verified = 1, receipt_id = ?, receipt_match_type = ?,
                    merchant = ?, order_number = ?, purchase_date = ?,
                    purchase_price_usd = ?, serial_number = ?, receipt_evidence = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    receipt_id,
                    match_type,
                    rec["merchant"],
                    rec["order_number"],
                    rec["purchase_date"],
                    paid,
                    sn,
                    evidence,
                    now,
                    item_id,
                ),
            )
            conn.commit()
        return self.get_item(item_id)

    def unlink_receipt(self, item_id: int) -> Optional[Dict[str, Any]]:
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                UPDATE items SET
                    receipt_verified = 0, receipt_id = NULL, receipt_match_type = 'auto',
                    merchant = NULL, order_number = NULL, purchase_date = NULL,
                    purchase_price_usd = NULL, receipt_evidence = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, item_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get_item(item_id)

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
            for fld in (
                "serial_number",
                "model_or_identifiers",
                "purchase_date",
                "merchant",
                "order_number",
                "receipt_evidence",
                "source_image",
                "overlay_image",
            ):
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

    def get_all_items(self, include_archived: bool = False) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            where = "" if include_archived else "WHERE COALESCE(i.review_status, 'unreviewed') != 'archived'"
            rows = conn.execute(
                f"""
                SELECT i.*,
                    (SELECT COUNT(*) FROM item_photos p WHERE p.item_id = i.id) AS photo_count,
                    (SELECT COUNT(*) FROM escalation_proposals ep WHERE ep.item_id = i.id AND ep.status = 'pending') AS pending_proposal_count
                FROM items i
                {where}
                ORDER BY COALESCE(i.estimated_replacement_value_usd, 0) DESC, i.id ASC
                """
            ).fetchall()
            out: List[Dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                d["user_edited_fields"] = _parse_edited_fields(d.get("user_edited_fields"))
                out.append(d)
            return out

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
            rows = conn.execute(
                """
                SELECT r.*,
                    (SELECT COUNT(*) FROM items i WHERE i.receipt_id = r.id OR (i.receipt_verified = 1 AND i.order_number = r.order_number)) AS linked_item_count
                FROM receipts r
                ORDER BY r.purchase_date DESC, r.id DESC
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def import_legacy_json(
        self,
        records: List[Dict[str, Any]],
        crops_dir: Optional[str] = None,
        overlays_dir: Optional[str] = None,
        replace_existing: bool = False,
    ) -> Dict[str, int]:
        """Import a curated legacy JSON inventory array into v3 SQLite (items + receipts + item_photos)."""
        from ..reconciliation.appraisal import estimate_acv

        if replace_existing:
            with self._get_connection() as conn:
                conn.execute("DELETE FROM escalation_proposals")
                conn.execute("DELETE FROM item_photos")
                conn.execute("DELETE FROM items")
                conn.execute("DELETE FROM receipts")
                conn.commit()

        crop_files = sorted(os.listdir(crops_dir)) if crops_dir and os.path.isdir(crops_dir) else []
        overlay_files = sorted(os.listdir(overlays_dir)) if overlays_dir and os.path.isdir(overlays_dir) else []

        def _infer_room(rec: Dict[str, Any]) -> str:
            if rec.get("room"):
                return str(rec["room"])
            blob = f"{rec.get('category', '')} {rec.get('item_name', '')}".lower()
            if any(k in blob for k in ("bicycle", "bike", "mower", "shredder", "tool", "garage", "outdoor", "zwift")):
                return "Garage"
            if any(k in blob for k in ("kitchen", "espresso", "gaggia", "oven", "appliance", "food processor", "coffee")):
                return "Kitchen"
            if any(k in blob for k in ("computer", "workstation", "laptop", "monitor", "camera", "nikon", "audio", "keyboard", "mouse", "tablet", "phone", "elgato", "focusrite", "nixie")):
                return "Office"
            if any(k in blob for k in ("closet", "clothes", "footwear", "boot", "shoe", "luggage", "suitcase", "jacket", "backpack")):
                return "Bedroom"
            return "Living Room"

        def _find_media(rec: Dict[str, Any]) -> tuple[str, str]:
            src = rec.get("source_image") or ""
            ov = rec.get("overlay_image") or ""
            if src and os.path.exists(src):
                return src, ov
            fn = os.path.splitext(os.path.basename(str(rec.get("filename") or "")))[0].lower()
            tokens = [t for t in re.split(r"[^a-z0-9]+", f"{rec.get('brand', '')} {rec.get('item_name', '')}".lower()) if len(t) >= 4]
            if fn and crops_dir:
                cands = [f for f in crop_files if fn in f.lower()]
                if cands:
                    cands.sort(key=lambda f: sum(1 for t in tokens if t in f.lower()), reverse=True)
                    src = os.path.join(crops_dir, cands[0])
            if not src and crops_dir and tokens:
                scored = [(sum(1 for t in tokens if t in f.lower()), f) for f in crop_files]
                best_score, best_f = max(scored, default=(0, ""))
                if best_score >= 2:
                    src = os.path.join(crops_dir, best_f)
            if not ov and fn and overlays_dir:
                ov_match = next((f for f in overlay_files if fn in f.lower()), "")
                if ov_match:
                    ov = os.path.join(overlays_dir, ov_match)
            return src, ov

        imported_items = 0
        imported_receipts = 0
        skipped_dups = 0
        seen_receipts: Dict[tuple[str, str], int] = {}

        def _to_text(val: Any, default: str = "") -> str:
            if val is None:
                return default
            if isinstance(val, dict):
                return ", ".join(f"{k}: {v}" for k, v in val.items() if v not in (None, "")) or default
            if isinstance(val, list):
                return ", ".join(str(x) for x in val if x not in (None, "")) or default
            return str(val).strip()

        def _to_opt_float(val: Any) -> Optional[float]:
            if val in (None, ""):
                return None
            try:
                return float(str(val).replace("$", "").replace(",", ""))
            except (TypeError, ValueError):
                return None

        for raw in records:
            name = _to_text(raw.get("item_name"))
            if not name or "[duplicate view]" in name.lower():
                skipped_dups += 1
                continue

            val = _to_opt_float(raw.get("estimated_replacement_value_usd")) or 0.0
            is_ver = bool(raw.get("receipt_verified"))
            rec_id: Optional[int] = None
            serial_txt = _to_text(raw.get("serial_number"))
            model_txt = _to_text(raw.get("model_or_identifiers") or raw.get("model"))
            brand_txt = _to_text(raw.get("brand"), default="Unknown") or "Unknown"
            p_price = _to_opt_float(raw.get("purchase_price_usd"))
            p_date = _to_text(raw.get("purchase_date"))
            merch_txt = _to_text(raw.get("merchant"))
            ord_txt = _to_text(raw.get("order_number"))
            ev_txt = _to_text(raw.get("receipt_evidence"))

            if is_ver:
                merch = merch_txt or "Verified Merchant"
                ord_num = ord_txt or f"IMPORT-{imported_items + 1:04d}"
                rkey = (merch.lower(), ord_num.lower())
                if rkey not in seen_receipts:
                    price = p_price if p_price is not None else val
                    rec_id = self.save_receipt(
                        {
                            "merchant": merch,
                            "order_number": ord_num,
                            "purchase_date": p_date[:10] or "2024-01-01",
                            "total_usd": price,
                            "source_message_id": f"import-{ord_num[:20]}",
                            "notes": ev_txt or _to_text(raw.get("verification_notes")),
                            "items": [
                                {
                                    "name": name,
                                    "price_usd": price,
                                    "quantity": 1,
                                    "serial_number": serial_txt or None,
                                }
                            ],
                        }
                    )
                    seen_receipts[rkey] = rec_id
                    imported_receipts += 1
                else:
                    rec_id = seen_receipts[rkey]

            src_img, ov_img = _find_media(raw)
            acv = _to_opt_float(raw.get("estimated_acv_usd"))
            if acv is None:
                acv = estimate_acv(val, _to_text(raw.get("category")), p_date or None, _to_text(raw.get("condition")))

            self.save_item(
                {
                    "item_name": name,
                    "category": _to_text(raw.get("category"), default="General") or "General",
                    "brand": brand_txt,
                    "model_or_identifiers": model_txt,
                    "room": _infer_room(raw),
                    "sub_location": _to_text(raw.get("sub_location")),
                    "condition": _to_text(raw.get("condition"), default="Good") or "Good",
                    "quantity": int(raw.get("quantity") or 1),
                    "estimated_replacement_value_usd": round(val, 2),
                    "estimated_acv_usd": round(float(acv), 2),
                    "receipt_verified": is_ver,
                    "receipt_match_type": "manual" if is_ver else "auto",
                    "receipt_id": rec_id,
                    "purchase_price_usd": p_price,
                    "purchase_date": p_date or None,
                    "merchant": merch_txt or None,
                    "order_number": ord_txt or None,
                    "serial_number": serial_txt,
                    "receipt_evidence": ev_txt or None,
                    "source_image": src_img,
                    "original_image": _to_text(raw.get("filename")),
                    "overlay_image": ov_img,
                    "badge_verified": bool(serial_txt or model_txt),
                    "review_status": "confirmed" if is_ver else "unreviewed",
                    "description": _to_text(raw.get("description")),
                }
            )
            imported_items += 1

        return {"items": imported_items, "receipts": imported_receipts, "skipped_duplicates": skipped_dups}
