import re
from typing import Optional, List
from .base import BaseReceiptParser, ParsedReceipt, ParsedItem


class BikeShopReceiptParser(BaseReceiptParser):
    """
    Parses specialty cycling shops (Wheelworks / Lightspeed), direct bike OEMs (Specialized),
    fitness trainers (Zwift), and outdoor retailers (REI).
    """

    def can_parse(self, sender: str, subject: str, body: str) -> bool:
        s = sender.lower()
        b = body.lower()
        return any(k in s for k in ["wheelworks", "lightspeedhq", "specialized.com", "zwift.com", "rei.com"]) or \
               any(k in b for k in ["wheelworks", "work order item:", "specialized bicycle components", "zwift hub", "rei co-op"])

    def parse(self, message_id: str, date: str, sender: str, subject: str, body: str) -> Optional[ParsedReceipt]:
        s = sender.lower()
        b = body.lower()

        # 1. Local Bike Shop / Wheelworks / Lightspeed
        if "wheelworks" in s or "wheelworks" in b or "lightspeed" in s:
            order_m = re.search(r"(?:Invoice Number|Work Order.*?#?)\s*[:#]?\s*([A-Za-z0-9\-]+)", body)
            order_num = order_m.group(1) if order_m else f"WW-{message_id[:8]}"

            items = []
            # Check for Item: ... line
            direct_item_m = re.search(r"Item:\s*([^\n\r]+)", body)
            if direct_item_m:
                item_name = direct_item_m.group(1).strip()
                sn_m = re.search(r"(?:Frame Serial Number|Serial Number|S/N):\s*([A-Za-z0-9\-]+)", body)
                sn = sn_m.group(1).strip() if sn_m else None
                pr_m = re.search(r"(?:Unit Price|Price):\s*\$?([0-9,]+\.[0-9]{2})", body)
                price = float(pr_m.group(1).replace(",", "")) if pr_m else 0.0
                items.append(ParsedItem(name=item_name, price_usd=price, serial_number=sn))

            # Extract Work Order Item: Bianchi 21 Impulso ... / celeste / wbk7027375
            wo_item = re.search(r"Work Order Item:\s*([^\n\r]+)", body)
            if wo_item:
                raw_item = wo_item.group(1).strip()
                parts = [p.strip() for p in raw_item.split("/")]
                item_name = parts[0]
                serial = parts[-1] if len(parts) > 1 and re.match(r"^[A-Za-z0-9\-]+$", parts[-1]) else None
                items.append(ParsedItem(name=item_name, price_usd=0.0, serial_number=serial))

            # Extract line items if no direct item
            if not items:
                line_matches = re.findall(r"([A-Za-z0-9\s,\-\"\']+?)\s+(\d+)\s+\$?([0-9,]+\.[0-9]{2})", body)
                for desc, qty, price_str in line_matches:
                    clean = re.sub(r"\s+", " ", desc).strip()
                    if not any(skip in clean for skip in ["Subtotal", "Labor", "Parts", "Tax", "Total", "Discount", "Employee", "Invoice Number", "Customer"]):
                        items.append(ParsedItem(name=clean, price_usd=float(price_str.replace(",", "")), quantity=int(qty)))

            total_m = re.search(r"(?:Total Paid|Total)\s*:?\s*\$?([0-9,]+\.[0-9]{2})", body)
            total = float(total_m.group(1).replace(",", "")) if total_m else 0.0

            return ParsedReceipt(
                merchant="Wheelworks Cycling",
                order_number=order_num,
                purchase_date=date[:10],
                total_usd=total,
                items=items,
                source_message_id=message_id,
                notes="Specialty cycling retail receipt / invoice"
            )

        # 2. Specialized Direct Store
        elif "specialized" in s or "specialized" in b:
            order_m = re.search(r"Order Number\s*([A-Za-z0-9\-]+)", body)
            order_num = order_m.group(1) if order_m else f"SPEC-{message_id[:8]}"
            total_m = re.search(r"Order Total\s+\$?([0-9,]+\.[0-9]{2})", body)
            total = float(total_m.group(1).replace(",", "")) if total_m else 0.0

            items = []
            # S-Works Evade 3 Color: Vivid Red Size: M Qty: 1 Price: $180.00
            item_m = re.search(r"([A-Za-z0-9\s\-]+?)\s+Color:.*?Price:\s*\$?([0-9,]+\.[0-9]{2})", body)
            if item_m:
                items.append(ParsedItem(name=item_m.group(1).strip(), price_usd=float(item_m.group(2).replace(",", ""))))
            else:
                items.append(ParsedItem(name="Specialized Bicycle Equipment", price_usd=total))

            return ParsedReceipt(
                merchant="Specialized",
                order_number=order_num,
                purchase_date=date[:10],
                total_usd=total,
                items=items,
                source_message_id=message_id,
                notes="Specialized Online Order"
            )

        # 3. Zwift Store
        elif "zwift" in s or "zwift" in b:
            order_m = re.search(r"(ZWUS[0-9]+)", body)
            order_num = order_m.group(1) if order_m else f"ZW-{message_id[:8]}"
            total_m = re.search(r"Total\s+\$?([0-9,]+\.[0-9]{2})\s*USD", body)
            total = float(total_m.group(1).replace(",", "")) if total_m else 0.0

            items = []
            if "zwift hub" in b:
                items.append(ParsedItem(name="Zwift Hub Smart Interactive Direct Drive Trainer", price_usd=499.00))

            return ParsedReceipt(
                merchant="Zwift",
                order_number=order_num,
                purchase_date=date[:10],
                total_usd=total,
                items=items,
                source_message_id=message_id,
                notes="Zwift Order"
            )

        # 4. REI Co-op eReceipt
        elif "rei" in s or "rei" in b:
            trans_m = re.search(r"Transaction #:\s*(\d+)", body)
            order_num = trans_m.group(1) if trans_m else f"REI-{message_id[:8]}"
            total_m = re.search(r"Total\s+\$?([0-9,]+\.[0-9]{2})", body)
            total = float(total_m.group(1).replace(",", "")) if total_m else 0.0

            items = []
            # Lines like: Kanken Sling Item #216845 Qty: 1 $48.93 $48.93
            rei_items = re.findall(r"([A-Za-z0-9\s#\-\'\.]+?)\s+Item #(\d+)\s+Qty:\s*(\d+)\s+\$?([0-9,]+\.[0-9]{2})", body)
            for desc, item_no, qty, price_str in rei_items:
                items.append(ParsedItem(
                    name=desc.strip(),
                    price_usd=float(price_str.replace(",", "")),
                    sku=item_no,
                    quantity=int(qty)
                ))

            return ParsedReceipt(
                merchant="REI Co-op",
                order_number=order_num,
                purchase_date=date[:10],
                total_usd=total,
                items=items,
                source_message_id=message_id,
                notes="REI in-store / online purchase"
            )

        return None
