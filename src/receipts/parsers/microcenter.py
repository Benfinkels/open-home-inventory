import re
from typing import Optional, List
from .base import BaseReceiptParser, ParsedReceipt, ParsedItem


class MicroCenterReceiptParser(BaseReceiptParser):
    """Parses Micro Center store receipts, eReceipts, and pickup reservations."""

    def can_parse(self, sender: str, subject: str, body: str) -> bool:
        s = sender.lower()
        b = body.lower()
        return "microcenter" in s or "micro center" in b

    def parse(self, message_id: str, date: str, sender: str, subject: str, body: str) -> Optional[ParsedReceipt]:
        ref_match = re.search(r"(?:Reference Number|Order Number)[:\s]+([0-9A-Za-z\-]+)", body, re.IGNORECASE)
        order_number = ref_match.group(1) if ref_match else f"MC-{message_id[:8]}"

        total = 0.0
        total_match = re.search(r"(?:Sale TOTAL|Total Due|TOTAL)[:\s]*\$?([0-9,]+\.[0-9]{2})", body, re.IGNORECASE)
        if total_match:
            total = float(total_match.group(1).replace(",", ""))

        items: List[ParsedItem] = []
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        i = 0
        while i < len(lines):
            line = lines[i]
            # Check for item header like "1. Item Name"
            numbered_match = re.match(r"^\d+\.\s+(.+)$", line)
            if numbered_match:
                item_name = numbered_match.group(1).strip()
                item_sku = None
                item_sn = None
                item_price = 0.0

                j = i + 1
                while j < min(len(lines), i + 6):
                    sub = lines[j]
                    if re.match(r"^\d+\.\s+", sub) or "SUBTOTAL" in sub.upper() or "TOTAL:" in sub.upper():
                        break
                    sku_m = re.search(r"SKU:\s*(\d+)", sub, re.IGNORECASE)
                    if sku_m:
                        item_sku = sku_m.group(1)
                    sn_m = re.search(r"(?:Serial Number|S/N):\s*([A-Za-z0-9\-]+)", sub, re.IGNORECASE)
                    if sn_m:
                        item_sn = sn_m.group(1)
                    pr_m = re.search(r"Price:\s*\$?([0-9,]+\.[0-9]{2})", sub, re.IGNORECASE)
                    if pr_m:
                        item_price = float(pr_m.group(1).replace(",", ""))
                    j += 1
                items.append(ParsedItem(
                    name=item_name,
                    price_usd=item_price,
                    sku=item_sku,
                    serial_number=item_sn,
                    quantity=1
                ))
                i = j
                continue

            # Check single line tabular format: 643718 AMD AMD RYZEN ... 349.99
            tab_match = re.match(r"^(\d{6})\s+(.+?)\s+(\d+)\s+([0-9,]+\.[0-9]{2})$", line)
            if tab_match:
                sku, desc, qty, pr = tab_match.groups()
                if not any(skip in desc for skip in ["Subtotal", "Tax", "Total", "Master Card", "Visa"]):
                    items.append(ParsedItem(
                        name=desc.strip(),
                        price_usd=float(pr.replace(",", "")),
                        sku=sku,
                        quantity=int(qty)
                    ))
            i += 1

        return ParsedReceipt(
            merchant="Micro Center",
            order_number=order_number,
            purchase_date=date[:10],
            total_usd=total,
            items=items,
            source_message_id=message_id,
            notes="Parsed via MicroCenterReceiptParser"
        )
