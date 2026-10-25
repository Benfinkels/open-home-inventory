import re
from typing import Optional, List
from .base import BaseReceiptParser, ParsedReceipt, ParsedItem


class DirectOEMReceiptParser(BaseReceiptParser):
    """
    Parses direct manufacturer store confirmations (NVIDIA, Samsung, Google Store, Apple).
    """

    def can_parse(self, sender: str, subject: str, body: str) -> bool:
        s = sender.lower()
        b = body.lower()
        return any(k in s for k in ["nvidia.com", "samsung.com", "google.com", "apple.com"]) or \
               any(k in b for k in ["nvidia geforce", "samsung electronics", "google store order", "apple order"])

    def parse(self, message_id: str, date: str, sender: str, subject: str, body: str) -> Optional[ParsedReceipt]:
        s = sender.lower()
        b = body.lower()

        # 1. NVIDIA Online Store
        if "nvidia" in s or "nvidia" in b:
            order_m = re.search(r"Order Number:\s*([0-9]+)", body)
            order_num = order_m.group(1) if order_m else f"NV-{message_id[:8]}"
            total_m = re.search(r"Total\s*:\s*\$?([0-9,]+\.[0-9]{2})", body)
            total = float(total_m.group(1).replace(",", "")) if total_m else 0.0

            item_m = re.search(r"(NVIDIA GEFORCE [A-Z0-9\s]+?)\s+Physical Shipment\s+Model Number:([A-Za-z0-9\-]+)\s+\$?([0-9,]+\.[0-9]{2})", body)
            items = []
            if item_m:
                items.append(ParsedItem(
                    name=item_m.group(1).strip(),
                    price_usd=float(item_m.group(3).replace(",", "")),
                    serial_number=item_m.group(2).strip()
                ))
            else:
                items.append(ParsedItem(name="NVIDIA GPU Hardware", price_usd=total))

            track_m = re.search(r"Tracking Number:\s*([0-9]+)", body)
            tracking = track_m.group(1) if track_m else None

            return ParsedReceipt(
                merchant="NVIDIA Online Store",
                order_number=order_num,
                purchase_date=date[:10],
                total_usd=total,
                items=items,
                tracking_number=tracking,
                source_message_id=message_id,
                notes="Direct OEM purchase"
            )

        # 2. Samsung Online Store
        elif "samsung" in s or "samsung.com" in b:
            order_m = re.search(r"(US[0-9]+)", body)
            order_num = order_m.group(1) if order_m else f"SAMS-{message_id[:8]}"

            items = []
            deliv_section = re.search(r"DELIVERED ITEM\(S\)\s+(.*?)(?:SHIPPING DETAILS|$)", body, re.DOTALL)
            if deliv_section:
                lines = [l.strip() for l in deliv_section.group(1).split("\n") if l.strip()]
                for l in lines:
                    if len(l) > 5 and not any(skip in l for skip in ["Never miss", "Shop Online"]):
                        items.append(ParsedItem(name=l, price_usd=0.0))

            return ParsedReceipt(
                merchant="Samsung Electronics",
                order_number=order_num,
                purchase_date=date[:10],
                total_usd=0.0,
                items=items,
                source_message_id=message_id,
                notes="Samsung Store Delivery"
            )

        # 3. Google Store
        elif "googlestore" in s or "google store" in b:
            order_m = re.search(r"(GS\.[0-9\-]+)", body)
            order_num = order_m.group(1) if order_m else f"GS-{message_id[:8]}"
            total_m = re.search(r"Total\s+\$?([0-9,]+\.[0-9]{2})", body)
            total = float(total_m.group(1).replace(",", "")) if total_m else 0.0

            items = []
            # Line item: Pixel Buds Pro 2... $70.00
            item_m = re.findall(r"([A-Za-z0-9\s,]+)\s+\$([0-9,]+\.[0-9]{2})\s+Quantity:\s*(\d+)", body)
            for name, price_str, qty in item_m:
                clean_name = name.strip()
                if not any(skip in clean_name for skip in ["Subtotal", "Shipping", "Estimated tax", "Total"]):
                    items.append(ParsedItem(
                        name=clean_name,
                        price_usd=float(price_str.replace(",", "")),
                        quantity=int(qty)
                    ))

            return ParsedReceipt(
                merchant="Google Store",
                order_number=order_num,
                purchase_date=date[:10],
                total_usd=total,
                items=items,
                source_message_id=message_id,
                notes="Google Store Order"
            )

        return None
