# Open Home Inventory (OHI)

<div align="center">

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-brightgreen.svg)
![Privacy](https://img.shields.io/badge/privacy-100%25%20Local%20%26%20Zero--PII-purple.svg)
![Architecture](https://img.shields.io/badge/architecture-Multi--Modal%20Vision%20%2B%20Receipt%20Reconciliation-orange.svg)
![Tests](https://img.shields.io/badge/tests-11%20passing-success.svg)

**Autonomous, Privacy-First Home Property Cataloging, Optical Asset Extraction & Cross-Modal Insurance Proof-of-Purchase Ledger**

[Quickstart](#quickstart) • [Architecture](#architecture) • [Vision & Optical Cropping](#vision--optical-crop-engine) • [Receipt Parsers](#receipt-scraping--parsers) • [Web Dashboard](#modern-web-dashboard) • [Extending](#how-to-extend)

</div>

---

## The Problem: Why Traditional Home Inventories Fail

Most homeowners realize they need an itemized home inventory only *after* a catastrophic loss (fire, pipe burst, burglary). When filing high-value property claims, insurance underwriters routinely reject claims or drastically depreciate payouts due to:
1. **The "Messy Photo" Problem:** A photo of a desk or garage contains dozens of items, but generic cloud vision models downsample 4K/8K images into tiny thumbnails (e.g. 512x512), completely destroying legible serial numbers, model stamped badges, and brand logos.
2. **Missing Proof of Purchase:** Having a photograph of a $2,000 GPU or a $6,000 bicycle is insufficient; insurers demand proof of payment, merchant invoices, and serial numbers.
3. **Manual Burden:** Manually typing hundreds of serial numbers and digging through 10 years of email receipts takes weeks and is rarely kept up to date.

**Open Home Inventory solves this end-to-end** using local multimodal AI, lossless optical patch extraction, and automated e-receipt parsing to build an audit-proof, insurance-ready inventory schedule in minutes.

---

## Architecture Overview

```
                      ┌─────────────────────────────────────────┐
                      │    Raw Room & Asset Photographs        │
                      │       (Native 4K / 8K Camera)           │
                      └────────────────────┬────────────────────┘
                                           │
                                           ▼
┌───────────────────────────────────────────────────────────────────────────────────┐
│                          STAGE 1: MULTIMODAL DETECTION                            │
│  Local Vision Model (Ollama MiniCPM-V / Qwen2.5-VL / vLLM / OpenAI API)           │
│  Isolates multiple items, classifies categories, and outputs bounding coordinates │
└──────────────────────────────────────────┬────────────────────────────────────────┘
                                           │
                                           ▼
┌───────────────────────────────────────────────────────────────────────────────────┐
│                      STAGE 2: LOSSLESS OPTICAL PATCH CROPPING                     │
│  OpticalCropEngine extracts 1:1 uncompressed pixel patches around each bounding   │
│  box directly from native originals, bypassing downsampling blur                  │
└──────────────────────────────────────────┬────────────────────────────────────────┘
                                           │
                                           ▼
┌───────────────────────────────────────────────────────────────────────────────────┐
│                         STAGE 3: BADGE & SERIAL OCR                               │
│  Focused secondary inspection detects stamped serial plates, regulatory badges,   │
│  and manufacturer part numbers from the lossless optical crops                     │
└──────────────────────────────────────────┬────────────────────────────────────────┘
                                           │
                                           ▼
       ┌─────────────────────────────────────────────────────────────┐
       │             STAGE 4: RECEIPT INGESTION & PARSING            │
       │  Scrapes & parses e-receipts from direct export or emails:   │
       │  • Amazon.com        • Micro Center        • Best Buy       │
       │  • Direct OEM (Apple/NVIDIA)               • Bike Pro Shops │
       └─────────────────────────────┬───────────────────────────────┘
                                     │
                                     ▼
┌───────────────────────────────────────────────────────────────────────────────────┐
│                  STAGE 5: CROSS-MODAL RECONCILIATION ENGINE                       │
│  Two-Tier Matching Algorithm:                                                     │
│  1. Exact Serial Linker: 100% confidence match between badge OCR & receipt serial │
│  2. Token Set & Sequence Matcher: Fuzzy similarity across brand, model, and specs │
└────────────────────────────────────┬──────────────────────────────────────────────┘
                                     │
                                     ▼
┌───────────────────────────────────────────────────────────────────────────────────┐
│                        STAGE 6: AUDIT PERSISTENCE & EXPORTS                       │
│  • Local SQLite Database (`data/inventory.db`)                                    │
│  • Insurance-Standard CSV Schedule (`data/exports/inventory_schedule.csv`)        │
│  • Executive Appraisal Markdown Report (`data/exports/appraisal_report.md`)       │
│  • Interactive Dark-Mode Web Dashboard (`inventory-ai serve` on :8000)            │
└───────────────────────────────────────────────────────────────────────────────────┘
```

---

## Key Features

- **100% Local & Zero-PII by Design:** Runs entirely on your local machine. No images, personal receipts, home addresses, or financial data are sent to external servers.
- **Hardware-Decoupled Execution:** Supports local Ollama endpoints (e.g. `minicpm-v`, `qwen2.5-vl`), vLLM, or offline synthetic mock mode. You can game on your GPU while running the entire inventory suite on CPU/RAM or local low-power models.
- **Lossless Optical Patch Isolation:** Eliminates resolution loss by extracting pixel-perfect 1:1 patches for badge reading and serial auditing.
- **Modular Merchant Parsers:** Out-of-the-box support for Amazon, Micro Center, Best Buy, Apple, NVIDIA, and specialty cycling retailers (Wheelworks / Lightspeed). Adding a new store takes fewer than 20 lines of code.
- **Audit-Proof Valuation & Depreciation Engine:** Automatically calculates total replacement valuation vs. purchase price basis, computes verified purchase percentages, and generates claim-ready insurance reports.
- **Modern Glassmorphism Web UI:** Clean, responsive, dark-mode dashboard featuring KPI summary cards, instant search, category/room filtering, and detailed asset inspection modals.

---

## Quickstart

### 1. Installation

```bash
# Clone the repository
git clone https://github.com/Benfinkels/open-home-inventory.git
cd open-home-inventory

# Create virtual environment and install dependencies
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

### 2. Run the Synthetic Demo in 5 Seconds

Generate a complete demo portfolio with verified receipts, database records, and exported insurance schedules:

```bash
inventory-ai demo
```

Output:
```
=================================================================
 Open Home Inventory: Generating Synthetic Demo Data
=================================================================
[*] Loaded 6 synthetic visual items.
[+] Parsed receipt: Micro Center (1 items, total: $1,599.99)
[+] Parsed receipt: Amazon.com (1 items, total: $616.24)
[+] Parsed receipt: Wheelworks Cycling (1 items, total: $6,906.25)
[*] Reconciled 3/6 items with verified purchase records.
[✓] Saved records into database: data/inventory.db
[✓] Exported CSV Schedule: data/exports/inventory_insurance_schedule.csv
[✓] Exported Markdown Audit Report: data/exports/inventory_appraisal_report.md
-----------------------------------------------------------------
 Total Cataloged Items:       6
 Total Replacement Valuation: $14,377.93 USD
 Verified Proof of Purchase:  3 items (67.1%)
 Verified Valuation Backed:   $9,649.98 USD
=================================================================
```

### 3. Launch the Web Dashboard

```bash
inventory-ai serve --host 127.0.0.1 --port 8000
```
Open your browser to `http://127.0.0.1:8000` to explore the dashboard.

---

## CLI Reference

The `inventory-ai` command-line tool provides a unified interface for all inventory workflows:

| Command | Description | Example |
| :--- | :--- | :--- |
| `demo` | Generates synthetic sample inventory & verifies pipeline | `inventory-ai demo` |
| `scan-photos` | Scans room photos and extracts items & optical crops | `inventory-ai scan-photos --input ./photos --room "Office"` |
| `scrape-receipts` | Ingests e-receipts from files or email exports | `inventory-ai scrape-receipts --source ./receipts` |
| `reconcile` | Runs cross-modal matching between vision & receipts | `inventory-ai reconcile --threshold 0.70` |
| `serve` | Launches the interactive FastAPI web dashboard | `inventory-ai serve --port 8000` |

---

## Vision & Optical Crop Engine

When a 24MP phone photo is submitted to a vision LLM, it is typically downscaled to fit within standard token limits (e.g. 512x512 or 768x768). Under this downscaling, stamped serial plates and small labels become an unreadable blur.

The **Optical Crop Subsystem** (`src/vision/optical_crop.py`) solves this:
1. The vision model analyzes the full scene once to generate normalized bounding boxes `[ymin, xmin, ymax, xmax]`.
2. The `OpticalCropEngine` opens the original uncompressed image and slices a lossless 1:1 pixel patch with dynamic padding around each asset.
3. The focused patch is saved to `data/optical_crops/` and passed to a secondary OCR inspection pass (`src/vision/badge_reader.py`) to extract clean alphanumeric serial numbers and model badges.

---

## Receipt Scraping & Parsers

All receipt parsing is strictly decoupled through the `BaseReceiptParser` interface (`src/receipts/parsers/base.py`):

```python
from src.receipts.parsers.base import BaseReceiptParser, ParsedReceipt, ParsedItem

class BaseReceiptParser(ABC):
    @abstractmethod
    def can_parse(self, sender: str, subject: str, body: str) -> bool:
        """Returns True if this parser handles the given message."""
        pass

    @abstractmethod
    def parse(self, message_id: str, date: str, sender: str, subject: str, body: str) -> Optional[ParsedReceipt]:
        """Extracts structured order, items, serials, and pricing."""
        pass
```

### Supported Retailers

- **Amazon.com:** Ingests order confirmations, extracting order IDs (`XXX-XXXXXXX-XXXXXXX`), item line names, and item prices.
- **Micro Center:** Ingests in-store e-receipts and pickup confirmations, extracting product SKUs and physical serial numbers recorded at the register.
- **Best Buy:** Extracts online orders and in-store pickup line items.
- **Direct OEMs:** Supports Apple Store (`W...` order numbers) and NVIDIA Store hardware purchases.
- **Specialty Bike Shops:** Ingests local bike shop work orders, Lightspeed invoices, frame serial numbers, and specialty retailer invoices (Wheelworks, Specialized, REI, Zwift).
- **Generic Fallback:** Gracefully captures receipts from any merchant with currency figures and order references.

---

## How to Extend: Adding a New Merchant Parser

Adding a custom store (e.g. B&H Photo, Target, Walmart, or Home Depot) takes less than 20 lines of code:

1. Create a new file `src/receipts/parsers/my_retailer.py`:
```python
import re
from typing import Optional
from .base import BaseReceiptParser, ParsedReceipt, ParsedItem

class MyRetailerParser(BaseReceiptParser):
    def can_parse(self, sender: str, subject: str, body: str) -> bool:
        return "myretailer.com" in sender.lower() or "my retailer" in body.lower()

    def parse(self, message_id: str, date: str, sender: str, subject: str, body: str) -> Optional[ParsedReceipt]:
        order_match = re.search(r"Order Number:\s*([A-Z0-9\-]+)", body)
        total_match = re.search(r"Total:\s*\$?([0-9,]+\.[0-9]{2})", body)
        
        return ParsedReceipt(
            merchant="My Retailer",
            order_number=order_match.group(1) if order_match else f"MR-{message_id[:8]}",
            purchase_date=date[:10],
            total_usd=float(total_match.group(1).replace(",", "")) if total_match else 0.0,
            items=[ParsedItem(name="Purchased Item", price_usd=100.0)],
            source_message_id=message_id
        )
```
2. Register it in `src/receipts/parser_registry.py`:
```python
from .parsers.my_retailer import MyRetailerParser

self.parsers.insert(0, MyRetailerParser())
```

---

## Modern Web Dashboard

The local web dashboard (`web/`) is built with FastAPI, Jinja2, and Vanilla CSS (no heavy external npm toolchains required).

### Key Views:
- **KPI Metrics:** Real-time calculation of total replacement cost, original purchase basis, receipt-verified ratio, and insurance audit readiness score (0–100).
- **Search & Filters:** Instant live search across brand, model, serial number, room, and category.
- **Audit Table:** Visual status badges showing verified proof-of-purchase vs. visual estimates.
- **Item Inspection Modal:** Slices and displays bounding box coordinates, optical crop archives, and matched merchant order references.
- **One-Click Export:** Download insurance schedules (`.csv`) and executive appraisal reports (`.md`) directly from the browser.

---

## Zero-PII & Privacy Guarantee

When sharing, adapting, or publishing this repository:
- **No Private Data:** All sample files (`examples/`) use synthetic serial numbers, mock prices, and fictitious addresses (`Cambridge, MA`).
- **No Credentials in Git:** The default `.gitignore` excludes `.env`, `*.db`, `*.jpg`, `*.png`, and email credentials.
- **Deterministic CI:** Automated GitHub Actions run across Python 3.10, 3.11, and 3.12 without touching external networks.

---

## Running the Unit Tests

```bash
pytest tests/ -v
```

All 11 tests validate the complete end-to-end flow:
- `test_vision.py`: BoundingBox normalization and optical patch extraction.
- `test_receipt_parsers.py`: Merchant regex extraction across Amazon, Micro Center, and Bike Shops.
- `test_matcher.py`: Sequence matcher fuzzy similarity and exact serial linking.
- `test_storage.py`: SQLite persistence and CSV/Markdown generation.

---

## License

Distributed under the **MIT License**. See [`LICENSE`](LICENSE) for more information.
