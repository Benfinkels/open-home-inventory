"""
High-value item escalation & OpenClaw/agent notification engine.

Zero-extra-cost defaults:
- Local multimodal Ollama (e.g. qwen3.5:9b on RTX 2080) with dual-image context.
- Antigravity / multimodal agent handoff (`export_candidates_bundle` / `import_proposals_bundle`)
  so your existing interactive agent can inspect high-value crops directly at $0 extra API spend.
- Optional opt-in cloud backends (`gemini` / `openai-compat`) guarded by --dry-run and --max-cost-usd.
"""

import json
import logging
import os
import re
import shlex
import subprocess
from typing import Any, Dict, List, Optional

import requests

from ..storage.database import GENERIC_BRANDS, InventoryDatabase
from .detector import VisionDetector, strip_json_fences

logger = logging.getLogger(__name__)

HIGH_SUBLIMIT_KEYWORDS = ("bicycle", "bike", "camera", "photography", "jewelry", "watch", "instrument", "firearm")

# Approximate paid per-1M token pricing for --dry-run / --max-cost-usd estimation.
MODEL_RATES_USD_PER_1M = {
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.6-flash": (0.75, 3.75),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.30, 2.50),
}


def estimate_call_cost_usd(backend: str, model: str, num_images: int = 2) -> float:
    """Conservative per-item cost estimate; $0.00 for local Ollama, mock, or agent handoff."""
    if backend in ("ollama", "mock", "agent"):
        return 0.0
    in_rate, out_rate = MODEL_RATES_USD_PER_1M.get(model.lower(), (0.75, 3.75))
    est_in_tokens = 450 + (num_images * 650)
    est_out_tokens = 350
    return round(((est_in_tokens * in_rate) + (est_out_tokens * out_rate)) / 1_000_000.0, 5)


def select_escalation_candidates(
    db: InventoryDatabase,
    min_value: float = 500.0,
    categories: Optional[List[str]] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Return high-value or high-sublimit items still missing brand, exact model, or badge verification."""
    cat_filters = [c.strip().lower() for c in (categories or []) if c.strip()]
    candidates: List[Dict[str, Any]] = []

    for item in db.get_all_items():
        val = float(item.get("estimated_replacement_value_usd") or 0.0)
        cat = (item.get("category") or "").lower()
        in_sublimit = any(kw in cat for kw in HIGH_SUBLIMIT_KEYWORDS)
        in_requested_cat = any(cf in cat for cf in cat_filters) if cat_filters else False

        if cat_filters and not in_requested_cat:
            continue
        if not cat_filters and val < min_value and not (in_sublimit and val >= 250.0):
            continue

        locked = set(item.get("user_edited_fields") or [])
        brand_missing = (item.get("brand") or "").strip().lower() in GENERIC_BRANDS
        model_missing = not (item.get("model_or_identifiers") or "").strip()
        unverified = not bool(item.get("badge_verified"))

        # Skip if user already manually locked brand + model + value
        if {"brand", "model_or_identifiers", "estimated_replacement_value_usd"}.issubset(locked):
            continue
        if not (brand_missing or model_missing or unverified):
            continue

        full = db.get_item(int(item["id"]))
        if full:
            candidates.append(full)

    if limit is not None and limit > 0:
        candidates = candidates[:limit]
    return candidates


def collect_item_images(item: Dict[str, Any], max_images: int = 2) -> List[str]:
    """Collect up to 2 existing EXIF-stripped images (prioritizing close-up badge -> optical crop -> overlay)."""
    role_priority = {"closeup_badge": 0, "optical_crop": 1, "room_overlay": 2}
    photos = sorted(
        item.get("photos") or [],
        key=lambda p: role_priority.get(p.get("photo_role") or "", 9),
    )
    paths: List[str] = []
    for p in photos:
        fp = p.get("file_path") or ""
        if fp and os.path.exists(fp) and fp not in paths:
            paths.append(fp)
    for fallback_key in ("source_image", "overlay_image"):
        fp = item.get(fallback_key) or ""
        if fp and os.path.exists(fp) and fp not in paths:
            paths.append(fp)
    return paths[:max_images]


def _escalation_prompt(item: Dict[str, Any]) -> str:
    return (
        "You are a specialist insurance property appraiser inspecting high-value asset photos.\n"
        f"Current catalog record: item_name={item.get('item_name')!r}, category={item.get('category')!r}, "
        f"brand={item.get('brand')!r}, model={item.get('model_or_identifiers')!r}, "
        f"serial={item.get('serial_number')!r}, room={item.get('room')!r}, "
        f"current_rcv_usd={item.get('estimated_replacement_value_usd')}.\n"
        "Using ONLY visual evidence in the provided image(s), identify the exact manufacturer brand, "
        "specific product model/series, any genuinely legible serial number (do NOT guess serials), "
        "and a realistic US replacement cost (RCV in USD).\n"
        "Return ONLY a single JSON object with keys:\n"
        '{"brand": str|null, "model": str|null, "serial_number": str|null, '
        '"estimated_replacement_value_usd": float|null, "reasoning": str}\n'
        "Leave a field null if not visually supported."
    )


def _complete_multi_image(
    backend: str,
    endpoint: str,
    model: str,
    image_paths: List[str],
    prompt: str,
    keep_alive: str = "15m",
    timeout: int = 120,
) -> str:
    if backend == "mock":
        return json.dumps(
            {
                "brand": "Specialized",
                "model": "Stumpjumper Comp Alloy S3",
                "serial_number": None,
                "estimated_replacement_value_usd": 3400.0,
                "reasoning": "Mock escalation identified full-suspension frame geometry and Specialized branding.",
            }
        )

    if backend in ("openai-compat", "openai") and "minimax-m2" in model.lower():
        raise ValueError(
            "MiniMax-M2.7 is text-only and does not accept image payloads. "
            "Use --backend ollama --model qwen3.5:9b ($0 local vision) or `proposals export-candidates` for Antigravity vision."
        )

    encoded = [VisionDetector.encode_image(p, max_dim=1400) for p in image_paths]
    if not encoded:
        raise FileNotFoundError("No readable image files found for item escalation.")

    if backend == "ollama":
        url = f"{endpoint.rstrip('/')}/api/generate"
        payload = {
            "model": model,
            "prompt": prompt,
            "images": [b64 for b64, _ in encoded],
            "stream": False,
            "keep_alive": keep_alive,
            "think": False,
            "format": "json",
            "options": {"temperature": 0.1, "num_predict": 512, "repeat_penalty": 1.1},
        }
        res = requests.post(url, json=payload, timeout=timeout)
        res.raise_for_status()
        return res.json().get("response", "")

    if backend in ("openai", "openai-compat", "vllm"):
        url = f"{endpoint.rstrip('/')}/v1/chat/completions"
        api_key = os.getenv("OPENAI_API_KEY", "dummy-local-key")
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        for b64, mime in encoded:
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.1,
            "max_tokens": 512,
            "response_format": {"type": "json_object"},
        }
        res = requests.post(url, headers=headers, json=payload, timeout=timeout)
        res.raise_for_status()
        return res.json()["choices"][0]["message"]["content"]

    if backend == "gemini":
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY is required when --backend gemini is explicitly selected.")
        base = endpoint if "generativelanguage.googleapis.com" in endpoint else "https://generativelanguage.googleapis.com"
        url = f"{base.rstrip('/')}/v1beta/models/{model}:generateContent?key={api_key}"
        parts: List[Dict[str, Any]] = [{"text": prompt}]
        for b64, mime in encoded:
            parts.append({"inline_data": {"mime_type": mime, "data": b64}})
        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 512, "responseMimeType": "application/json"},
        }
        res = requests.post(url, json=payload, timeout=timeout)
        res.raise_for_status()
        return res.json()["candidates"][0]["content"]["parts"][0]["text"]

    raise ValueError(f"Unsupported escalation backend: {backend}")


def _parse_escalation_json(raw_text: str) -> Dict[str, Any]:
    clean = strip_json_fences(raw_text)
    try:
        data = json.loads(clean)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", clean, flags=re.DOTALL)
        if not m:
            raise ValueError(f"Escalation model returned non-JSON: {clean[:200]}")
        data = json.loads(m.group(0))

    def _clean_opt(val: Any) -> Optional[str]:
        if val is None:
            return None
        s = str(val).strip()
        return None if s.lower() in ("", "null", "none", "n/a", "unknown") else s

    val_raw = data.get("estimated_replacement_value_usd")
    proposed_val: Optional[float] = None
    if val_raw not in (None, "", "null"):
        try:
            proposed_val = round(float(str(val_raw).replace("$", "").replace(",", "")), 2)
        except ValueError:
            proposed_val = None

    return {
        "brand": _clean_opt(data.get("brand")),
        "model": _clean_opt(data.get("model")),
        "serial_number": _clean_opt(data.get("serial_number")),
        "estimated_replacement_value_usd": proposed_val,
        "reasoning": str(data.get("reasoning") or "").strip(),
    }


def escalate_high_value_items(
    db: InventoryDatabase,
    backend: str = "ollama",
    endpoint: str = "http://localhost:11434",
    model: str = "qwen3.5:9b",
    min_value: float = 500.0,
    categories: Optional[List[str]] = None,
    limit: Optional[int] = None,
    dry_run: bool = False,
    auto_apply: bool = False,
    max_cost_usd: float = 0.25,
    keep_alive: str = "15m",
    timeout: int = 120,
) -> Dict[str, Any]:
    """Run high-value escalation and store diffs in escalation_proposals (or auto-apply)."""
    candidates = select_escalation_candidates(db, min_value=min_value, categories=categories, limit=limit)
    plan: List[Dict[str, Any]] = []
    total_est_cost = 0.0

    for item in candidates:
        imgs = collect_item_images(item)
        if not imgs and backend != "mock":
            continue
        est_cost = estimate_call_cost_usd(backend, model, num_images=max(1, len(imgs)))
        total_est_cost += est_cost
        plan.append({"item": item, "images": imgs, "est_cost_usd": est_cost})

    summary: Dict[str, Any] = {
        "backend": backend,
        "model": model,
        "dry_run": dry_run,
        "candidates": len(plan),
        "estimated_total_cost_usd": round(total_est_cost, 5),
        "proposals_created": [],
        "applied_count": 0,
        "errors": [],
    }

    if dry_run:
        summary["preview"] = [
            {
                "item_id": p["item"]["id"],
                "item_name": p["item"]["item_name"],
                "room": p["item"].get("room"),
                "current_value_usd": p["item"].get("estimated_replacement_value_usd"),
                "images": p["images"],
                "est_cost_usd": p["est_cost_usd"],
            }
            for p in plan
        ]
        return summary

    spent = 0.0
    for entry in plan:
        if spent + entry["est_cost_usd"] > max_cost_usd and entry["est_cost_usd"] > 0:
            summary["errors"].append(f"Stopped at budget cap (${max_cost_usd:.2f})")
            break
        item = entry["item"]
        try:
            raw = _complete_multi_image(
                backend=backend,
                endpoint=endpoint,
                model=model,
                image_paths=entry["images"],
                prompt=_escalation_prompt(item),
                keep_alive=keep_alive,
                timeout=timeout,
            )
            parsed = _parse_escalation_json(raw)
            prop_id = db.create_proposal(
                item_id=int(item["id"]),
                provider=backend,
                model=model,
                proposed_brand=parsed["brand"],
                proposed_model=parsed["model"],
                proposed_serial=parsed["serial_number"],
                proposed_value_usd=parsed["estimated_replacement_value_usd"],
                reasoning=parsed["reasoning"],
                est_cost_usd=entry["est_cost_usd"],
            )
            spent += entry["est_cost_usd"]
            if auto_apply:
                db.apply_proposal(prop_id)
                summary["applied_count"] += 1
            summary["proposals_created"].append(
                {"proposal_id": prop_id, "item_id": item["id"], "item_name": item["item_name"], **parsed}
            )
        except Exception as e:
            logger.error(f"Escalation failed for item #{item['id']}: {e}")
            summary["errors"].append(f"Item #{item['id']} ({item['item_name']}): {e}")

    summary["actual_est_cost_usd"] = round(spent, 5)
    return summary


def export_candidates_bundle(
    db: InventoryDatabase,
    min_value: float = 500.0,
    categories: Optional[List[str]] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Export high-value candidate metadata + EXIF-stripped image paths for $0 Antigravity/agent inspection."""
    candidates = select_escalation_candidates(db, min_value=min_value, categories=categories, limit=limit)
    rows = []
    for item in candidates:
        rows.append(
            {
                "item_id": item["id"],
                "item_name": item["item_name"],
                "room": item.get("room"),
                "category": item.get("category"),
                "brand": item.get("brand"),
                "model_or_identifiers": item.get("model_or_identifiers"),
                "serial_number": item.get("serial_number"),
                "estimated_replacement_value_usd": item.get("estimated_replacement_value_usd"),
                "image_paths": [os.path.abspath(p) for p in collect_item_images(item)],
            }
        )
    return {"min_value_usd": min_value, "count": len(rows), "candidates": rows}


def import_proposals_bundle(
    db: InventoryDatabase,
    proposals_payload: List[Dict[str, Any]],
    default_provider: str = "agent",
    default_model: str = "antigravity",
    auto_apply: bool = False,
) -> List[int]:
    """Import structured proposals generated by Antigravity or any external multimodal agent."""
    created_ids: List[int] = []
    for entry in proposals_payload:
        item_id = int(entry["item_id"])
        prop_id = db.create_proposal(
            item_id=item_id,
            provider=str(entry.get("provider") or default_provider),
            model=str(entry.get("model") or default_model),
            proposed_brand=entry.get("proposed_brand") or entry.get("brand"),
            proposed_model=entry.get("proposed_model") or entry.get("model_or_identifiers") or entry.get("model_name"),
            proposed_serial=entry.get("proposed_serial") or entry.get("serial_number"),
            proposed_value_usd=entry.get("proposed_value_usd") or entry.get("estimated_replacement_value_usd"),
            reasoning=str(entry.get("reasoning") or ""),
            est_cost_usd=float(entry.get("est_cost_usd") or 0.0),
        )
        if auto_apply:
            db.apply_proposal(prop_id)
        created_ids.append(prop_id)
    return created_ids


def format_escalation_digest(
    proposals: List[Dict[str, Any]],
    missing_evidence_count: int = 0,
    dashboard_url: str = "http://127.0.0.1:8080",
) -> str:
    """Build a concise Discord/OpenClaw-friendly Markdown digest of pending proposals."""
    lines = [
        "**Open Home Inventory — Escalation Review**",
        f"Pending proposals: **{len(proposals)}** | High-value missing evidence: **{missing_evidence_count}**",
        "",
    ]
    for p in proposals[:10]:
        pid = p.get("id") or p.get("proposal_id")
        name = p.get("item_name", f"Item #{p.get('item_id')}")
        diffs: List[str] = []
        if p.get("proposed_brand"):
            diffs.append(f"Brand -> `{p['proposed_brand']}`")
        if p.get("proposed_model"):
            diffs.append(f"Model -> `{p['proposed_model']}`")
        if p.get("proposed_serial"):
            diffs.append(f"Serial -> `{p['proposed_serial']}`")
        if p.get("proposed_value_usd") is not None:
            diffs.append(f"RCV -> `${float(p['proposed_value_usd']):,.0f}`")
        diff_str = " | ".join(diffs) or "No field changes"
        lines.append(f"- **#{pid}** {name}: {diff_str}")

    if len(proposals) > 10:
        lines.append(f"- _...and {len(proposals) - 10} more in the ledger_")
    lines.append("")
    lines.append(
        f"Review in UI: {dashboard_url}  |  CLI/Agent: `inventory-ai proposals apply <id>` / `reject <id>`"
    )
    return "\n".join(lines)


def send_webhook_notification(
    webhook_url: str,
    digest_text: str,
    structured_payload: Optional[Dict[str, Any]] = None,
    timeout: int = 15,
) -> bool:
    """Send digest to a Discord webhook or generic JSON agent webhook."""
    is_discord = "discord.com/api/webhooks/" in webhook_url or "discordapp.com/api/webhooks/" in webhook_url
    body: Dict[str, Any] = (
        {"content": digest_text[:1950]}
        if is_discord
        else {
            "event": "open_home_inventory.escalation",
            "message": digest_text,
            "data": structured_payload or {},
        }
    )
    res = requests.post(webhook_url, json=body, timeout=timeout)
    res.raise_for_status()
    return True


def send_openclaw_notification(digest_text: str, cmd: Optional[str] = None, timeout: int = 30) -> bool:
    """Hand off the escalation digest to OpenClaw (e.g. Alfred on Sheldon) via stdin."""
    raw_cmd = cmd or os.getenv("OPENCLAW_NOTIFY_CMD", "openclaw agent --message-file -")
    argv = shlex.split(raw_cmd)
    res = subprocess.run(argv, input=digest_text, text=True, capture_output=True, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(f"OpenClaw notify command failed ({res.returncode}): {res.stderr.strip()}")
    return True
