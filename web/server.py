"""
FastAPI Dashboard Server for Open Home Inventory.
Provides REST APIs and the Archival Property Ledger local web UI for
reviewing/editing visual scans, merging multi-angle photos, uploading close-up
serial badges, resolving escalation diffs, linking receipts, and exporting Claim Packs.
"""

import hashlib
import io
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import zipfile

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, ImageOps
from pydantic import BaseModel, Field
from starlette.requests import Request

from src.receipts.parsers.base import ParsedReceipt
from src.reconciliation.appraisal import PortfolioAuditStats, estimate_acv
from src.reconciliation.matcher import InventoryMatcher
from src.storage.csv_exporter import export_inventory_csv
from src.storage.database import InventoryDatabase
from src.storage.markdown_exporter import export_inventory_markdown
from src.vision.badge_reader import BadgeReader
from src.vision.detector import DetectedItem, VisionDetector

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
DB_PATH = Path(os.getenv("INVENTORY_DB_PATH", str(PROJECT_ROOT / "data" / "inventory.db")))

app = FastAPI(
    title="Open Home Inventory",
    description="Local-first, privacy-focused visual & receipt inventory management.",
    version="3.0.0",
)

STATIC_DIR.mkdir(parents=True, exist_ok=True)
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
CROPS_DIR = Path(os.getenv("INVENTORY_CROPS_DIR", str(PROJECT_ROOT / "data" / "optical_crops")))
OVERLAYS_DIR = Path(os.getenv("INVENTORY_OVERLAYS_DIR", str(PROJECT_ROOT / "data" / "overlays")))
CROPS_DIR.mkdir(parents=True, exist_ok=True)
OVERLAYS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/crops", StaticFiles(directory=str(CROPS_DIR)), name="crops")
app.mount("/overlays", StaticFiles(directory=str(OVERLAYS_DIR)), name="overlays")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


class ItemPatchRequest(BaseModel):
    item_name: Optional[str] = None
    category: Optional[str] = None
    brand: Optional[str] = None
    model_or_identifiers: Optional[str] = None
    room: Optional[str] = None
    sub_location: Optional[str] = None
    condition: Optional[str] = None
    quantity: Optional[int] = None
    estimated_replacement_value_usd: Optional[float] = None
    estimated_acv_usd: Optional[float] = None
    serial_number: Optional[str] = None
    purchase_price_usd: Optional[float] = None
    purchase_date: Optional[str] = None
    merchant: Optional[str] = None
    order_number: Optional[str] = None
    receipt_evidence: Optional[str] = None
    receipt_verified: Optional[bool] = None
    badge_verified: Optional[bool] = None
    review_status: Optional[str] = None
    description: Optional[str] = None


class BulkDeleteRequest(BaseModel):
    item_ids: List[int] = Field(default_factory=list)
    archive: bool = False


class MergeItemsRequest(BaseModel):
    primary_id: int
    merge_ids: List[int] = Field(default_factory=list)


class ReceiptLinkRequest(BaseModel):
    receipt_id: int
    price_usd: Optional[float] = None
    serial_number: Optional[str] = None


def get_db() -> InventoryDatabase:
    """Returns database connection, creating directories if needed."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return InventoryDatabase(str(DB_PATH))


def _media_url(file_path: Optional[str]) -> Optional[str]:
    if not file_path:
        return None
    norm = str(file_path).replace("\\", "/")
    fname = os.path.basename(norm)
    if "/overlays/" in norm or fname.startswith("overlay_"):
        return f"/overlays/{fname}"
    return f"/crops/{fname}"


def _enrich_item_urls(item: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(item)
    out["crop_url"] = _media_url(out.get("source_image"))
    out["overlay_url"] = _media_url(out.get("overlay_image"))
    if out.get("estimated_acv_usd") is None and out.get("estimated_replacement_value_usd"):
        out["estimated_acv_usd"] = estimate_acv(
            float(out["estimated_replacement_value_usd"]),
            out.get("category"),
            out.get("purchase_date"),
            out.get("condition"),
        )
    if "photos" in out and isinstance(out["photos"], list):
        out["photos"] = [
            {**p, "url": _media_url(p.get("file_path"))}
            for p in out["photos"]
        ]
    return out


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Renders the main Archival Property Ledger dashboard."""
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/items")
async def list_items(
    category: Optional[str] = None,
    room: Optional[str] = None,
    review_status: Optional[str] = None,
    verified_only: bool = False,
    missing_evidence: bool = False,
    has_proposal: bool = False,
    include_archived: bool = False,
    query: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List inventory items with room, category, status, evidence-gap, proposal, and search filters."""
    db = get_db()
    items = db.get_all_items(include_archived=include_archived)

    if category:
        items = [it for it in items if (it.get("category") or "").lower() == category.lower()]
    if room:
        items = [it for it in items if (it.get("room") or "General").lower() == room.lower()]
    if review_status:
        items = [it for it in items if (it.get("review_status") or "unreviewed").lower() == review_status.lower()]
    if verified_only:
        items = [it for it in items if it.get("receipt_verified")]
    if missing_evidence:
        items = [
            it
            for it in items
            if float(it.get("estimated_replacement_value_usd") or 0.0) >= 500.0
            and (not it.get("receipt_verified") or not (it.get("serial_number") or "").strip())
        ]
    if has_proposal:
        items = [it for it in items if int(it.get("pending_proposal_count") or 0) > 0]
    if query:
        q_lower = query.strip().lower()
        items = [
            it
            for it in items
            if q_lower
            in (
                f"{it.get('item_name','')} {it.get('brand','')} {it.get('model_or_identifiers','')} "
                f"{it.get('serial_number','')} {it.get('room','')} {it.get('sub_location','')} {it.get('category','')}"
            ).lower()
        ]

    return [_enrich_item_urls(it) for it in items]


@app.get("/api/items/{item_id}")
async def get_item(item_id: int) -> Dict[str, Any]:
    """Retrieve full item details including photo filmstrip and pending escalation proposals."""
    db = get_db()
    item = db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    return _enrich_item_urls(item)


@app.patch("/api/items/{item_id}")
async def patch_item(item_id: int, payload: ItemPatchRequest) -> Dict[str, Any]:
    """Update editable fields on an inventory item and record locked user_edited_fields."""
    db = get_db()
    updates = payload.model_dump(exclude_unset=True)
    try:
        updated = db.update_item(item_id, updates, record_user_edit=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not updated:
        raise HTTPException(status_code=404, detail="Item not found")
    return _enrich_item_urls(updated)


@app.delete("/api/items/{item_id}")
async def delete_item(item_id: int, archive: bool = False) -> Dict[str, Any]:
    """Delete (or soft-archive with ?archive=true) a false positive or clutter item."""
    db = get_db()
    ok = db.delete_item(item_id, archive=archive)
    if not ok:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"deleted": True, "item_id": item_id, "archived": archive}


@app.post("/api/items/bulk-delete")
async def bulk_delete_items(payload: BulkDeleteRequest) -> Dict[str, Any]:
    """Delete or soft-archive multiple selected items in one request."""
    db = get_db()
    count = db.bulk_delete_items(payload.item_ids, archive=payload.archive)
    return {"deleted_count": count, "archived": payload.archive}


@app.post("/api/items/merge")
async def merge_items(payload: MergeItemsRequest) -> Dict[str, Any]:
    """Merge multi-angle detections into primary_id, preserving all photos in item_photos."""
    db = get_db()
    merged = db.merge_items(payload.primary_id, payload.merge_ids)
    if not merged:
        raise HTTPException(status_code=404, detail="Primary item not found")
    return _enrich_item_urls(merged)


@app.post("/api/items/{item_id}/badge-photo")
async def upload_badge_photo(
    item_id: int,
    file: UploadFile = File(...),
    run_ocr: bool = True,
) -> Dict[str, Any]:
    """
    Upload a close-up serial/badge photo, strip EXIF/GPS metadata, attach to item_photos,
    and optionally run local BadgeReader OCR to return suggested field updates.
    """
    db = get_db()
    item = db.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    raw_bytes = await file.read()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Empty upload")

    try:
        with Image.open(io.BytesIO(raw_bytes)) as raw_img:
            img = ImageOps.exif_transpose(raw_img)
            if img.mode != "RGB":
                img = img.convert("RGB")
            if max(img.size) > 2000:
                img.thumbnail((2000, 2000), Image.Resampling.LANCZOS)
            out_buf = io.BytesIO()
            img.save(out_buf, format="JPEG", quality=92)
            clean_bytes = out_buf.getvalue()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid image file: {e}")

    digest = hashlib.sha256(clean_bytes).hexdigest()
    CROPS_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"badge_{item_id}_{digest[:10]}.jpg"
    out_path = CROPS_DIR / fname
    out_path.write_bytes(clean_bytes)

    photo_id = db.add_item_photo(item_id, "closeup_badge", str(out_path), sha256=digest)

    suggested: Optional[Dict[str, Any]] = None
    ocr_error: Optional[str] = None
    if run_ocr:
        try:
            backend = os.getenv("INVENTORY_VISION_BACKEND", "ollama")
            endpoint = os.getenv("INVENTORY_VISION_ENDPOINT", "http://localhost:11434")
            model = os.getenv("INVENTORY_VISION_MODEL", "gemma4:e2b-it-q4_K_M")
            reader = BadgeReader(VisionDetector(backend=backend, endpoint=endpoint, model=model, timeout=30))
            suggested = reader.read_badge(str(out_path))
        except Exception as e:
            ocr_error = str(e)

    updated_item = db.get_item(item_id)
    return {
        "photo_id": photo_id,
        "photo_url": f"/crops/{fname}",
        "suggested_fields": suggested,
        "ocr_error": ocr_error,
        "item": _enrich_item_urls(updated_item) if updated_item else None,
    }


@app.get("/api/receipts")
async def list_receipts() -> List[Dict[str, Any]]:
    """List all ingested receipts with line items and linked asset counts."""
    db = get_db()
    rows = db.get_all_receipts()
    out: List[Dict[str, Any]] = []
    for r in rows:
        item = dict(r)
        try:
            parsed = json.loads(item.get("raw_json") or "{}")
            item["items"] = parsed.get("items") or []
        except Exception:
            item["items"] = []
        out.append(item)
    return out


@app.get("/api/items/{item_id}/receipt-candidates")
async def get_receipt_candidates(item_id: int, top_k: int = 3) -> List[Dict[str, Any]]:
    """Return top-K scored receipt candidates for 1-click manual linking."""
    db = get_db()
    raw = db.get_item(item_id)
    if not raw:
        raise HTTPException(status_code=404, detail="Item not found")

    det = DetectedItem(
        item_name=raw["item_name"],
        category=raw.get("category") or "General",
        brand=raw.get("brand"),
        model=raw.get("model_or_identifiers") or None,
        serial_number=raw.get("serial_number") or None,
        room=raw.get("room") or "General",
        condition=raw.get("condition") or "Good",
        estimated_replacement_value_usd=float(raw.get("estimated_replacement_value_usd") or 0.0),
        description=raw.get("description") or "",
    )
    parsed_receipts = []
    for rec in db.get_all_receipts():
        try:
            r_json = json.loads(rec.get("raw_json") or "{}")
            parsed_receipts.append((int(rec["id"]), ParsedReceipt(**r_json)))
        except Exception:
            continue

    matcher = InventoryMatcher()
    return matcher.rank_receipt_candidates(det, parsed_receipts, top_k=top_k)


@app.post("/api/items/{item_id}/receipt-link")
async def link_item_receipt(item_id: int, payload: ReceiptLinkRequest) -> Dict[str, Any]:
    """Manually link a receipt to an inventory item (protected as match_type='manual')."""
    db = get_db()
    updated = db.link_receipt(
        item_id=item_id,
        receipt_id=payload.receipt_id,
        price_usd=payload.price_usd,
        serial_number=payload.serial_number,
        match_type="manual",
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Item or receipt not found")
    return _enrich_item_urls(updated)


@app.delete("/api/items/{item_id}/receipt-link")
async def unlink_item_receipt(item_id: int) -> Dict[str, Any]:
    """Unlink proof-of-purchase receipt from an inventory item."""
    db = get_db()
    updated = db.unlink_receipt(item_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Item not found")
    return _enrich_item_urls(updated)


@app.get("/api/proposals")
async def list_proposals(status: str = "pending") -> List[Dict[str, Any]]:
    """List escalation diff proposals (default: pending)."""
    db = get_db()
    props = db.list_proposals(status=status)
    return [{**p, "crop_url": _media_url(p.get("source_image"))} for p in props]


@app.post("/api/proposals/{proposal_id}/accept")
async def accept_proposal(proposal_id: int, force: bool = False) -> Dict[str, Any]:
    """Accept an escalation diff proposal and apply non-locked fields to the item."""
    db = get_db()
    updated = db.apply_proposal(proposal_id, force=force)
    if not updated:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return _enrich_item_urls(updated)


@app.post("/api/proposals/{proposal_id}/reject")
async def reject_proposal(proposal_id: int) -> Dict[str, Any]:
    """Reject a pending escalation diff proposal."""
    db = get_db()
    if not db.reject_proposal(proposal_id):
        raise HTTPException(status_code=404, detail="Proposal not found")
    return {"rejected": True, "proposal_id": proposal_id}


@app.get("/api/stats")
async def get_stats() -> Dict[str, Any]:
    """Calculate aggregate insurance valuation, ACV, evidence gaps, and audit readiness metrics."""
    db = get_db()
    items = db.get_all_items()

    total_val = sum(float(it.get("estimated_replacement_value_usd") or 0.0) for it in items)
    total_acv = sum(
        float(it["estimated_acv_usd"])
        if it.get("estimated_acv_usd") is not None
        else estimate_acv(
            float(it.get("estimated_replacement_value_usd") or 0.0),
            it.get("category"),
            it.get("purchase_date"),
            it.get("condition"),
        )
        for it in items
    )
    orig_cost = sum(float(it.get("purchase_price_usd") or 0.0) for it in items)
    verified = [it for it in items if it.get("receipt_verified")]
    verified_val = sum(float(it.get("estimated_replacement_value_usd") or 0.0) for it in verified)
    with_serial = sum(1 for it in items if (it.get("serial_number") or "").strip())
    pct = (verified_val / total_val * 100.0) if total_val > 0 else 0.0
    audit_score = int(min(100, (pct * 0.6) + ((with_serial / len(items) * 100) * 0.4))) if items else 0

    missing_evidence_count = sum(
        1
        for it in items
        if float(it.get("estimated_replacement_value_usd") or 0.0) >= 500.0
        and (not it.get("receipt_verified") or not (it.get("serial_number") or "").strip())
    )
    pending_proposals = db.list_proposals(status="pending")
    receipts = db.get_all_receipts()

    cats: Dict[str, int] = {}
    rooms: Dict[str, int] = {}
    for it in items:
        c = it.get("category") or "General"
        r = it.get("room") or "General"
        cats[c] = cats.get(c, 0) + 1
        rooms[r] = rooms.get(r, 0) + 1

    return {
        "total_items": len(items),
        "total_replacement_cost": round(total_val, 2),
        "total_acv_usd": round(total_acv, 2),
        "total_purchase_cost": round(orig_cost, 2),
        "verified_items": len(verified),
        "verified_percentage": round(pct, 1),
        "verified_valuation_usd": round(verified_val, 2),
        "items_with_serial": with_serial,
        "missing_evidence_count": missing_evidence_count,
        "pending_proposals_count": len(pending_proposals),
        "receipts_count": len(receipts),
        "audit_readiness_score": audit_score,
        "category_breakdown": cats,
        "rooms_breakdown": rooms,
    }


def _build_portfolio_stats(items: List[Dict[str, Any]], stats_dict: Dict[str, Any]) -> PortfolioAuditStats:
    cat_summary: Dict[str, Dict[str, Any]] = {}
    missing_evidence: List[Dict[str, Any]] = []
    for it in items:
        cat = it.get("category") or "General"
        val = float(it.get("estimated_replacement_value_usd") or 0.0)
        is_v = bool(it.get("receipt_verified"))
        if cat not in cat_summary:
            cat_summary[cat] = {"count": 0, "value": 0.0, "verified_count": 0, "verified_val": 0.0}
        cat_summary[cat]["count"] += 1
        cat_summary[cat]["value"] += val
        if is_v:
            cat_summary[cat]["verified_count"] += 1
            cat_summary[cat]["verified_val"] += val
        if val >= 500.0:
            gaps = []
            if not is_v:
                gaps.append("receipt")
            if not (it.get("serial_number") or "").strip():
                gaps.append("serial_number")
            if gaps:
                missing_evidence.append(
                    {
                        "item_name": it.get("item_name", "Item"),
                        "room": it.get("room") or "General",
                        "estimated_replacement_value_usd": round(val, 2),
                        "missing": gaps,
                    }
                )

    return PortfolioAuditStats(
        total_items=stats_dict["total_items"],
        total_replacement_value_usd=stats_dict["total_replacement_cost"],
        total_acv_usd=stats_dict["total_acv_usd"],
        verified_items_count=stats_dict["verified_items"],
        verified_valuation_usd=stats_dict["verified_valuation_usd"],
        verification_percentage=stats_dict["verified_percentage"],
        categories=cat_summary,
        missing_evidence_items=sorted(
            missing_evidence,
            key=lambda x: x["estimated_replacement_value_usd"],
            reverse=True,
        ),
    )


@app.get("/api/export/csv")
async def export_csv():
    """Generates and returns an insurance-standard inventory CSV."""
    db = get_db()
    items = [_enrich_item_urls(it) for it in db.get_all_items()]
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
    stats = _build_portfolio_stats(items, stats_dict)
    export_inventory_markdown(items, stats, str(report_file))
    return FileResponse(
        path=str(report_file),
        filename="inventory_appraisal_report.md",
        media_type="text/markdown",
    )


@app.get("/api/export/claim-pack")
async def export_claim_pack():
    """Generates an insurer-ready Claim Pack .zip (CSV + Markdown appraisal + receipts JSON + EXIF-stripped crops)."""
    db = get_db()
    items = [_enrich_item_urls(it) for it in db.get_all_items()]
    receipts = db.get_all_receipts()
    export_dir = PROJECT_ROOT / "data" / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)

    csv_file = export_dir / "inventory_insurance_schedule.csv"
    report_file = export_dir / "inventory_appraisal_report.md"
    zip_file = export_dir / "home_inventory_claim_pack.zip"

    export_inventory_csv(items, str(csv_file))
    stats = _build_portfolio_stats(items, await get_stats())
    export_inventory_markdown(items, stats, str(report_file))

    with zipfile.ZipFile(zip_file, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(csv_file, arcname="inventory_insurance_schedule.csv")
        zf.write(report_file, arcname="inventory_appraisal_report.md")
        zf.writestr("receipts.json", json.dumps(receipts, indent=2))
        added_photos: set[str] = set()
        for it in items:
            full = db.get_item(int(it["id"]))
            for p in (full.get("photos") or []) if full else []:
                fp = p.get("file_path") or ""
                if fp and os.path.exists(fp):
                    arc = f"photos/{os.path.basename(fp)}"
                    if arc not in added_photos:
                        zf.write(fp, arcname=arc)
                        added_photos.add(arc)

    return FileResponse(
        path=str(zip_file),
        filename="home_inventory_claim_pack.zip",
        media_type="application/zip",
    )


def start_server(host: str = "127.0.0.1", port: int = 8080):
    """Launches the Uvicorn ASGI server with the FastAPI dashboard."""
    import uvicorn

    print(f"[*] Launching Open Home Inventory Dashboard on http://{host}:{port}")
    uvicorn.run("web.server:app", host=host, port=port, reload=False)
