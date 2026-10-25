import difflib
import logging
import re
from typing import List, Optional, Tuple, Dict, Any
from pydantic import BaseModel
from ..vision.detector import DetectedItem
from ..receipts.parsers.base import ParsedReceipt, ParsedItem

logger = logging.getLogger(__name__)


class MatchResult(BaseModel):
    item: DetectedItem
    receipt: Optional[ParsedReceipt] = None
    receipt_item: Optional[ParsedItem] = None
    match_confidence: float = 0.0
    match_type: str = "none"  # "serial_exact", "fuzzy_high", "brand_model", "none"


class InventoryMatcher:
    """
    Cross-modal reconciliation engine that links visual physical item detections
    with digital purchase orders and manufacturer e-receipts.
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

        # Sequence matcher ratio for word overlap
        seq = difflib.SequenceMatcher(None, self._normalize(s1), self._normalize(s2)).ratio()
        return (jaccard * 0.4) + (seq * 0.6)

    def match_item_to_receipts(
        self,
        item: DetectedItem,
        receipts: List[ParsedReceipt]
    ) -> MatchResult:
        """Finds the best matching receipt and line item for a given visual asset."""
        best_receipt: Optional[ParsedReceipt] = None
        best_line_item: Optional[ParsedItem] = None
        best_score = 0.0
        match_type = "none"

        item_str = f"{item.brand or ''} {item.model or ''} {item.item_name}"

        for r in receipts:
            for line in r.items:
                line_str = f"{line.name} {line.sku or ''} {line.serial_number or ''}"

                # 1. Exact Serial Number Match (100% confidence)
                if line.serial_number:
                    line_sn_clean = line.serial_number.strip().lower()
                    item_sn = (item.serial_number or "").strip().lower()
                    item_model = (item.model or "").strip().lower()
                    item_desc = (item.description or "").strip().lower()

                    if (item_sn and line_sn_clean == item_sn) or \
                       (item_model and line_sn_clean in item_model) or \
                       (item_desc and line_sn_clean in item_desc):
                        return MatchResult(
                            item=item,
                            receipt=r,
                            receipt_item=line,
                            match_confidence=1.0,
                            match_type="serial_exact"
                        )

                # 2. Key Identifier Match (e.g. RTX 4090, AW3423DW, Stumpjumper)
                keywords = [k for k in re.split(r"\s+", self._normalize(item_str)) if len(k) >= 4]
                keyword_hits = sum(1 for k in keywords if k in self._normalize(line_str))
                keyword_ratio = (keyword_hits / len(keywords)) if keywords else 0.0

                # 3. Fuzzy similarity
                sim = self._token_similarity(item_str, line.name)
                combined_score = (keyword_ratio * 0.5) + (sim * 0.5)

                if combined_score > best_score:
                    best_score = combined_score
                    best_receipt = r
                    best_line_item = line
                    match_type = "fuzzy_high" if combined_score >= 0.85 else "brand_model"

        if best_score >= self.fuzzy_threshold and best_receipt and best_line_item:
            return MatchResult(
                item=item,
                receipt=best_receipt,
                receipt_item=best_line_item,
                match_confidence=round(best_score, 3),
                match_type=match_type
            )

        return MatchResult(item=item, match_confidence=0.0, match_type="none")

    def match_batch(
        self,
        items: List[DetectedItem],
        receipts: List[ParsedReceipt]
    ) -> List[MatchResult]:
        """Runs batch reconciliation across all cataloged items."""
        return [self.match_item_to_receipts(it, receipts) for it in items]
