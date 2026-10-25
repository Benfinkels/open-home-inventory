"""
FastAPI Dashboard Server for Open Home Inventory.
Provides REST APIs and a modern local web UI for reviewing visual scans,
receipt matches, and insurance appraisal reports.
"""

import os
from pathlib import Path
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from src.storage.database import InventoryDatabase
from src.storage.csv_exporter import export_inventory_csv
from src.storage.markdown_exporter import export_inventory_markdown
from src.reconciliation.appraisal import PortfolioAuditStats

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
DB_PATH = PROJECT_ROOT / "data" / "inventory.db"

app = FastAPI(
    title="Open Home Inventory",
    description="Local-first, privacy-focused visual & receipt inventory management.",
    version="1.0.0",
)

# Mount static files and templates
STATIC_DIR.mkdir(parents=True, exist_ok=True)
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def get_db() -> InventoryDatabase:
    """Returns database connection, creating directories if needed."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return InventoryDatabase(str(DB_PATH))


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Renders the main inventory management dashboard."""
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/items")
async def list_items(
    category: Optional[str] = None,
    verified_only: bool = False,
    query: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List inventory items with optional category, query, and verified filters."""
    db = get_db()
    items = db.get_all_items()

    if category:
        items = [it for it in items if (it.get("category") or "").lower() == category.lower()]

    if verified_only:
        items = [it for it in items if it.get("receipt_verified")]

    if query:
        q_lower = query.strip().lower()
        filtered = []
        for it in items:
            searchable = f"{it.get('item_name','')} {it.get('brand','')} {it.get('model_or_identifiers','')} {it.get('serial_number','')}".lower()
            if q_lower in searchable:
                filtered.append(it)
        items = filtered

    return items


@app.get("/api/items/{item_id}")
async def get_item(item_id: int) -> Dict[str, Any]:
    """Retrieve details for a single inventory item."""
    db = get_db()
    items = db.get_all_items()
    for it in items:
        if it.get("id") == item_id:
            return it
    raise HTTPException(status_code=404, detail="Item not found")


@app.get("/api/stats")
async def get_stats() -> Dict[str, Any]:
    """Calculate aggregate insurance valuation and audit readiness metrics."""
    db = get_db()
    items = db.get_all_items()

    total_val = sum(it.get("estimated_replacement_value_usd") or 0.0 for it in items)
    orig_cost = sum(it.get("purchase_price_usd") or 0.0 for it in items)
    verified = [it for it in items if it.get("receipt_verified")]
    verified_val = sum(it.get("estimated_replacement_value_usd") or 0.0 for it in verified)
    with_serial = sum(1 for it in items if it.get("serial_number"))
    pct = (verified_val / total_val * 100.0) if total_val > 0 else 0.0

    audit_score = int(min(100, (pct * 0.6) + ((with_serial / len(items) * 100) * 0.4))) if items else 0

    cats = {}
    for it in items:
        c = it.get("category") or "General"
        cats[c] = cats.get(c, 0) + 1

    return {
        "total_items": len(items),
        "total_replacement_cost": round(total_val, 2),
        "total_purchase_cost": round(orig_cost, 2),
        "verified_items": len(verified),
        "verified_percentage": round(pct, 1),
        "verified_valuation_usd": round(verified_val, 2),
        "items_with_serial": with_serial,
        "audit_readiness_score": audit_score,
        "category_breakdown": cats,
    }


@app.get("/api/export/csv")
async def export_csv():
    """Generates and returns an insurance-standard inventory CSV."""
    db = get_db()
    items = db.get_all_items()
    export_dir = PROJECT_ROOT / "data" / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    csv_file = export_dir / "inventory_insurance_schedule.csv"

    export_inventory_csv(items, str(csv_file))
    return FileResponse(
        path=str(csv_file),
        filename="inventory_insurance_schedule.csv",
        media_type="text/csv",
    )


@app.get("/api/export/report")
async def export_report():
    """Generates and returns an executive Markdown appraisal report."""
    db = get_db()
    items = db.get_all_items()
    export_dir = PROJECT_ROOT / "data" / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    report_file = export_dir / "inventory_appraisal_report.md"

    stats_dict = await get_stats()
    cat_summary = {}
    for it in items:
        cat = it.get("category") or "General"
        val = it.get("estimated_replacement_value_usd") or 0.0
        is_v = bool(it.get("receipt_verified"))
        if cat not in cat_summary:
            cat_summary[cat] = {"count": 0, "value": 0.0, "verified_count": 0, "verified_val": 0.0}
        cat_summary[cat]["count"] += 1
        cat_summary[cat]["value"] += val
        if is_v:
            cat_summary[cat]["verified_count"] += 1
            cat_summary[cat]["verified_val"] += val

    stats = PortfolioAuditStats(
        total_items=stats_dict["total_items"],
        total_replacement_value_usd=stats_dict["total_replacement_cost"],
        verified_items_count=stats_dict["verified_items"],
        verified_valuation_usd=stats_dict["verified_valuation_usd"],
        verification_percentage=stats_dict["verified_percentage"],
        categories=cat_summary,
    )

    export_inventory_markdown(items, stats, str(report_file))
    return FileResponse(
        path=str(report_file),
        filename="inventory_appraisal_report.md",
        media_type="text/markdown",
    )


def start_server(host: str = "127.0.0.1", port: int = 8080):
    """Launches the Uvicorn ASGI server with the FastAPI dashboard."""
    import uvicorn
    print(f"[*] Launching Open Home Inventory Dashboard on http://{host}:{port}")
    uvicorn.run("web.server:app", host=host, port=port, reload=False)

