from datetime import datetime
import logging
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from .matcher import MatchResult

logger = logging.getLogger(__name__)

# Standard straight-line useful life (years) by category keyword for ACV estimation.
# shortcut: simplified category depreciation curve; upgrade to carrier-specific schedules if filing a large claim.
CATEGORY_USEFUL_LIFE_YEARS = {
    "computer": 5.0,
    "laptop": 5.0,
    "electronics": 5.0,
    "monitor": 6.0,
    "photography": 7.0,
    "camera": 7.0,
    "audio": 8.0,
    "bicycle": 10.0,
    "bike": 10.0,
    "sports": 10.0,
    "tool": 10.0,
    "appliance": 12.0,
    "kitchen": 10.0,
    "furniture": 15.0,
}

CONDITION_MULTIPLIER = {
    "mint": 0.95,
    "excellent": 0.85,
    "good": 0.72,
    "fair": 0.55,
    "poor": 0.35,
}


def estimate_acv(
    rcv_usd: float,
    category: Optional[str] = None,
    purchase_date: Optional[str] = None,
    condition: Optional[str] = None,
) -> float:
    """Estimate Actual Cash Value (depreciated RCV) with a 25% salvage floor."""
    if not rcv_usd or rcv_usd <= 0:
        return 0.0
    cat_lower = (category or "").lower()
    life_years = 10.0
    for kw, years in CATEGORY_USEFUL_LIFE_YEARS.items():
        if kw in cat_lower:
            life_years = years
            break

    age_years: Optional[float] = None
    if purchase_date:
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%Y"):
            try:
                dt = datetime.strptime(str(purchase_date).strip()[:10], fmt)
                age_years = max(0.0, (datetime.now() - dt).days / 365.25)
                break
            except ValueError:
                continue

    if age_years is not None:
        factor = max(0.25, 1.0 - (age_years / life_years))
    else:
        factor = CONDITION_MULTIPLIER.get((condition or "good").strip().lower(), 0.72)
    return round(rcv_usd * factor, 2)


class PortfolioAuditStats(BaseModel):
    total_items: int
    total_replacement_value_usd: float
    total_acv_usd: float = 0.0
    verified_items_count: int
    verified_valuation_usd: float
    verification_percentage: float
    categories: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    missing_evidence_items: List[Dict[str, Any]] = Field(default_factory=list)


class AppraisalEngine:
    """
    Computes insurance replacement values, validates market equity,
    flags high-value items missing proof-of-purchase or serial numbers,
    and produces consolidated portfolio audit statistics.
    """

    def __init__(self, default_inflation_rate: float = 0.03, high_value_threshold_usd: float = 500.0):
        self.default_inflation_rate = default_inflation_rate
        self.high_value_threshold_usd = high_value_threshold_usd

    def evaluate_portfolio(self, matches: List[MatchResult]) -> Tuple[List[Dict[str, Any]], PortfolioAuditStats]:
        reconciled_records = []
        total_value = 0.0
        total_acv = 0.0
        verified_value = 0.0
        verified_count = 0
        categories: Dict[str, Dict[str, Any]] = {}
        missing_evidence: List[Dict[str, Any]] = []

        for m in matches:
            it = m.item
            rep_val = float(it.estimated_replacement_value_usd or 0.0)

            is_verified = m.receipt is not None
            purchase_price = m.receipt_item.price_usd if (m.receipt_item and m.receipt_item.price_usd > 0) else None
            purchase_date = m.receipt.purchase_date if m.receipt else None
            merchant = m.receipt.merchant if m.receipt else None
            order_num = m.receipt.order_number if m.receipt else None
            # Never silently confuse model names with actual serial numbers
            serial_num = (m.receipt_item.serial_number if m.receipt_item else None) or it.serial_number or None

            if rep_val <= 0.0 and purchase_price and purchase_price > 0.0:
                rep_val = purchase_price

            acv_val = estimate_acv(rep_val, it.category, purchase_date, it.condition)

            evidence_str = None
            if is_verified and m.receipt:
                evidence_str = f"{m.receipt.merchant} Order #{m.receipt.order_number} ({m.receipt.purchase_date})"

            record = {
                "item_name": it.item_name,
                "category": it.category,
                "brand": it.brand,
                "model_or_identifiers": it.model or "",
                "room": it.room or "General",
                "condition": it.condition,
                "estimated_replacement_value_usd": round(rep_val, 2),
                "estimated_acv_usd": acv_val,
                "receipt_verified": is_verified,
                "purchase_price_usd": round(purchase_price, 2) if purchase_price else None,
                "purchase_date": purchase_date,
                "merchant": merchant,
                "order_number": order_num,
                "serial_number": serial_num,
                "receipt_evidence": evidence_str,
                "match_confidence": m.match_confidence,
                "source_image": it.source_image,
                "original_image": it.original_image,
                "badge_verified": it.badge_verified,
                "description": it.description,
            }
            reconciled_records.append(record)

            total_value += rep_val
            total_acv += acv_val
            if is_verified:
                verified_count += 1
                verified_value += rep_val

            if rep_val >= self.high_value_threshold_usd:
                gaps = []
                if not is_verified:
                    gaps.append("receipt")
                if not serial_num:
                    gaps.append("serial_number")
                if gaps:
                    missing_evidence.append(
                        {
                            "item_name": it.item_name,
                            "room": it.room or "General",
                            "estimated_replacement_value_usd": round(rep_val, 2),
                            "missing": gaps,
                        }
                    )

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
            total_acv_usd=round(total_acv, 2),
            verified_items_count=verified_count,
            verified_valuation_usd=round(verified_value, 2),
            verification_percentage=round(pct, 1),
            categories=categories,
            missing_evidence_items=sorted(
                missing_evidence,
                key=lambda x: x["estimated_replacement_value_usd"],
                reverse=True,
            ),
        )
        return reconciled_records, stats
