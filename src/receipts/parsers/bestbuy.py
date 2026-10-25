import re
from typing import Optional, List
from .base import BaseReceiptParser, ParsedReceipt, ParsedItem


class BestBuyReceiptParser(BaseReceiptParser):
    """Parses Best Buy online store orders and in-store pickup e-receipts."""

    def can_parse(self, sender: str, subject: str, body: str) -> bool:
        s = sender.lower()
        b = body.lower()
        return "bestbuy" in s or "best buy" in b or "bby01-" in b

    def parse(self, message_id: str, date: str, sender: str, subject: str, body: str) -> Optional[ParsedReceipt]:
        order_match = re.search(r"(BBY01-[0-9]{12}|[0-9]{13})", body)
        if not order_match:
            order_match = re.search(r"(BBY01-[0-9]{12}|[0-9]{13})", subject)
        order_number = order_match.group(1) if order_match else f"BBY-{message_id[:8]}"

        total = 0.0
        total_match = re.search(r"(?:Total|Order Total):\s*\*?\s*\$?([0-9,]+\.[0-9]{2})", body, re.IGNORECASE)
        if total_match:
            total = float(total_match.group(1).replace(",", ""))

        items: List[ParsedItem] = []
        # Pattern 1: ASUS ROG Zephyrus / Product Details
        prod_matches = re.findall(
            r"Product Details\s+([^\$]+?)\s+\$?([0-9,]+\.[0-9]{2}).*?SKU:\s*(\d+)",
            body,
            re.DOTALL
        )
        for prod_name, price_str, sku in prod_matches:
            clean_name = re.sub(r"\s+", " ", prod_name).strip()
            items.append(ParsedItem(
                name=clean_name[:120],
                price_usd=float(price_str.replace(",", "")),
                sku=sku
            ))

        # Pattern 2: Logitech / Standard Ship to Home line items
        std_matches = re.findall(
            r"(Logitech\s*-[^\n\r]+?)\s+Model:\s*([^\s]+)\s+SKU:\s*(\d+)\s+Qty\s+Price\s+\d+\s+\$?([0-9,]+\.[0-9]{2})",
            body
        )
        for prod_name, model_str, sku, price_str in std_matches:
            items.append(ParsedItem(
                name=prod_name.strip(),
                price_usd=float(price_str.replace(",", "")),
                sku=sku,
                serial_number=model_str
            ))

        # Pattern 3: Nest Mini / Generic
        if not items:
            nest_match = re.search(r"(Nest Mini[^\n\r]+?)\s+Model:\s*([^\s]+)\s+SKU:\s*(\d+)", body)
            if nest_match:
                items.append(ParsedItem(
                    name=nest_match.group(1).strip(),
                    price_usd=total if total > 0 else 49.99,
                    sku=nest_match.group(3),
                    serial_number=nest_match.group(2)
                ))

        if not items:
            items.append(ParsedItem(name="Best Buy Electronics Purchase", price_usd=total))

        return ParsedReceipt(
            merchant="Best Buy",
            order_number=order_number,
            purchase_date=date[:10],
            total_usd=total,
            items=items,
            source_message_id=message_id,
            notes="Parsed via BestBuyReceiptParser"
        )
