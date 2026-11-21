"""
Multi-format local receipt folder ingestor (.pdf, .eml, .txt, .md, .jpg/.png/.webp).
Prioritizes zero-token deterministic Python extraction (pypdf + stdlib email + HTML strip +
merchant regex parsers), with optional $0-incremental LLM JSON fallback when requested.
"""

from datetime import datetime
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
import hashlib
import io
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from ..vision.detector import VisionDetector, strip_json_fences
from .parser_registry import (
    ReceiptParserRegistry,
    normalize_merchant,
    parse_receipt_text,
    strip_html,
)
from .parsers.base import ParsedItem, ParsedReceipt

logger = logging.getLogger(__name__)

TEXT_EXTS = (".txt", ".md")
EML_EXTS = (".eml",)
PDF_EXTS = (".pdf",)
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract selectable text from a PDF using pure-Python pypdf (zero LLM tokens)."""
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("pypdf is not installed; skipping PDF text extraction (install with `pip install pypdf`).")
        return ""

    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(pages).strip()


def _parse_email_date(raw_date: Optional[str]) -> str:
    if not raw_date:
        return datetime.now().strftime("%Y-%m-%d")
    try:
        return parsedate_to_datetime(raw_date).strftime("%Y-%m-%d")
    except Exception:
        return str(raw_date).strip()[:10]


def llm_extract_receipt(
    text: str,
    source_id: str,
    backend: str = "ollama",
    endpoint: str = "http://localhost:11434",
    model: str = "gemma4:e2b-it-q4_K_M",
    keep_alive: str = "15m",
    timeout: int = 60,
) -> Optional[ParsedReceipt]:
    """
    Optional structured JSON fallback for messy unformatted receipts.
    - Local Ollama enforces the 6,000-char (~1,500-token) ceiling with keep_alive & think=False.
    - OpenAI-compatible (e.g. MiniMax-M2.7) sends full text with reasoning_split=True.
    """
    prompt = (
        "Extract purchase receipt details from the text below into a single JSON object with keys:\n"
        '{"merchant": str, "order_number": str, "purchase_date": "YYYY-MM-DD", "total_usd": float, '
        '"items": [{"name": str, "price_usd": float, "quantity": int, "serial_number": str|null}]}\n'
        "Do not invent serial numbers or items. Return ONLY valid JSON.\n\n"
        f"RECEIPT TEXT:\n{text[:5800] if backend == 'ollama' else text[:40000]}"
    )

    raw_resp = ""
    if backend == "ollama":
        url = f"{endpoint.rstrip('/')}/api/generate"
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "keep_alive": keep_alive,
            "think": False,
            "format": "json",
            "options": {"temperature": 0.0, "num_predict": 600},
        }
        res = requests.post(url, json=payload, timeout=timeout)
        res.raise_for_status()
        raw_resp = res.json().get("response", "")
    elif backend in ("openai-compat", "minimax", "openai"):
        url = f"{endpoint.rstrip('/')}/v1/chat/completions"
        api_key = os.getenv("MINIMAX_API_KEY") or os.getenv("OPENAI_API_KEY") or "dummy-local-key"
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 4000,
        }
        if "minimax" in model.lower() or "minimax" in endpoint.lower():
            payload["reasoning_split"] = True
        else:
            payload["response_format"] = {"type": "json_object"}
        res = requests.post(url, headers=headers, json=payload, timeout=timeout)
        res.raise_for_status()
        raw_resp = res.json()["choices"][0]["message"]["content"]
    else:
        raise ValueError(f"Unsupported LLM receipt fallback backend: {backend}")

    data = json.loads(strip_json_fences(raw_resp))
    if not isinstance(data, dict) or not data.get("merchant"):
        return None

    items: List[ParsedItem] = []
    for raw_it in data.get("items") or []:
        if not isinstance(raw_it, dict) or not raw_it.get("name"):
            continue
        items.append(
            ParsedItem(
                name=str(raw_it["name"]).strip(),
                price_usd=float(raw_it.get("price_usd") or 0.0),
                quantity=max(1, int(raw_it.get("quantity") or 1)),
                serial_number=str(raw_it["serial_number"]).strip() if raw_it.get("serial_number") else None,
            )
        )
    if not items:
        items = [ParsedItem(name=f"{data['merchant']} Purchase", price_usd=float(data.get("total_usd") or 0.0))]

    order_num = str(data.get("order_number") or f"LLM-{source_id[:8]}").strip()
    return ParsedReceipt(
        merchant=normalize_merchant(str(data["merchant"])),
        order_number=order_num,
        purchase_date=str(data.get("purchase_date") or datetime.now().strftime("%Y-%m-%d"))[:10],
        total_usd=float(data.get("total_usd") or sum(i.price_usd * i.quantity for i in items)),
        items=items,
        source_message_id=source_id,
        notes=f"Parsed via LLM fallback ({model})",
    )


class LocalReceiptIngestor:
    """Walks a file or directory of receipts and converts them into ParsedReceipt records."""

    def __init__(
        self,
        registry: Optional[ReceiptParserRegistry] = None,
        vision_detector: Optional[VisionDetector] = None,
        llm_fallback: bool = False,
        llm_backend: str = "ollama",
        llm_endpoint: str = "http://localhost:11434",
        llm_model: str = "gemma4:e2b-it-q4_K_M",
    ):
        self.registry = registry or ReceiptParserRegistry()
        self.vision_detector = vision_detector
        self.llm_fallback = llm_fallback
        self.llm_backend = llm_backend
        self.llm_endpoint = llm_endpoint
        self.llm_model = llm_model

    def ingest_path(self, target: str) -> List[ParsedReceipt]:
        root = Path(target)
        files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
        results: List[ParsedReceipt] = []
        seen_orders: set[tuple[str, str]] = set()

        for fpath in files:
            ext = fpath.suffix.lower()
            parsed_list: List[ParsedReceipt] = []
            try:
                if ext in TEXT_EXTS:
                    parsed_list = self._ingest_text_file(fpath)
                elif ext in EML_EXTS:
                    parsed_list = self._ingest_eml_file(fpath)
                elif ext in PDF_EXTS:
                    parsed_list = self._ingest_pdf_bytes(fpath.read_bytes(), fpath.name)
                elif ext in IMAGE_EXTS:
                    parsed_list = self._ingest_image_file(fpath)
            except Exception as e:
                logger.warning(f"Failed to ingest receipt {fpath}: {e}")
                continue

            for rec in parsed_list:
                rec.merchant = normalize_merchant(rec.merchant)
                key = (rec.merchant.lower(), (rec.order_number or "").lower())
                if key not in seen_orders:
                    seen_orders.add(key)
                    results.append(rec)
        return results

    def _parse_block(
        self,
        text: str,
        source_id: str,
        sender: str = "",
        subject: str = "",
        date: str = "",
    ) -> Optional[ParsedReceipt]:
        cleaned = strip_html(text).strip()
        if len(cleaned) < 20:
            return None

        parsed = (
            self.registry.parse_email(source_id, date or datetime.now().strftime("%Y-%m-%d"), sender, subject, cleaned)
            if sender
            else parse_receipt_text(cleaned, sender=sender, subject=subject, date=date or datetime.now().strftime("%Y-%m-%d"), message_id=source_id)
        )
        used_generic = parsed is not None and "GenericFallbackParser" in (parsed.notes or "")
        if (parsed is None or (used_generic and parsed.total_usd == 0.0)) and self.llm_fallback:
            try:
                llm_rec = llm_extract_receipt(
                    cleaned,
                    source_id=source_id,
                    backend=self.llm_backend,
                    endpoint=self.llm_endpoint,
                    model=self.llm_model,
                )
                if llm_rec:
                    return llm_rec
            except Exception as e:
                logger.warning(f"LLM receipt fallback failed on {source_id}: {e}")
        return parsed

    def _ingest_text_file(self, fpath: Path) -> List[ParsedReceipt]:
        content = fpath.read_text(encoding="utf-8", errors="ignore")
        blocks = content.split("=== END_OF_RECEIPT ===")
        out: List[ParsedReceipt] = []
        for idx, block in enumerate(blocks):
            src_id = f"{fpath.stem}-{idx + 1}"
            rec = self._parse_block(block, source_id=src_id)
            if rec:
                out.append(rec)
        return out

    def _ingest_pdf_bytes(self, raw_bytes: bytes, filename: str) -> List[ParsedReceipt]:
        text = extract_pdf_text(raw_bytes)
        if len(text) < 40:
            logger.info(f"PDF '{filename}' has <40 chars of selectable text (likely scanned image PDF).")
            return []
        digest = hashlib.sha256(raw_bytes).hexdigest()[:10]
        rec = self._parse_block(text, source_id=f"PDF-{digest}", subject=Path(filename).stem)
        return [rec] if rec else []

    def _ingest_eml_file(self, fpath: Path) -> List[ParsedReceipt]:
        raw_bytes = fpath.read_bytes()
        msg = BytesParser(policy=policy.default).parsebytes(raw_bytes)
        sender = str(msg.get("From") or "")
        subject = str(msg.get("Subject") or fpath.stem)
        date_str = _parse_email_date(msg.get("Date"))
        msg_id = str(msg.get("Message-ID") or f"EML-{hashlib.sha256(raw_bytes).hexdigest()[:10]}").strip("<>")

        plain_parts: List[str] = []
        html_parts: List[str] = []
        pdf_attachments: List[tuple[str, bytes]] = []

        if msg.is_multipart():
            for part in msg.walk():
                ctype = (part.get_content_type() or "").lower()
                disp = str(part.get_content_disposition() or "").lower()
                fname = part.get_filename() or ""
                if ctype == "application/pdf" or fname.lower().endswith(".pdf"):
                    payload = part.get_payload(decode=True)
                    if payload:
                        pdf_attachments.append((fname or "attachment.pdf", payload))
                elif "attachment" not in disp:
                    if ctype == "text/plain":
                        plain_parts.append(part.get_content())
                    elif ctype == "text/html":
                        html_parts.append(part.get_content())
        else:
            ctype = (msg.get_content_type() or "").lower()
            content = msg.get_content()
            if ctype == "text/html":
                html_parts.append(content)
            else:
                plain_parts.append(content)

        body = "\n".join(plain_parts).strip() or strip_html("\n".join(html_parts))
        out: List[ParsedReceipt] = []
        rec = self._parse_block(body, source_id=msg_id, sender=sender, subject=subject, date=date_str)
        if rec:
            out.append(rec)

        for fname, pdf_bytes in pdf_attachments:
            out.extend(self._ingest_pdf_bytes(pdf_bytes, fname))
        return out

    def _ingest_image_file(self, fpath: Path) -> List[ParsedReceipt]:
        if self.vision_detector is None:
            logger.info(f"Skipping image receipt '{fpath.name}' (pass --ocr-images to enable local Vision OCR).")
            return []
        b64, mime = VisionDetector.encode_image(str(fpath), max_dim=1600)
        prompt = (
            "Transcribe all legible text from this receipt photo verbatim, preserving merchant name, "
            "order/reference number, date, line items with prices, serial numbers, and total."
        )
        text = self.vision_detector.complete_vision(b64, prompt, mime_type=mime, temperature=0.0, expect_json=False, max_tokens=1024)
        digest = hashlib.sha256(fpath.read_bytes()).hexdigest()[:10]
        rec = self._parse_block(text, source_id=f"IMG-{digest}", subject=fpath.stem)
        return [rec] if rec else []
