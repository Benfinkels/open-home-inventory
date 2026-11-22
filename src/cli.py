import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List

import yaml

from .receipts.ingestor import LocalReceiptIngestor
from .receipts.parser_registry import ReceiptParserRegistry
from .receipts.parsers.base import ParsedReceipt
from .receipts.scraper import ReceiptScraper
from .reconciliation.appraisal import AppraisalEngine
from .reconciliation.matcher import InventoryMatcher
from .storage.csv_exporter import export_inventory_csv
from .storage.database import InventoryDatabase, sha256_file
from .storage.markdown_exporter import export_inventory_markdown
from .vision.badge_reader import BadgeReader
from .vision.detector import DetectedItem, VisionDetector
from .vision.escalator import (
    escalate_high_value_items,
    export_candidates_bundle,
    format_escalation_digest,
    import_proposals_bundle,
    send_openclaw_notification,
    send_webhook_notification,
)
from .vision.optical_crop import OpticalCropEngine, sanitize_slug

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("inventory-ai")

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def load_config(config_path: str = "./config/default_config.yaml") -> Dict[str, Any]:
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def run_demo():
    """Runs a complete end-to-end demonstration using synthetic mock data."""
    from examples.generate_sample_data import generate_demo

    generate_demo()


def _discover_images(target: str) -> List[tuple[str, str]]:
    """Return sorted list of (image_path, inferred_room) tuples."""
    root_path = Path(target)
    if root_path.is_file():
        return [(str(root_path), "General")]
    found: List[tuple[str, str]] = []
    for root, _, files in os.walk(root_path):
        rel = Path(root).relative_to(root_path)
        room = rel.parts[0].replace("_", " ").replace("-", " ").title() if rel.parts else "General"
        for fname in sorted(files):
            if fname.lower().endswith(IMAGE_EXTS):
                found.append((os.path.join(root, fname), room))
    found.sort(key=lambda x: x[0])
    return found


def scan_photos_pipeline(
    image_dir: str,
    detector: VisionDetector,
    db: InventoryDatabase,
    crops_dir: str = "./data/optical_crops",
    overlays_dir: str = "./data/overlays",
    run_badge_ocr: bool = True,
    save_overlays: bool = False,
    room_override: str | None = None,
    rescan: bool = False,
    dedupe: bool = False,
    limit: int | None = None,
    padding_px: int = 40,
) -> Dict[str, Any]:
    """Run the 2-stage scan (Stage-1 detection -> 1:1 optical crop -> Stage-2 BadgeReader OCR)."""
    crop_engine = OpticalCropEngine(output_dir=crops_dir, padding_px=padding_px)
    badge_reader = BadgeReader(detector=detector) if run_badge_ocr else None

    discovered = _discover_images(image_dir)
    if limit is not None and limit > 0:
        discovered = discovered[:limit]

    stats: Dict[str, Any] = {
        "discovered": len(discovered),
        "scanned": 0,
        "skipped_cached": 0,
        "failed": 0,
        "items_saved": 0,
        "badges_enriched": 0,
        "duplicates_merged": 0,
        "errors": [],
    }

    for img_path, inferred_room in discovered:
        room = room_override or inferred_room
        img_hash = sha256_file(img_path)

        if not rescan and db.is_image_scanned(img_hash):
            stats["skipped_cached"] += 1
            logger.info(f"Skipping already-scanned photo: {os.path.basename(img_path)}")
            continue

        logger.info(f"Scanning [{room}] {img_path}...")
        try:
            items = detector.analyze_image(img_path, room=room)
        except Exception as e:
            stats["failed"] += 1
            err_msg = f"{os.path.basename(img_path)}: {e}"
            stats["errors"].append(err_msg)
            db.record_scanned_image(img_hash, img_path, room=room, status="error", item_count=0, error_message=str(e))
            logger.error(f"Vision detection failed on {img_path}: {e}")
            continue

        enriched_items: List[DetectedItem] = []
        overlay_path = ""
        for idx, it in enumerate(items):
            crop_p = None
            if it.box:
                slug = f"{it.brand}_{it.item_name}_{idx}"
                crop_p = crop_engine.crop_item(img_path, it.box, slug)
                if crop_p:
                    it.source_image = crop_p
                    if badge_reader is not None:
                        try:
                            it, _ = badge_reader.enrich_item(it, crop_p)
                            if it.badge_verified:
                                stats["badges_enriched"] += 1
                        except Exception as ocr_err:
                            logger.warning(f"Stage-2 badge OCR failed on {crop_p}: {ocr_err}")
            enriched_items.append(it)

        if save_overlays and enriched_items:
            stem = sanitize_slug(os.path.splitext(os.path.basename(img_path))[0])
            ov_out = os.path.join(overlays_dir, f"overlay_{stem}.jpg")
            overlay_path = crop_engine.render_overlay(img_path, enriched_items, ov_out) or ""

        for it in enriched_items:
            db.save_item(
                {
                    "item_name": it.item_name,
                    "category": it.category,
                    "brand": it.brand,
                    "model_or_identifiers": it.model or "",
                    "room": it.room or room,
                    "condition": it.condition,
                    "estimated_replacement_value_usd": it.estimated_replacement_value_usd,
                    "receipt_verified": False,
                    "serial_number": it.serial_number or "",
                    "source_image": it.source_image,
                    "original_image": img_path,
                    "overlay_image": overlay_path,
                    "image_hash": img_hash,
                    "badge_verified": it.badge_verified,
                    "description": it.description,
                }
            )
            stats["items_saved"] += 1

        db.record_scanned_image(img_hash, img_path, room=room, status="ok", item_count=len(enriched_items))
        stats["scanned"] += 1

    if dedupe:
        stats["duplicates_merged"] = db.deduplicate_items()

    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Open Home Inventory: Multimodal Vision & Proof-of-Purchase Pipeline"
    )
    subparsers = parser.add_subparsers(dest="command", help="Pipeline commands")

    # Demo
    subparsers.add_parser("demo", help="Run full pipeline on synthetic mock data")

    # Scan Photos
    scan_p = subparsers.add_parser("scan-photos", help="Scan directory (or single file) of photos for valuable assets")
    scan_p.add_argument("image_dir", type=str, help="Directory or image file path")
    scan_p.add_argument("--backend", type=str, default=None, choices=["ollama", "openai", "openai-compat", "vllm", "gemini", "mock"])
    scan_p.add_argument("--endpoint", type=str, default=None, help="Vision endpoint URL (e.g. http://192.0.2.10:11434)")
    scan_p.add_argument("--model", type=str, default=None, help="Vision model ID (e.g. gemma4:e2b-it-q4_K_M)")
    scan_p.add_argument("--room", type=str, default=None, help="Override room name (default: inferred from subfolder)")
    scan_p.add_argument("--db-path", type=str, default=None, help="Override SQLite database path")
    scan_p.add_argument("--crops-dir", type=str, default=None, help="Override optical crops directory")
    scan_p.add_argument("--no-ocr", action="store_true", help="Skip Stage-2 BadgeReader OCR on optical crops")
    scan_p.add_argument("--save-overlays", action="store_true", help="Save annotated bounding-box overlay images")
    scan_p.add_argument("--rescan", action="store_true", help="Re-scan images even if their SHA-256 hash is already cached")
    scan_p.add_argument("--dedupe", action="store_true", help="Merge cross-photo duplicate detections after scanning")
    scan_p.add_argument("--limit", type=int, default=None, help="Process at most N photos")

    # Scrape Receipts
    scrape_p = subparsers.add_parser("scrape-receipts", help="Ingest order receipts from folder, file, or Gmail")
    scrape_p.add_argument("--dir", type=str, default=None, help="Directory of .pdf/.eml/.txt/.jpg receipts")
    scrape_p.add_argument("--file", type=str, default=None, help="Single receipt file (.txt/.eml/.pdf/.jpg)")
    scrape_p.add_argument("--provider", type=str, default=None, choices=["mock", "gog", "file"], help="Email provider")
    scrape_p.add_argument("--query", type=str, default="receipt OR order OR invoice", help="Gmail search query")
    scrape_p.add_argument("--max", type=int, default=25, help="Maximum emails to retrieve")
    scrape_p.add_argument("--llm-fallback", action="store_true", help="Use local Ollama or OpenAI-compat JSON fallback for messy receipts")
    scrape_p.add_argument("--llm-backend", type=str, default="ollama", choices=["ollama", "openai-compat", "minimax"])
    scrape_p.add_argument("--llm-endpoint", type=str, default=None)
    scrape_p.add_argument("--llm-model", type=str, default=None)
    scrape_p.add_argument("--ocr-images", action="store_true", help="Run local Vision OCR on receipt image files (.jpg/.png)")
    scrape_p.add_argument("--db-path", type=str, default=None, help="Override SQLite database path")

    # Escalate High-Value Items ($0 local Ollama default; opt-in cloud or Antigravity agent handoff)
    esc_p = subparsers.add_parser("escalate-high-value", help="Re-inspect high-value items with a stronger multimodal model")
    esc_p.add_argument("--min-value", type=float, default=500.0, help="Minimum RCV threshold in USD")
    esc_p.add_argument("--categories", type=str, default=None, help="Comma-separated category keywords to target")
    esc_p.add_argument("--backend", type=str, default="ollama", choices=["ollama", "openai-compat", "gemini", "mock"])
    esc_p.add_argument("--endpoint", type=str, default=None, help="Model endpoint URL (default from config)")
    esc_p.add_argument("--model", type=str, default="qwen3.5:9b", help="Multimodal model ID (default: qwen3.5:9b on local Ollama)")
    esc_p.add_argument("--limit", type=int, default=None, help="Maximum items to escalate")
    esc_p.add_argument("--dry-run", action="store_true", help="Print candidate count and estimated cost without calling any model")
    esc_p.add_argument("--auto-apply", action="store_true", help="Apply proposals immediately instead of queuing for review")
    esc_p.add_argument("--max-cost-usd", type=float, default=0.25, help="Hard USD budget cap for cloud backends")
    esc_p.add_argument("--json", action="store_true", dest="as_json", help="Emit machine-readable JSON output")
    esc_p.add_argument("--notify-webhook", type=str, default=None, help="Send digest to Discord or generic JSON webhook URL")
    esc_p.add_argument("--notify-openclaw", action="store_true", help="Pipe digest to OpenClaw (Alfred) via stdin")
    esc_p.add_argument("--dashboard-url", type=str, default="http://127.0.0.1:8080", help="Dashboard link included in notifications")
    esc_p.add_argument("--db-path", type=str, default=None, help="Override SQLite database path")

    # Proposals CLI (for Web UI, OpenClaw/Alfred, and $0 Antigravity agent handoff)
    prop_p = subparsers.add_parser("proposals", help="Inspect, export, import, accept, or reject escalation proposals")
    prop_sub = prop_p.add_subparsers(dest="prop_action", required=True)

    p_list = prop_sub.add_parser("list", help="List escalation proposals")
    p_list.add_argument("--status", type=str, default="pending", choices=["pending", "accepted", "rejected", "all"])
    p_list.add_argument("--json", action="store_true", dest="as_json")
    p_list.add_argument("--db-path", type=str, default=None)

    p_exp = prop_sub.add_parser("export-candidates", help="Export high-value candidate crops for $0 Antigravity/agent vision inspection")
    p_exp.add_argument("--min-value", type=float, default=500.0)
    p_exp.add_argument("--categories", type=str, default=None)
    p_exp.add_argument("--limit", type=int, default=None)
    p_exp.add_argument("--out", type=str, default=None, help="Optional output JSON file (default: stdout)")
    p_exp.add_argument("--db-path", type=str, default=None)

    p_imp = prop_sub.add_parser("import", help="Import JSON proposals produced by Antigravity or an external agent")
    p_imp.add_argument("json_file", type=str, help="Path to JSON file (array or {'proposals': [...]})")
    p_imp.add_argument("--auto-apply", action="store_true")
    p_imp.add_argument("--notify-webhook", type=str, default=None)
    p_imp.add_argument("--notify-openclaw", action="store_true")
    p_imp.add_argument("--db-path", type=str, default=None)

    p_app = prop_sub.add_parser("apply", help="Accept and apply a pending proposal by ID")
    p_app.add_argument("proposal_id", type=int)
    p_app.add_argument("--force", action="store_true", help="Overwrite user-edited fields")
    p_app.add_argument("--db-path", type=str, default=None)

    p_rej = prop_sub.add_parser("reject", help="Reject a pending proposal by ID")
    p_rej.add_argument("proposal_id", type=int)
    p_rej.add_argument("--db-path", type=str, default=None)

    # Reconcile
    recon_p = subparsers.add_parser("reconcile", help="Reconcile visual detections against parsed receipts")
    recon_p.add_argument("--threshold", type=float, default=0.65, help="Fuzzy matching threshold (0.0 to 1.0)")
    recon_p.add_argument("--db-path", type=str, default=None, help="Override SQLite database path")
    recon_p.add_argument("--dedupe", action="store_true", help="Deduplicate items before reconciliation")

    # Import Legacy JSON
    imp_p = subparsers.add_parser("import-json", help="Import a curated JSON inventory array into SQLite")
    imp_p.add_argument("json_file", type=str, help="Path to JSON inventory file")
    imp_p.add_argument("--replace", action="store_true", help="Replace existing DB items and receipts")
    imp_p.add_argument("--crops-dir", type=str, default=None, help="Optical crops directory to match thumbnails")
    imp_p.add_argument("--overlays-dir", type=str, default=None, help="Overlays directory to match context views")
    imp_p.add_argument("--db-path", type=str, default=None, help="Override SQLite database path")

    # Serve
    serve_p = subparsers.add_parser("serve", help="Launch local interactive web dashboard")
    serve_p.add_argument("--port", type=int, default=8080, help="Web server port")
    serve_p.add_argument("--host", type=str, default="127.0.0.1", help="Web server host")

    args = parser.parse_args()

    if args.command == "demo" or not args.command:
        run_demo()

    elif args.command == "serve":
        from web.server import start_server

        start_server(host=args.host, port=args.port)

    elif args.command == "import-json":
        cfg = load_config()
        s_cfg = cfg.get("storage", {})
        db = InventoryDatabase(args.db_path or s_cfg.get("db_path", "./data/inventory.db"))
        raw = json.loads(Path(args.json_file).read_text(encoding="utf-8"))
        records = raw if isinstance(raw, list) else raw.get("items", [])
        res = db.import_legacy_json(
            records,
            crops_dir=args.crops_dir or s_cfg.get("crops_dir", "./data/optical_crops"),
            overlays_dir=args.overlays_dir or s_cfg.get("overlays_dir", "./data/overlays"),
            replace_existing=args.replace,
        )
        print(
            f"[✓] Imported {res['items']} curated items and {res['receipts']} verified receipts "
            f"(skipped {res['skipped_duplicates']} duplicate views)."
        )

    elif args.command == "scan-photos":
        cfg = load_config()
        v_cfg = cfg.get("vision", {})
        s_cfg = cfg.get("storage", {})
        crop_cfg = v_cfg.get("optical_crop", {})

        db = InventoryDatabase(args.db_path or s_cfg.get("db_path", "./data/inventory.db"))
        detector = VisionDetector(
            backend=args.backend or v_cfg.get("backend", "ollama"),
            endpoint=args.endpoint or v_cfg.get("endpoint", "http://localhost:11434"),
            model=args.model or v_cfg.get("model", "gemma4:e2b-it-q4_K_M"),
            temperature=float(v_cfg.get("temperature", 0.1)),
            timeout=int(v_cfg.get("timeout_seconds", 90)),
            keep_alive=str(v_cfg.get("keep_alive", "15m")),
        )

        res = scan_photos_pipeline(
            image_dir=args.image_dir,
            detector=detector,
            db=db,
            crops_dir=args.crops_dir or s_cfg.get("crops_dir", "./data/optical_crops"),
            overlays_dir=s_cfg.get("overlays_dir", "./data/overlays"),
            run_badge_ocr=not args.no_ocr,
            save_overlays=args.save_overlays,
            room_override=args.room,
            rescan=args.rescan,
            dedupe=args.dedupe,
            limit=args.limit,
            padding_px=int(crop_cfg.get("padding_px", 40)),
        )
        print(
            f"[✓] Scan complete: {res['scanned']} scanned, {res['skipped_cached']} cached/skipped, "
            f"{res['failed']} failed | {res['items_saved']} items saved ({res['badges_enriched']} badge-enriched, "
            f"{res['duplicates_merged']} duplicates merged)."
        )

    elif args.command == "scrape-receipts":
        cfg = load_config()
        r_cfg = cfg.get("receipts", {})
        v_cfg = cfg.get("vision", {})
        s_cfg = cfg.get("storage", {})
        db = InventoryDatabase(args.db_path or s_cfg.get("db_path", "./data/inventory.db"))

        target_path = args.dir or args.file
        if target_path and os.path.exists(target_path):
            print(f"[*] Ingesting receipts from: {target_path}")
            v_det = (
                VisionDetector(
                    backend=v_cfg.get("backend", "ollama"),
                    endpoint=v_cfg.get("endpoint", "http://localhost:11434"),
                    model=v_cfg.get("model", "gemma4:e2b-it-q4_K_M"),
                    keep_alive=str(v_cfg.get("keep_alive", "15m")),
                )
                if args.ocr_images
                else None
            )
            llm_ep = args.llm_endpoint or (
                "https://api.minimax.io" if args.llm_backend == "minimax" else v_cfg.get("endpoint", "http://localhost:11434")
            )
            llm_mod = args.llm_model or (
                "MiniMax-M2.7" if args.llm_backend == "minimax" else v_cfg.get("model", "gemma4:e2b-it-q4_K_M")
            )
            ingestor = LocalReceiptIngestor(
                vision_detector=v_det,
                llm_fallback=args.llm_fallback,
                llm_backend=args.llm_backend,
                llm_endpoint=llm_ep,
                llm_model=llm_mod,
            )
            receipts = ingestor.ingest_path(target_path)
            for parsed in receipts:
                db.save_receipt(parsed)
                print(f"      -> Ingested {parsed.merchant} #{parsed.order_number} (${parsed.total_usd:,.2f})")
            print(f"[✓] Ingested and stored {len(receipts)} receipts into database.")
        else:
            provider = args.provider or r_cfg.get("provider", "mock")
            scraper = ReceiptScraper(provider=provider)
            messages = scraper.fetch_receipts(query=args.query, max_results=args.max)
            registry = ReceiptParserRegistry()
            saved_count = 0
            for m in messages:
                rec = registry.parse_email(m.message_id, m.date, m.sender, m.subject, m.body)
                if rec:
                    db.save_receipt(rec)
                    saved_count += 1
                    print(f"      -> Ingested {rec.merchant} #{rec.order_number} (${rec.total_usd:,.2f})")
            print(f"[✓] Retrieved and stored {saved_count} receipts into database.")

    elif args.command == "escalate-high-value":
        cfg = load_config()
        v_cfg = cfg.get("vision", {})
        s_cfg = cfg.get("storage", {})
        db = InventoryDatabase(args.db_path or s_cfg.get("db_path", "./data/inventory.db"))
        cats = [c.strip() for c in args.categories.split(",")] if args.categories else None

        res = escalate_high_value_items(
            db=db,
            backend=args.backend,
            endpoint=args.endpoint or v_cfg.get("endpoint", "http://localhost:11434"),
            model=args.model,
            min_value=args.min_value,
            categories=cats,
            limit=args.limit,
            dry_run=args.dry_run,
            auto_apply=args.auto_apply,
            max_cost_usd=args.max_cost_usd,
            keep_alive=str(v_cfg.get("keep_alive", "15m")),
        )

        if not args.dry_run and (args.notify_webhook or args.notify_openclaw):
            pending = db.list_proposals(status="pending")
            missing_cnt = sum(
                1
                for it in db.get_all_items()
                if float(it.get("estimated_replacement_value_usd") or 0.0) >= 500.0
                and (not it.get("receipt_verified") or not (it.get("serial_number") or "").strip())
            )
            digest = format_escalation_digest(pending, missing_evidence_count=missing_cnt, dashboard_url=args.dashboard_url)
            if args.notify_webhook:
                send_webhook_notification(args.notify_webhook, digest, structured_payload=res)
                res["notified_webhook"] = True
            if args.notify_openclaw:
                send_openclaw_notification(digest)
                res["notified_openclaw"] = True

        if args.as_json:
            print(json.dumps(res, indent=2))
        elif args.dry_run:
            print(
                f"[DRY RUN] {res['candidates']} candidate items (>= ${args.min_value:,.0f}) | "
                f"backend={res['backend']} model={res['model']} | est_cost=${res['estimated_total_cost_usd']:.4f}"
            )
            for row in res.get("preview", []):
                print(
                    f"  - #{row['item_id']} [{row['room']}] {row['item_name']} "
                    f"(${float(row['current_value_usd'] or 0):,.0f}) | {len(row['images'])} img(s)"
                )
        else:
            print(
                f"[✓] Escalation complete: {len(res['proposals_created'])} proposals queued "
                f"({res['applied_count']} auto-applied), {len(res['errors'])} errors | "
                f"est_cost=${res.get('actual_est_cost_usd', 0.0):.4f}"
            )

    elif args.command == "proposals":
        cfg = load_config()
        s_cfg = cfg.get("storage", {})
        db = InventoryDatabase(args.db_path or s_cfg.get("db_path", "./data/inventory.db"))

        if args.prop_action == "list":
            props = db.list_proposals(status=args.status)
            if args.as_json:
                print(json.dumps(props, indent=2))
            elif not props:
                print(f"[*] No {args.status} proposals.")
            else:
                for p in props:
                    print(
                        f"#{p['id']} [{p['status']}] Item #{p['item_id']} ({p['item_name']}) "
                        f"-> Brand: {p['proposed_brand']} | Model: {p['proposed_model']} | "
                        f"Serial: {p['proposed_serial']} | RCV: {p['proposed_value_usd']} ({p['provider']}/{p['model']})"
                    )
        elif args.prop_action == "export-candidates":
            cats = [c.strip() for c in args.categories.split(",")] if args.categories else None
            bundle = export_candidates_bundle(db, min_value=args.min_value, categories=cats, limit=args.limit)
            payload_str = json.dumps(bundle, indent=2)
            if args.out:
                Path(args.out).write_text(payload_str, encoding="utf-8")
                print(f"[✓] Exported {bundle['count']} high-value candidate items to {args.out}")
            else:
                print(payload_str)
        elif args.prop_action == "import":
            raw = json.loads(Path(args.json_file).read_text(encoding="utf-8"))
            entries = raw.get("proposals", []) if isinstance(raw, dict) else raw
            ids = import_proposals_bundle(db, entries, auto_apply=args.auto_apply)
            print(f"[✓] Imported {len(ids)} proposals: {ids}")
            if args.notify_webhook or args.notify_openclaw:
                pending = db.list_proposals(status="pending")
                digest = format_escalation_digest(pending)
                if args.notify_webhook:
                    send_webhook_notification(args.notify_webhook, digest, structured_payload={"imported_ids": ids})
                if args.notify_openclaw:
                    send_openclaw_notification(digest)
        elif args.prop_action == "apply":
            updated = db.apply_proposal(args.proposal_id, force=args.force)
            if not updated:
                print(f"[!] Proposal #{args.proposal_id} not found.")
            else:
                print(f"[✓] Applied proposal #{args.proposal_id} to Item #{updated['id']} ({updated['item_name']}).")
        elif args.prop_action == "reject":
            ok = db.reject_proposal(args.proposal_id)
            print(f"[✓] Rejected proposal #{args.proposal_id}." if ok else f"[!] Proposal #{args.proposal_id} not found.")

    elif args.command == "reconcile":
        cfg = load_config()
        s_cfg = cfg.get("storage", {})
        db_path = args.db_path or s_cfg.get("db_path", "./data/inventory.db")
        db = InventoryDatabase(db_path)

        if args.dedupe:
            merged = db.deduplicate_items()
            if merged:
                print(f"[*] Deduplicated {merged} duplicate cross-photo detections.")

        db_items = db.get_all_items()
        db_receipts_raw = db.get_all_receipts()

        if not db_items:
            print("[!] No cataloged assets in database. Run 'scan-photos' or 'demo' first.")
            return

        items = [
            DetectedItem(
                item_name=raw["item_name"],
                category=raw.get("category") or "General",
                brand=raw.get("brand"),
                model=raw.get("model_or_identifiers") or None,
                room=raw.get("room") or "General",
                condition=raw.get("condition") or "Good",
                serial_number=raw.get("serial_number") or None,
                estimated_replacement_value_usd=raw.get("estimated_replacement_value_usd") or 0.0,
                description=raw.get("description") or "",
                source_image=raw.get("source_image") or "",
                original_image=raw.get("original_image") or "",
                badge_verified=bool(raw.get("badge_verified")),
            )
            for raw in db_items
        ]

        receipts = []
        receipt_id_by_order: Dict[str, int] = {}
        for r_raw in db_receipts_raw:
            try:
                r_json = json.loads(r_raw.get("raw_json", "{}"))
                rec = ParsedReceipt(**r_json)
                receipts.append(rec)
                if rec.order_number:
                    receipt_id_by_order[rec.order_number] = int(r_raw["id"])
            except Exception:
                continue

        print(f"[*] Reconciling {len(items)} cataloged assets against {len(receipts)} digital receipts...")
        matcher = InventoryMatcher(fuzzy_threshold=args.threshold)
        matches = matcher.match_batch(items, receipts)

        appraiser = AppraisalEngine()
        reconciled_records, stats = appraiser.evaluate_portfolio(matches)

        # Update items in place so IDs, item_photos, proposals, user edits, and manual receipt links are preserved.
        for raw, rec in zip(db_items, reconciled_records):
            if raw.get("receipt_match_type") == "manual" and raw.get("receipt_verified"):
                # Keep user's manual receipt link untouched; only backfill ACV if empty
                if raw.get("estimated_acv_usd") is None:
                    db.update_item(int(raw["id"]), {"estimated_acv_usd": rec["estimated_acv_usd"]}, record_user_edit=False)
                continue
            updates = {
                "estimated_replacement_value_usd": rec["estimated_replacement_value_usd"],
                "estimated_acv_usd": rec["estimated_acv_usd"],
                "receipt_verified": rec["receipt_verified"],
                "purchase_price_usd": rec["purchase_price_usd"],
                "purchase_date": rec["purchase_date"],
                "merchant": rec["merchant"],
                "order_number": rec["order_number"],
                "serial_number": rec["serial_number"] or raw.get("serial_number"),
                "receipt_evidence": rec["receipt_evidence"],
            }
            db.update_item(int(raw["id"]), updates, record_user_edit=False)

        fresh_items = db.get_all_items()
        export_dir = Path("./data/exports")
        export_dir.mkdir(parents=True, exist_ok=True)
        csv_file = export_dir / "inventory_insurance_schedule.csv"
        report_file = export_dir / "inventory_appraisal_report.md"

        export_inventory_csv(fresh_items, str(csv_file))
        export_inventory_markdown(fresh_items, stats, str(report_file))

        print("\n" + "=" * 65)
        print("  CROSS-MODAL RECONCILIATION COMPLETE")
        print(f"  Total Cataloged Assets:       {stats.total_items}")
        print(f"  Total Replacement Value:      ${stats.total_replacement_value_usd:,.2f} USD")
        print(f"  Estimated Depreciated ACV:    ${stats.total_acv_usd:,.2f} USD")
        print(f"  Receipt-Verified Assets:      {stats.verified_items_count} ({stats.verification_percentage:.1f}%)")
        print(f"  Verified Valuation Backed:    ${stats.verified_valuation_usd:,.2f} USD")
        print(f"  High-Value Missing Evidence:  {len(stats.missing_evidence_items)} items (>= $500)")
        print(f"  Database Updated:             {db_path}")
        print(f"  CSV Schedule Exported:        {csv_file}")
        print(f"  Appraisal Report Exported:    {report_file}")
        print("=" * 65)


if __name__ == "__main__":
    main()
