import logging
from typing import List, Dict, Any, Tuple
from pydantic import BaseModel
from .matcher import MatchResult

logger = logging.getLogger(__name__)


class PortfolioAuditStats(BaseModel):
    total_items: int
    total_replacement_value_usd: float
    verified_items_count: int
    verified_valuation_usd: float
    verification_percentage: float
    categories: Dict[str, Dict[str, Any]] = {}


class AppraisalEngine:
    """
    Computes insurance replacement values, validates market equity,
    and produces consolidated portfolio audit statistics.
    """

    def __init__(self, default_inflation_rate: float = 0.03):
        self.default_inflation_rate = default_inflation_rate

    def evaluate_portfolio(self, matches: List[MatchResult]) -> Tuple[List[Dict[str, Any]], PortfolioAuditStats]:
        """
        Enriches inventory records with receipt evidence and calculates
        macro-level audit portfolio metrics.
        """
        reconciled_records = []
        total_value = 0.0
        verified_value = 0.0
        verified_count = 0
        categories: Dict[str, Dict[str, Any]] = {}

        for m in matches:
            it = m.item
            rep_val = it.estimated_replacement_value_usd

            is_verified = m.receipt is not None
            purchase_price = m.receipt_item.price_usd if (m.receipt_item and m.receipt_item.price_usd > 0) else None
            purchase_date = m.receipt.purchase_date if m.receipt else None
            merchant = m.receipt.merchant if m.receipt else None
            order_num = m.receipt.order_number if m.receipt else None
            serial_num = (m.receipt_item.serial_number if m.receipt_item else None) or getattr(it, "serial_number", None) or it.model

            # If item had $0 estimate but receipt exists, elevate replacement baseline
            if rep_val <= 0.0 and purchase_price and purchase_price > 0.0:
                rep_val = purchase_price

            evidence_str = None
            if is_verified and m.receipt:
                evidence_str = f"{m.receipt.merchant} Order #{m.receipt.order_number} ({m.receipt.purchase_date})"

            record = {
                "item_name": it.item_name,
                "category": it.category,
                "brand": it.brand,
                "model_or_identifiers": it.model or "",
                "condition": it.condition,
                "estimated_replacement_value_usd": round(rep_val, 2),
                "receipt_verified": is_verified,
                "purchase_price_usd": round(purchase_price, 2) if purchase_price else None,
                "purchase_date": purchase_date,
                "merchant": merchant,
                "order_number": order_num,
                "serial_number": serial_num,
                "receipt_evidence": evidence_str,
                "match_confidence": m.match_confidence,
                "source_image": it.source_image,
                "description": it.description
            }
            reconciled_records.append(record)

            total_value += rep_val
            if is_verified:
                verified_count += 1
                verified_value += rep_val

            # Category tally
            cat = it.category or "General"
            if cat not in categories:
                categories[cat] = {"count": 0, "value": 0.0, "verified_count": 0, "verified_val": 0.0}
            categories[cat]["count"] += 1
            categories[cat]["value"] += rep_val
            if is_verified:
                categories[cat]["verified_count"] += 1
                categories[cat]["verified_val"] += rep_val

        pct = (verified_value / total_value * 100.0) if total_value > 0 else 0.0
        stats = PortfolioAuditStats(
            total_items=len(reconciled_records),
            total_replacement_value_usd=round(total_value, 2),
            verified_items_count=verified_count,
            verified_valuation_usd=round(verified_value, 2),
            verification_percentage=round(pct, 1),
            categories=categories
        )

        return reconciled_records, stats
