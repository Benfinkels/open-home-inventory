import base64
import json
import logging
import os
import subprocess
from typing import List, Optional, Dict, Any
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ScrapedMessage(BaseModel):
    message_id: str
    date: str
    sender: str
    subject: str
    body: str


class ReceiptScraper:
    """
    Scrapes order confirmations and e-receipts across email providers.
    Supports gog CLI (Gmail), standard IMAP, file exports, and mock mode.
    """

    def __init__(
        self,
        provider: str = "mock",
        account_email: Optional[str] = None,
        keyring_password: Optional[str] = None
    ):
        self.provider = provider.lower()
        self.account_email = account_email or os.getenv("RECEIPT_ACCOUNT_EMAIL", "user@example.com")
        self.keyring_password = keyring_password or os.getenv("GOG_KEYRING_PASSWORD", "")

    def fetch_receipts(self, query: str = "receipt OR order OR invoice", max_results: int = 20) -> List[ScrapedMessage]:
        """Fetches raw receipt candidate messages according to configured provider."""
        if self.provider == "mock":
            return self._fetch_mock_receipts()
        elif self.provider == "gog":
            return self._fetch_gog_receipts(query, max_results)
        elif self.provider == "file":
            return self._fetch_file_receipts()
        else:
            raise ValueError(f"Unsupported receipt scraping provider: {self.provider}")

    def _fetch_gog_receipts(self, query: str, max_results: int) -> List[ScrapedMessage]:
        env = os.environ.copy()
        if self.keyring_password:
            env["GOG_KEYRING_PASSWORD"] = self.keyring_password

        gog_bin = os.getenv("GOG_BIN", "/home/linuxbrew/.linuxbrew/bin/gog")
        cmd = [gog_bin, "gmail", "search", query, f"--max={max_results}", "-j"]

        try:
            res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)
            if res.returncode != 0:
                logger.error(f"gog search failed: {res.stderr}")
                return []

            data = json.loads(res.stdout)
            threads = data.get("threads", [])
            messages: List[ScrapedMessage] = []

            for t in threads:
                tid = t.get("id")
                # Fetch message body
                get_cmd = [gog_bin, "gmail", "get", tid, "-j"]
                get_res = subprocess.run(get_cmd, env=env, capture_output=True, text=True, timeout=15)
                if get_res.returncode == 0:
                    msg_data = json.loads(get_res.stdout)
                    msg_obj = msg_data.get("message", msg_data)
                    body_text = self._extract_text_from_payload(msg_obj.get("payload", {}))
                    messages.append(ScrapedMessage(
                        message_id=tid,
                        date=t.get("date", "2026-01-01"),
                        sender=t.get("from", "Unknown"),
                        subject=t.get("subject", ""),
                        body=body_text
                    ))
            return messages

        except Exception as e:
            logger.error(f"Error fetching via gog: {e}")
            return []

    @staticmethod
    def _extract_text_from_payload(part: Dict[str, Any]) -> str:
        texts = []
        body = part.get("body", {})
        if "data" in body:
            try:
                raw = base64.urlsafe_b64decode(body["data"] + "==").decode("utf-8", errors="ignore")
                texts.append(raw)
            except Exception:
                pass
        for p in part.get("parts", []):
            texts.append(ReceiptScraper._extract_text_from_payload(p))
        return " ".join(texts)

    def _fetch_file_receipts(self, file_path: str = "./examples/sample_receipt.txt") -> List[ScrapedMessage]:
        if os.path.exists(file_path):
            with open(file_path, "r") as f:
                content = f.read()
            return [
                ScrapedMessage(
                    message_id="FILE-001",
                    date="2025-12-28",
                    sender="sales@microcenter.com",
                    subject="Your receipt from Micro Center",
                    body=content
                )
            ]
        return []

    def _fetch_mock_receipts(self) -> List[ScrapedMessage]:
        """Returns realistic synthetic receipts for offline demo and testing."""
        return [
            ScrapedMessage(
                message_id="MOCK-NV-001",
                date="2023-12-18",
                sender="NVIDIA Store <donotreply@nvidia.com>",
                subject="NVIDIA - Shipping Confirmation (Order #1148118264539)",
                body=(
                    "Order Number: 1148118264539\n"
                    "Order Date: December 18, 2023\n"
                    "NVIDIA GEFORCE RTX 4090 Physical Shipment Model Number:900-1G136-2530-000 $1,599.00 Qty: 1\n"
                    "Tracking Number: 722762985829\n"
                    "Subtotal : $1,599.00  Tax : $99.94  Total : $1,698.94"
                )
            ),
            ScrapedMessage(
                message_id="MOCK-MC-002",
                date="2023-12-28",
                sender="Micro Center <NoReply@microcenter.com>",
                subject="Your receipt from Micro Center: Cambridge",
                body=(
                    "Reference Number >> 121-WP-12808881\n"
                    "Transaction Date >> 12/28/2023\n"
                    "643718 AMD AMD RYZEN 7 7800X3D WOF S/N: 9KW2751S30089 1 349.99 349.99\n"
                    "504936 ASUS ROG STRIX B650E-F GAM WF S/N: R3M0KC322403CWW 1 289.99 289.99\n"
                    "390054 DELL 34\"OLED AW3423DW MONI/G S/N: G8HQ2K3 1 899.99 899.99\n"
                    "600387 FOCUSRITE SCARLETT SOLO 4TH GEN S/N: S1KX5TJ39925C8 1 139.99 139.99\n"
                    "Sale TOTAL >> $3,135.29"
                )
            ),
            ScrapedMessage(
                message_id="MOCK-BBY-003",
                date="2024-07-03",
                sender="Best Buy Notifications <BestBuyInfo@emailinfo.bestbuy.com>",
                subject="Your order BBY01-806947300479 has been picked up",
                body=(
                    "Order number: BBY01-806947300479\n"
                    "Product Details ASUS - ROG Zephyrus G16 16\" OLED 240Hz Gaming Laptop - "
                    "Intel Core Ultra 9 - 16GB LPDDR5X - NVIDIA GeForce RTX 4070 - 1TB SSD - Eclipse Gray $1,999.99\n"
                    "SKU: 6570222 Qty: 1\n"
                    "Subtotal $1,999.99 Sales Tax $125.00 Total $2,124.99"
                )
            ),
            ScrapedMessage(
                message_id="MOCK-BIKE-004",
                date="2025-06-15",
                sender="Belmont Wheelworks <noreply@lightspeedhq.com>",
                subject="Belmont Wheelworks Receipt For Order #110301",
                body=(
                    "Work Order #110301\n"
                    "Work Order Item: Specialized Click and Collect Bike Order / GREEN / S3 / WSBC014373007V\n"
                    "Status: Finished\n"
                    "Notes: BIKE PURCHASED 6/15/25 REAR BRAKE NEEDS BLEED. DO AS WARRANTY\n"
                    "Total $0.00"
                )
            ),
            ScrapedMessage(
                message_id="MOCK-ZWIFT-005",
                date="2022-11-25",
                sender="Zwift <orders@zwift.com>",
                subject="Order #ZWUS56969 confirmed",
                body=(
                    "Order #: ZWUS56969\n"
                    "Zwift Hub Qty: 1 11-Speed $499.00\n"
                    "Subtotal $499.00 Shipping $25.00 Taxes $31.19 Total $555.19 USD"
                )
            )
        ]
