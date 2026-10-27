import unittest
import logging
import re
from typing import List, Optional
from dataclasses import dataclass

# --- Standalone Data Models ---
@dataclass
class DetectedItem:
    brand: Optional[str] = None
    model: Optional[str] = None
    item_name: str = ""
    serial_number: Optional[str] = None
    description: Optional[str] = None

@dataclass
class ParsedItem:
    name: str = ""
    sku: Optional[str] = None
    serial_number: Optional[str] = None

@dataclass
class ParsedReceipt:
    items: List[ParsedItem]

# --- Source Code Implementation ---
import difflib

class MatchResult:
    def __init__(self, item: DetectedItem, receipt: Optional[ParsedReceipt] = None, 
                 receipt_item: Optional[ParsedItem] = None, match_confidence: float = 0.0, 
                 match_type: str = "none"):
        self.item = item
        self.receipt = receipt
        self.receipt_item = receipt_item
        self.match_confidence = match_confidence
        self.match_type = match_type

class InventoryMatcher:
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

    def match_item_to_receipts(self, item: DetectedItem, receipts: List[ParsedReceipt]) -> MatchResult:
        best_receipt: Optional[ParsedReceipt] = None
        best_line_item: Optional[ParsedItem] = None
        best_score = 0.0
        match_type = "none"

        item_str = f"{item.brand or ''} {item.model or ''} {item.item_name}"

        for r in receipts:
            for line in r.items:
                line_str = f"{line.name} {line.sku or ''} {line.serial_number or ''}"

                # 1. Exact Serial Number Match
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

                # 2. Key Identifier Match
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

    def match_batch(self, items: List[DetectedItem], receipts: List[ParsedReceipt]) -> List[MatchResult]:
        return [self.match_item_to_receipts(it, receipts) for it in items]

# --- Test Suite ---
class TestInventoryMatcher(unittest.TestCase):

    def setUp(self):
        self.matcher = InventoryMatcher()
        self.receipts = [
            ParsedReceipt(items=[
                ParsedItem(name="NVIDIA GeForce RTX 4090", serial_number="SN123456"),
                ParsedItem(name="Samsung 4K UHD Monitor", sku="SKU-ABC"),
            ])
        ]

    def test_exact_serial_number_match(self):
        item = DetectedItem(item_name="Graphics Card", serial_number="SN123456")
        result = self.matcher.match_item_to_receipts(item, self.receipts)
        self.assertEqual(result.match_confidence, 1.0)
        self.assertEqual(result.match_type, "serial_exact")
        self.assertIsNotNone(result.receipt_item)

    def test_serial_in_model_match(self):
        item = DetectedItem(model="Model SN123456", item_name="Widget")
        result = self.matcher.match_item_to_receipts(item, self.receipts)
        self.assertEqual(result.match_confidence, 1.0)
        self.assertEqual(result.match_type, "serial_exact")

    def test_brand_model_fuzzy_match(self):
        item = DetectedItem(brand="NVIDIA", model="RTX 4090", item_name="GPU")
        result = self.matcher.match_item_to_receipts(item, self.receipts)
        self.assertGreaterEqual(result.match_confidence, self.matcher.fuzzy_threshold)
        self.assertIn(result.match_type, ["fuzzy_high", "brand_model"])

    def test_unrelated_item_no_match(self):
        item = DetectedItem(item_name="Toaster", brand="KitchenAid")
        result = self.matcher.match_item_to_receipts(item, self.receipts)
        self.assertEqual(result.match_confidence, 0.0)
        self.assertEqual(result.match_type, "none")

    def test_none_attributes(self):
        item = DetectedItem(item_name="Unknown Item")
        result = self.matcher.match_item_to_receipts(item, self.receipts)
        self.assertEqual(result.match_confidence, 0.0)
        self.assertEqual(result.match_type, "none")

    def test_special_characters(self):
        item = DetectedItem(item_name="Item @#$%^&*()!", brand="Test-Brand_123")
        result = self.matcher.match_item_to_receipts(item, self.receipts)
        # Should not crash and should return a valid MatchResult
        self.assertIsInstance(result, MatchResult)

    def test_empty_description(self):
        item = DetectedItem(item_name="Item", description="")
        result = self.matcher.match_item_to_receipts(item, self.receipts)
        self.assertIsInstance(result, MatchResult)

    def test_match_batch(self):
        items = [
            DetectedItem(item_name="RTX 4090", brand="NVIDIA"),
            DetectedItem(item_name="Toaster", brand="KitchenAid"),
        ]
        results = self.matcher.match_batch(items, self.receipts)
        self.assertEqual(len(results), 2)
        self.assertGreaterEqual(results[0].match_confidence, self.matcher.fuzzy_threshold)
        self.assertEqual(results[1].match_confidence, 0.0)

if __name__ == '__main__':
    unittest.main()