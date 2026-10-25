import os
from datetime import datetime
from typing import List, Dict, Any
from ..reconciliation.appraisal import PortfolioAuditStats


def export_inventory_markdown(
    items: List[Dict[str, Any]],
    stats: PortfolioAuditStats,
    output_path: str
):
    """
    Generates an executive insurance appraisal and audit-proof inventory summary
    formatted for submission to insurance underwriters.
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    verified_items = [it for it in items if it.get("receipt_verified")]

    md = f"""# Master Home Inventory & Proof-of-Purchase Ledger

*Generated on {datetime.now().strftime('%B %d, %Y at %H:%M:%S')}*  
*Audit Engine: **Open Home Inventory Pipeline** (Multimodal Vision & Digital Ingestion)*

---

## 1. Executive Portfolio Summary

| Metric | Valuation / Count | Audit Significance |
| :--- | :--- | :--- |
| **Total Cataloged Assets** | **{stats.total_items} items** | Optical multi-item vision extractions + hardware probes |
| **Total Replacement Valuation** | **${stats.total_replacement_value_usd:,.2f} USD** | Market replacement appraisal baseline |
| **Receipt-Backed Assets** | **{stats.verified_items_count} items ({stats.verification_percentage:.1f}%)** | Sourced from authentic merchant orders & e-receipts |
| **Verified Valuation** | **${stats.verified_valuation_usd:,.2f} USD** | Total asset value backed by formal proof-of-purchase |

---

## 2. Category Breakdown

| Category | Item Count | Replacement Value | Verified Items | Verified Value (% of Category) |
| :--- | :---: | :---: | :---: | :---: |
"""

    for cat_name, c_data in sorted(stats.categories.items(), key=lambda x: x[1]["value"], reverse=True):
        c_val = c_data["value"]
        v_val = c_data["verified_val"]
        pct = (v_val / c_val * 100.0) if c_val > 0 else 0.0
        md += f"| **{cat_name}** | {c_data['count']} | ${c_val:,.2f} | {c_data['verified_count']} | ${v_val:,.2f} ({pct:.1f}%) |\n"

    md += """
---

## 3. Verified Proof-of-Purchase Ledger (Audit-Ready)

The following high-value assets have been reconciled against merchant order confirmations, store e-receipts, and manufacturer registries:

| Item Name | Brand & Model | Serial / Model # | Purchase Date | Amount Paid | Merchant & Order Ref | Evidence Source |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
"""

    for it in sorted(verified_items, key=lambda x: (x.get("estimated_replacement_value_usd") or 0.0), reverse=True):
        name = it.get("item_name", "Item")[:36]
        brand = (it.get("brand") or "N/A")[:18]
        sn = (it.get("serial_number") or it.get("model_or_identifiers") or "N/A")[:30]
        pdate = it.get("purchase_date") or "N/A"
        paid = it.get("purchase_price_usd")
        val = it.get("estimated_replacement_value_usd") or 0.0
        paid_str = f"${paid:,.2f}" if paid else f"${val:,.2f}"
        merchant = (it.get("merchant") or "N/A")[:26]
        evidence = (it.get("receipt_evidence") or "Verified")[:40]

        md += f"| **{name}** | {brand} | `{sn}` | {pdate} | **{paid_str}** | {merchant} | {evidence} |\n"

    md += """
---

## 4. Complete Cataloged Inventory

| Item Name | Category | Brand | Condition | Replacement Value | Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
"""

    for it in items:
        status_badge = "Verified Receipt" if it.get("receipt_verified") else "Visual Estimate"
        name = it.get("item_name", "Item")[:40]
        cat = it.get("category", "General")[:30]
        brand = it.get("brand") or "N/A"
        cond = it.get("condition") or "Good"
        val = it.get("estimated_replacement_value_usd") or 0.0
        md += f"| **{name}** | {cat} | {brand} | {cond} | ${val:,.2f} | {status_badge} |\n"

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(md)
