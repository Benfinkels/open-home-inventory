import re
from typing import Optional, List
from .base import BaseReceiptParser, ParsedReceipt, ParsedItem


class AmazonReceiptParser(BaseReceiptParser):
    """Parses Amazon order confirmations and delivery notices."""

    def can_parse(self, sender: str, subject: str, body: str) -> bool:
        s = sender.lower()
        sub = subject.lower()
        return "amazon.com" in s or "amazon order" in sub or "ordered:" in sub

    def parse(self, message_id: str, date: str, sender: str, subject: str, body: str) -> Optional[ParsedReceipt]:
        order_match = re.search(r"([0-9]{3}-[0-9]{7}-[0-9]{7})", body)
        if not order_match:
            order_match = re.search(r"([0-9]{3}-[0-9]{7}-[0-9]{7})", subject)
        order_number = order_match.group(1) if order_match else f"AMZN-{message_id[:8]}"

        total = 0.0
        total_match = re.search(r"(?:Order Total|Grand Total|Total):\s*\$?([0-9,]+\.[0-9]{2})", body, re.IGNORECASE)
        if total_match:
            total = float(total_match.group(1).replace(",", ""))

        items: List[ParsedItem] = []
        # Pattern 1: Ordered: "Item Name..."
        subj_match = re.search(r"Ordered:\s*\"([^\"]+)\"", subject)
        if subj_match:
            item_name = subj_match.group(1)
            items.append(ParsedItem(name=item_name, price_usd=total if total > 0 else 0.0))

        # Pattern 2: "1 of: Item Name..."
        of_matches = re.findall(r"(\d+)\s+of:\s*([^\n\r]+)", body)
        if of_matches:
            for qty, item_name in of_matches:
                clean_name = item_name.strip()
                # Check for item price if available
                item_price = total
                pr_match = re.search(r"Price:\s*\$?([0-9,]+\.[0-9]{2})", body)
                if pr_match:
                    item_price = float(pr_match.group(1).replace(",", ""))
                items.append(ParsedItem(
                    name=clean_name,
                    price_usd=item_price,
                    quantity=int(qty)
                ))

        # Pattern 3: Bullet item lines in email body
        if not items:
            body_items = re.findall(r"\*\s*([^\n\r]+?)(?:\s+Quantity:\s*(\d+))?\s+([0-9,]+\.[0-9]{2})\s*USD", body)
            for item_name, qty, price_str in body_items:
                clean_name = item_name.strip()
                p_val = float(price_str.replace(",", ""))
                items.append(ParsedItem(
                    name=clean_name,
                    price_usd=p_val,
                    quantity=int(qty) if qty else 1
                ))

        if not items:
            items.append(ParsedItem(name=subject.replace("Your Amazon.com order of", "").strip(), price_usd=total))

        return ParsedReceipt(
            merchant="Amazon.com",
            order_number=order_number,
            purchase_date=date[:10],
            total_usd=total,
            items=items,
            source_message_id=message_id,
            notes="Parsed via AmazonReceiptParser"
        )
