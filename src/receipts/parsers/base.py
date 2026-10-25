from abc import ABC, abstractmethod
from typing import List, Optional
from pydantic import BaseModel


class ParsedItem(BaseModel):
    name: str
    price_usd: float
    sku: Optional[str] = None
    serial_number: Optional[str] = None
    quantity: int = 1
    description: Optional[str] = None


class ParsedReceipt(BaseModel):
    merchant: str
    order_number: str
    purchase_date: str
    total_usd: float
    items: List[ParsedItem] = []
    tracking_number: Optional[str] = None
    source_message_id: Optional[str] = None
    notes: Optional[str] = None


class BaseReceiptParser(ABC):
    """Abstract interface for merchant-specific order & receipt extractors."""

    @abstractmethod
    def can_parse(self, sender: str, subject: str, body: str) -> bool:
        """Determines if this parser handles the given email message."""
        pass

    @abstractmethod
    def parse(self, message_id: str, date: str, sender: str, subject: str, body: str) -> Optional[ParsedReceipt]:
        """Extracts structured order, items, serials, and pricing from raw email."""
        pass
