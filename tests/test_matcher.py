"""
Unit tests for Reconciliation Engine: exact serial number linking,
fuzzy token set matching, and portfolio appraisal calculations.
"""

import pytest
from src.vision.detector import DetectedItem
from src.receipts.parsers.base import ParsedReceipt, ParsedItem
from src.reconciliation.matcher import InventoryMatcher
from src.reconciliation.appraisal import AppraisalEngine


def test_exact_serial_matcher():
    item = DetectedItem(
        item_name="GeForce RTX 4090 FE",
        category="Electronics",
        brand="NVIDIA",
        model="RTX 4090",
        serial_number="SN-RTX4090-FE-2024-8842",
        estimated_replacement_value_usd=1800.00
    )

    rec_item = ParsedItem(
        name="NVIDIA GEFORCE RTX 4090 24GB",
        price_usd=1599.99,
        serial_number="SN-RTX4090-FE-2024-8842"
    )
    receipt = ParsedReceipt(
        merchant="Micro Center",
        order_number="MC-892410-ORD",
        purchase_date="2023-11-15",
        total_usd=1699.98,
        items=[rec_item]
    )

    matcher = InventoryMatcher()
    result = matcher.match_item_to_receipts(item, [receipt])

    assert result.match_type == "serial_exact"
    assert result.match_confidence == 1.0
    assert result.receipt is not None
    assert result.receipt.merchant == "Micro Center"


def test_fuzzy_matcher_similarity():
    item = DetectedItem(
        item_name="UltraSharp 27 4K USB-C Hub Monitor (U2723QE)",
        category="Electronics",
        brand="Dell",
        model="U2723QE",
        estimated_replacement_value_usd=599.00
    )

    rec_item = ParsedItem(
        name="Dell UltraSharp 27 4K USB-C Hub Monitor (U2723QE) - 3840 x 2160 UHD",
        price_usd=579.99
    )
    receipt = ParsedReceipt(
        merchant="Amazon.com",
        order_number="112-9842109-8472911",
        purchase_date="2023-08-10",
        total_usd=616.24,
        items=[rec_item]
    )

    matcher = InventoryMatcher(fuzzy_threshold=0.65)
    result = matcher.match_item_to_receipts(item, [receipt])

    assert result.match_confidence >= 0.70
    assert result.receipt is not None
    assert result.receipt.merchant == "Amazon.com"


def test_appraisal_portfolio_evaluation():
    item1 = DetectedItem(
        item_name="RTX 4090",
        category="Electronics",
        brand="NVIDIA",
        model="4090",
        serial_number="SN-4090",
        estimated_replacement_value_usd=1800.00
    )
    item2 = DetectedItem(
        item_name="Office Chair",
        category="Furniture",
        brand="Herman Miller",
        model="Aeron",
        estimated_replacement_value_usd=1200.00
    )

    rec_item = ParsedItem(name="NVIDIA RTX 4090", price_usd=1599.99, serial_number="SN-4090")
    receipt = ParsedReceipt(
        merchant="Micro Center",
        order_number="MC-1",
        purchase_date="2023-11-15",
        total_usd=1599.99,
        items=[rec_item]
    )

    matcher = InventoryMatcher()
    matches = matcher.match_batch([item1, item2], [receipt])

    appraisal = AppraisalEngine()
    records, stats = appraisal.evaluate_portfolio(matches)

    assert stats.total_items == 2
    assert stats.verified_items_count == 1
    assert stats.total_replacement_value_usd == 3000.00
    assert stats.verified_valuation_usd == 1800.00
    assert stats.verification_percentage == 60.0
