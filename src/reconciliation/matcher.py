import difflib
import logging
import re
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel

from ..receipts.parsers.base import ParsedItem, ParsedReceipt
from ..vision.detector import DetectedItem

logger = logging.getLogger(__name__)


class MatchResult(BaseModel):
    item: DetectedItem
    receipt: Optional[ParsedReceipt] = None
    receipt_item: Optional[ParsedItem] = None
    match_confidence: float = 0.0
    match_type: str = "none"  # "serial_exact", "fuzzy_high", "brand_model", "none"


class InventoryMatcher:
    """
    Cross-modal reconciliation engine linking visual detections with digital receipts.
    Enforces quantity-aware 1-to-1 assignment in match_batch so a single receipt line
    cannot double-verify multiple distinct items.
    """

    def __init__(self, fuzzy_threshold: float = 0.70):
        self.fuzzy_threshold = fuzzy_threshold

    @staticmethod
    def _normalize(text: str) -> str:
        clean = re.sub(r"[^a-zA-Z0-9\s]", " ", text.lower())
        return " ".join(clean.split())

    def _token_similarity(self, s1: str, s2: str) -> float:
        n1 = set(self._normalize(s1).split())
        n2 = set(self._normalize(s2).split())
        if not n1 or not n2:
            return 0.0

        intersection = n1.intersection(n2)
        union = n1.union(n2)
        jaccard = len(intersection) / len(union)
        seq = difflib.SequenceMatcher(None, self._normalize(s1), self._normalize(s2)).ratio()
        return (jaccard * 0.4) + (seq * 0.6)

    def _score_pair(self, item: DetectedItem, line: ParsedItem) -> Tuple[float, str]:
        item_str = f"{item.brand or ''} {item.model or ''} {item.item_name}"
        line_str = f"{line.name} {line.sku or ''} {line.serial_number or ''}"

        # 1. Exact Serial Number Match (100% confidence)
        if line.serial_number:
            line_sn_clean = line.serial_number.strip().lower()
            item_sn = (item.serial_number or "").strip().lower()
            item_model = (item.model or "").strip().lower()
            item_desc = (item.description or "").strip().lower()
            if (
                (item_sn and line_sn_clean == item_sn)
                or (item_model and line_sn_clean in item_model)
                or (item_desc and line_sn_clean in item_desc)
            ):
                return 1.0, "serial_exact"

        # 2. Key identifier & fuzzy similarity
        keywords = [k for k in re.split(r"\s+", self._normalize(item_str)) if len(k) >= 4]
        norm_line = self._normalize(line_str)
        keyword_hits = sum(1 for k in keywords if k in norm_line)
        keyword_ratio = (keyword_hits / len(keywords)) if keywords else 0.0
        sim = self._token_similarity(item_str, line.name)
        combined = (keyword_ratio * 0.5) + (sim * 0.5)
        match_type = "fuzzy_high" if combined >= 0.85 else "brand_model"
        return combined, match_type

    def match_item_to_receipts(
        self,
        item: DetectedItem,
        receipts: List[ParsedReceipt],
    ) -> MatchResult:
        """Find the best matching receipt and line item for a single visual asset."""
        best_receipt: Optional[ParsedReceipt] = None
        best_line_item: Optional[ParsedItem] = None
        best_score = 0.0
        best_type = "none"

        for r in receipts:
            for line in r.items:
                score, mtype = self._score_pair(item, line)
                if mtype == "serial_exact":
                    return MatchResult(
                        item=item,
                        receipt=r,
                        receipt_item=line,
                        match_confidence=1.0,
                        match_type="serial_exact",
                    )
                if score > best_score:
                    best_score = score
                    best_receipt = r
                    best_line_item = line
                    best_type = mtype

        if best_score >= self.fuzzy_threshold and best_receipt and best_line_item:
            return MatchResult(
                item=item,
                receipt=best_receipt,
                receipt_item=best_line_item,
                match_confidence=round(best_score, 3),
                match_type=best_type,
            )

        return MatchResult(item=item, match_confidence=0.0, match_type="none")

    def match_batch(
        self,
        items: List[DetectedItem],
        receipts: List[ParsedReceipt],
    ) -> List[MatchResult]:
        """
        Quantity-aware 1-to-1 greedy assignment across all items and receipt lines.
        Exact serial matches always take priority; each receipt line can match at most
        line.quantity items (default 1).
        """
        candidates: List[Tuple[int, float, int, int, int, str]] = []
        remaining_qty: Dict[Tuple[int, int], int] = {}

        for r_idx, r in enumerate(receipts):
            for l_idx, line in enumerate(r.items):
                remaining_qty[(r_idx, l_idx)] = max(1, int(getattr(line, "quantity", 1) or 1))

        for i_idx, item in enumerate(items):
            for r_idx, r in enumerate(receipts):
                for l_idx, line in enumerate(r.items):
                    score, mtype = self._score_pair(item, line)
                    if mtype == "serial_exact" or score >= self.fuzzy_threshold:
                        priority = 1 if mtype == "serial_exact" else 0
                        candidates.append((priority, score, i_idx, r_idx, l_idx, mtype))

        candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)

        assigned: Dict[int, MatchResult] = {}
        for _, score, i_idx, r_idx, l_idx, mtype in candidates:
            if i_idx in assigned:
                continue
            if remaining_qty.get((r_idx, l_idx), 0) <= 0:
                continue
            remaining_qty[(r_idx, l_idx)] -= 1
            assigned[i_idx] = MatchResult(
                item=items[i_idx],
                receipt=receipts[r_idx],
                receipt_item=receipts[r_idx].items[l_idx],
                match_confidence=1.0 if mtype == "serial_exact" else round(score, 3),
                match_type=mtype,
            )

        return [
            assigned.get(i_idx, MatchResult(item=item, match_confidence=0.0, match_type="none"))
            for i_idx, item in enumerate(items)
        ]

    def rank_receipt_candidates(
        self,
        item: DetectedItem,
        receipts: List[Tuple[int, ParsedReceipt]],
        top_k: int = 3,
    ) -> List[Dict[str, object]]:
        """Return top-K scored receipt candidates for interactive 1-click linking (even below threshold)."""
        scored: List[Dict[str, object]] = []
        for rec_id, rec in receipts:
            best_score = 0.0
            best_line: Optional[ParsedItem] = None
            best_type = "none"
            for line in rec.items:
                sc, mt = self._score_pair(item, line)
                if sc > best_score:
                    best_score = sc
                    best_line = line
                    best_type = mt
            scored.append(
                {
                    "receipt_id": rec_id,
                    "merchant": rec.merchant,
                    "order_number": rec.order_number,
                    "purchase_date": rec.purchase_date,
                    "total_usd": rec.total_usd,
                    "matched_line": best_line.name if best_line else "",
                    "line_price_usd": best_line.price_usd if best_line else rec.total_usd,
                    "line_serial": best_line.serial_number if best_line else None,
                    "confidence": round(best_score, 3),
                    "match_type": best_type,
                }
            )
        scored.sort(key=lambda x: float(x["confidence"]), reverse=True)
        return scored[:top_k]
