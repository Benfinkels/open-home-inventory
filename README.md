# Open Home Inventory

A local-first tool that scans room photos, slices lossless optical crops to read serial numbers, and reconciles them against digital receipts to build an audit-proof home insurance inventory.

<div align="center">

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-brightgreen.svg)
![Local First](https://img.shields.io/badge/architecture-local--first%20%26%20offline-success.svg)
![Tests](https://img.shields.io/badge/tests-11%20passing-brightgreen.svg)

[Why I Built This](#why-i-built-this) • [How It Works](#how-it-works) • [Quickstart](#quickstart) • [The Optical Crop Problem](#the-optical-crop-problem) • [Receipt Parsers](#receipt-parsers) • [Dashboard](#local-dashboard) • [Extending](#adding-new-receipt-parsers)

</div>

---

## Why I Built This

If you've ever dealt with homeowners or renters insurance after a burst pipe, theft, or fire, you know the drill: insurance adjusters don't just take your word for it. When you claim high-value electronics, camera gear, appliances, or a custom road bike, they demand proof:
1. Proof you owned the item (photos).
2. The specific make, model, and serial number (not just "a computer monitor").
3. Proof of purchase (invoices, receipts, dates, and what you actually paid).

Most people either don't have an inventory at all, or they give up after spending an afternoon typing serial numbers into a spreadsheet.

I built this project to automate the entire process locally on my own hardware:
- Take wide-angle room photos (desks, workshops, living rooms).
- Detect items and automatically slice out **1:1 uncompressed optical crops** directly from the native camera resolution so stamped serial plates and model badges are actually legible.
- Parse digital e-receipts from my inbox (Amazon, Micro Center, Best Buy, specialty bike shops, direct OEMs).
- Cross-reference visual items against receipt line items using exact serial matching and fuzzy string similarity.
- Export insurance-compliant CSV schedules and executive appraisal summaries, or view the whole portfolio in a local dark-mode dashboard.

---

## How It Works

```
   Raw Room Photos (Native 4K / Phone Camera)
                      │
                      ▼
 ┌─────────────────────────────────────────────────────────────┐
 │ 1. Scene Detection (Local Ollama / vLLM / OpenAI API)       │
 │    Finds all items in the room and gets bounding boxes      │
 └────────────────────────────┬────────────────────────────────┘
                              │
                              ▼
 ┌─────────────────────────────────────────────────────────────┐
 │ 2. Lossless Optical Patch Cropper                           │
 │    Pulls 1:1 uncompressed pixel crops from the original     │
 │    photo so badges, barcodes, and serial plates stay sharp  │
 └────────────────────────────┬────────────────────────────────┘
                              │
                              ▼
 ┌─────────────────────────────────────────────────────────────┐
 │ 3. Receipt Parsing (Local Scrapers & File Ingestion)        │
 │    Parses line items, order numbers, prices & serials from: │
 │    • Amazon        • Micro Center      • Best Buy           │
 │    • Apple/NVIDIA  • Local Bike Shops  • Generic Fallback   │
 └────────────────────────────┬────────────────────────────────┘
                              │
                              ▼
 ┌─────────────────────────────────────────────────────────────┐
 │ 4. Cross-Modal Reconciliation Engine                        │
 │    • Tier 1: 100% exact match on hardware serial number     │
 │    • Tier 2: Fuzzy Jaccard token matching on brand & model  │
 └────────────────────────────┬────────────────────────────────┘
                              │
                              ▼
 ┌─────────────────────────────────────────────────────────────┐
 │ 5. Local Storage & Insurance Exports                        │
 │    • SQLite database (`data/inventory.db`)                  │
 │    • Insurance Schedule CSV (`data/exports/*.csv`)          │
 │    • Markdown Appraisal Report (`data/exports/*.md`)        │
 │    • Fast local web UI (`http://127.0.0.1:8000`)            │
 └─────────────────────────────────────────────────────────────┘
```

---

## Quickstart

### 1. Setup

```bash
git clone https://github.com/Benfinkels/open-home-inventory.git
cd open-home-inventory

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

### 2. Run the Demo

You can test the entire pipeline immediately without needing a camera or your personal email:

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

### 3. Open the Local Dashboard

```bash
inventory-ai serve --host 127.0.0.1 --port 8000
```
Then visit `http://127.0.0.1:8000` in your browser.

---

## The Optical Crop Problem

When you send a 24-megapixel photo of your desk or workshop to a standard vision LLM, the model resizes the image down to 512×512 or 768×768 pixels to stay within token limits. 

At that resolution, you can tell there is a GPU or a monitor on the desk, but **the serial number sticker and regulatory model text become a blurry, unreadable smudge.**

Here is how this pipeline fixes that:
1. **Pass 1 (Overview):** The vision model scans the whole photo once to detect items and return normalized coordinates `[ymin, xmin, ymax, xmax]`.
2. **Pass 2 (Lossless Extraction):** [`OpticalCropEngine`](src/vision/optical_crop.py) re-opens the original high-res image and cuts out the exact bounding box with custom padding at full native resolution.
3. **Pass 3 (Badge Inspection):** A secondary OCR pass inspects only that cropped patch, where the letters, numbers, and barcodes are still pin-sharp.

---

## Receipt Parsers

Receipt handling is completely decoupled via [`BaseReceiptParser`](src/receipts/parsers/base.py).

Out of the box, it includes parsers for:
- **Amazon:** Parses order numbers (`XXX-XXXXXXX-XXXXXXX`), item names, and line prices.
- **Micro Center:** Parses in-store register e-receipts and pickup slips, extracting product SKUs and physical serial numbers scanned at the register.
- **Best Buy:** Parses online orders and store pickups.
- **Direct OEMs:** Apple Store invoices and NVIDIA Store orders.
- **Specialty Bike Shops:** Work orders, Lightspeed invoices, and bike shop slips (Wheelworks, Specialized, REI, Zwift).
- **Generic Fallback:** Catches order numbers and totals from any merchant with standard receipt wording.

---

## Adding New Receipt Parsers

Adding support for any other store (B&H, Newegg, Target, Home Depot, etc.) takes less than 20 lines of Python.

1. Create `src/receipts/parsers/my_store.py`:
```python
import re
from typing import Optional
from .base import BaseReceiptParser, ParsedReceipt, ParsedItem

class MyStoreParser(BaseReceiptParser):
    def can_parse(self, sender: str, subject: str, body: str) -> bool:
        return "mystore.com" in sender.lower() or "my store" in body.lower()

    def parse(self, message_id: str, date: str, sender: str, subject: str, body: str) -> Optional[ParsedReceipt]:
        order_m = re.search(r"Order Number:\s*([A-Z0-9\-]+)", body)
        total_m = re.search(r"Total:\s*\$?([0-9,]+\.[0-9]{2})", body)
        
        return ParsedReceipt(
            merchant="My Store",
            order_number=order_m.group(1) if order_m else f"MS-{message_id[:8]}",
            purchase_date=date[:10],
            total_usd=float(total_m.group(1).replace(",", "")) if total_m else 0.0,
            items=[ParsedItem(name="Purchased Item", price_usd=100.0)],
            source_message_id=message_id
        )
```

2. Register it in `src/receipts/parser_registry.py`:
```python
from .parsers.my_store import MyStoreParser
self.parsers.insert(0, MyStoreParser())
```

---

## Local Dashboard

The dashboard (`web/`) is built with FastAPI and clean Vanilla CSS (no React or Node build steps needed).

It features:
- **KPI Cards:** Live calculation of total replacement cost, original purchase basis, receipt-verified percentage, and an audit-readiness score.
- **Instant Search & Filtering:** Filter by room, category, verification status, or search by serial number.
- **Item Detail Modal:** Inspects original bounding boxes, optical crop paths, and matched receipt order references.
- **One-Click Export:** Download insurance CSV schedules or Markdown appraisal reports directly from the UI.

---

## Privacy & Local-First Design

Everything in this repo is designed to run entirely on your own machine. Your receipts, order histories, equipment serials, and home photos never get uploaded to third-party cloud servers or shared with anyone.

- **Offline / Local Models:** Can run completely offline or against local model endpoints (e.g., Ollama running `minicpm-v` or `qwen2.5-vl`).
- **Clean Test Fixtures:** The included demo fixtures (`examples/`) use synthetic test data (mock Cambridge, MA addresses, synthetic order numbers, and dummy serials) so you can test and develop without exposing personal info.
- **Safe Git Configuration:** `.gitignore` is set up out of the box to prevent local databases (`*.db`), images, crops, and `.env` files from ever being committed to git.

---

## CLI Reference

| Command | What it does | Example |
| :--- | :--- | :--- |
| `demo` | Generates a complete synthetic test portfolio | `inventory-ai demo` |
| `scan-photos` | Scans room photos and extracts items & optical crops | `inventory-ai scan-photos ./photos` |
| `scrape-receipts` | Scrapes receipts from email or directory | `inventory-ai scrape-receipts` |
| `reconcile` | Runs matching between photos and receipts | `inventory-ai reconcile` |
| `serve` | Launches the local web dashboard | `inventory-ai serve --port 8000` |

---

## Running Tests

```bash
pytest tests/ -v
```

All 11 unit tests run in under 0.25 seconds:
- `test_vision.py`: Bounding boxes and optical crop extraction.
- `test_receipt_parsers.py`: Merchant parsers across Amazon, Micro Center, and bike shops.
- `test_matcher.py`: Fuzzy sequence matching and exact serial number linking.
- `test_storage.py`: SQLite persistence and CSV / Markdown generation.

---

## License

MIT License. See [LICENSE](LICENSE) for details.
