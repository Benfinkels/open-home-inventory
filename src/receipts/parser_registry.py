import logging
from typing import List, Optional
from .parsers.base import BaseReceiptParser, ParsedReceipt, ParsedItem
from .parsers.amazon import AmazonReceiptParser
from .parsers.bestbuy import BestBuyReceiptParser
from .parsers.microcenter import MicroCenterReceiptParser
from .parsers.direct_oem import DirectOEMReceiptParser
from .parsers.bike_shops import BikeShopReceiptParser

logger = logging.getLogger(__name__)


class GenericFallbackParser(BaseReceiptParser):
    """Fallback parser for unmatched purchase confirmations."""

    def can_parse(self, sender: str, subject: str, body: str) -> bool:
        return True

    def parse(self, message_id: str, date: str, sender: str, subject: str, body: str) -> Optional[ParsedReceipt]:
        import re
        total_m = re.search(r"\$([0-9,]+\.[0-9]{2})", body)
        total = float(total_m.group(1).replace(",", "")) if total_m else 0.0

        return ParsedReceipt(
            merchant=sender.split("<")[0].strip() or "Generic Merchant",
            order_number=f"GEN-{message_id[:8]}",
            purchase_date=date[:10],
            total_usd=total,
            items=[ParsedItem(name=subject[:80], price_usd=total)],
            source_message_id=message_id,
            notes="Parsed via GenericFallbackParser"
        )


class ReceiptParserRegistry:
    """Manages merchant-specific parsers and routes messages to the matching extractor."""

    def __init__(self):
        self.parsers: List[BaseReceiptParser] = [
            AmazonReceiptParser(),
            BestBuyReceiptParser(),
            MicroCenterReceiptParser(),
            DirectOEMReceiptParser(),
            BikeShopReceiptParser(),
        ]
        self.fallback = GenericFallbackParser()

    def register(self, parser: BaseReceiptParser):
        self.parsers.insert(0, parser)

    def parse_email(
        self,
        message_id: str,
        date: str,
        sender: str,
        subject: str,
        body: str
    ) -> Optional[ParsedReceipt]:
        """Finds the first parser matching the email and returns structured receipt data."""
        for parser in self.parsers:
            if parser.can_parse(sender, subject, body):
                try:
                    res = parser.parse(message_id, date, sender, subject, body)
                    if res:
                        return res
                except Exception as e:
                    logger.warning(f"Parser {parser.__class__.__name__} failed on {message_id}: {e}")

        # Fallback if receipt keywords are in text
        if any(k in subject.lower() or k in body.lower() for k in ["receipt", "order", "invoice", "payment successful", "total"]):
            return self.fallback.parse(message_id, date, sender, subject, body)

        return None


_default_registry = ReceiptParserRegistry()


def parse_receipt_text(
    text: str,
    sender: str = "",
    subject: str = "",
    date: str = "2024-01-01",
    message_id: str = "msg-direct"
) -> Optional[ParsedReceipt]:
    """Convenience helper to parse raw receipt text using the registry."""
    # Infer sender/subject hints from text if not provided
    inferred_sender = sender
    inferred_subject = subject

    if not inferred_sender:
        lower_text = text.lower()
        if "micro center" in lower_text:
            inferred_sender = "Micro Center <e-receipt@microcenter.com>"
        elif "amazon.com" in lower_text or "amazon order" in lower_text:
            inferred_sender = "Amazon.com <auto-confirm@amazon.com>"
        elif "best buy" in lower_text:
            inferred_sender = "Best Buy <orders@bestbuy.com>"
        elif "wheelworks" in lower_text or "lightspeed" in lower_text:
            inferred_sender = "Wheelworks <orders@wheelworks.com>"
        elif "nvidia" in lower_text:
            inferred_sender = "NVIDIA Online Store <orders@nvidia.com>"
        elif "apple" in lower_text:
            inferred_sender = "Apple Store <order-update@apple.com>"
        else:
            inferred_sender = "Receipt Parser <receipts@retailer.com>"

    if not inferred_subject:
        inferred_subject = "Order Confirmation & Receipt"

    return _default_registry.parse_email(
        message_id=message_id,
        date=date,
        sender=inferred_sender,
        subject=inferred_subject,
        body=text,
    )

