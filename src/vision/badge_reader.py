import json
import logging
import os
import re
from typing import Any, Dict, Optional

from .detector import DetectedItem, VisionDetector, strip_json_fences

logger = logging.getLogger(__name__)

GENERIC_BRANDS = frozenset({"", "unknown", "generic", "generic brand", "n/a", "none", "unbranded"})


def redact_serial(serial: Optional[str]) -> Optional[str]:
    """Mask the middle characters of a serial number for safe public sharing."""
    if not serial:
        return serial
    s = serial.strip()
    if len(s) <= 6:
        return "REDACTED"
    return f"{s[:3]}-REDACTED-{s[-3:]}"


class BadgeReader:
    """
    Focused Stage-2 OCR & Badge Extraction agent operating on 1:1 native optical crops
    to transcribe serial numbers, stamped model identifiers, and brand badges.
    Works across all VisionDetector backends (ollama, openai-compat, gemini, mock).
    """

    def __init__(self, detector: Optional[VisionDetector] = None):
        self.detector = detector or VisionDetector()

    def _badge_prompt(self) -> str:
        return (
            "You are an optical character recognition and industrial badge inspector. "
            "Examine this 1:1 native-resolution cropped image closely. "
            "Transcribe ONLY text, brand logos, model numbers, component markings, and serial numbers "
            "that are genuinely legible in the crop. Do NOT guess or fabricate serial numbers.\n"
            "Return ONLY a JSON object with keys:\n"
            "- brand (string or null)\n"
            "- model (string or null)\n"
            "- serial_number (string or null)\n"
            "- legible_text (array of strings)\n"
            "- confidence (float 0.0 to 1.0)\n"
        )

    def read_badge(self, crop_image_path: str) -> Dict[str, Any]:
        """Inspect a 1:1 optical crop and return structured OCR fields. Never returns fake mock data on error."""
        if not os.path.exists(crop_image_path):
            raise FileNotFoundError(f"Crop image not found: {crop_image_path}")

        if self.detector.backend == "mock":
            return self._mock_badge_read(crop_image_path)

        # Keep native resolution up to 1600px on crops so fine stamped text stays legible
        image_b64, mime_type = VisionDetector.encode_image(crop_image_path, max_dim=1600)
        raw_text = self.detector.complete_vision(
            image_b64,
            self._badge_prompt(),
            mime_type=mime_type,
            temperature=0.0,
            expect_json=True,
        )
        return self._parse_badge_json(raw_text)

    @staticmethod
    def _parse_badge_json(raw_text: str) -> Dict[str, Any]:
        clean = strip_json_fences(raw_text)
        try:
            data = json.loads(clean)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", clean, flags=re.DOTALL)
            if not m:
                return {"brand": None, "model": None, "serial_number": None, "legible_text": [], "confidence": 0.0}
            data = json.loads(m.group(0))

        if not isinstance(data, dict):
            return {"brand": None, "model": None, "serial_number": None, "legible_text": [], "confidence": 0.0}

        def _norm_opt(val: Any) -> Optional[str]:
            if val is None:
                return None
            s = str(val).strip()
            return None if s.lower() in ("", "null", "none", "n/a", "unknown") else s

        legible = data.get("legible_text")
        if not isinstance(legible, list):
            legible = [str(legible)] if legible else []
        legible_clean = [str(x).strip() for x in legible if str(x).strip()]

        try:
            conf = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
        except (TypeError, ValueError):
            conf = 0.0

        return {
            "brand": _norm_opt(data.get("brand")),
            "model": _norm_opt(data.get("model")),
            "serial_number": _norm_opt(data.get("serial_number")),
            "legible_text": legible_clean,
            "confidence": conf,
        }

    def enrich_item(self, item: DetectedItem, crop_image_path: str) -> tuple[DetectedItem, Dict[str, Any]]:
        """Run Stage-2 badge OCR on crop_image_path and merge verified fields into item."""
        badge = self.read_badge(crop_image_path)
        updated = item.model_copy(deep=True)
        changed = False

        b_brand = badge.get("brand")
        if b_brand and (
            (updated.brand or "").strip().lower() in GENERIC_BRANDS or badge.get("confidence", 0.0) >= 0.75
        ):
            updated.brand = b_brand
            changed = True

        b_model = badge.get("model")
        if b_model and (not updated.model or badge.get("confidence", 0.0) >= 0.70):
            updated.model = b_model
            changed = True

        b_serial = badge.get("serial_number")
        if b_serial and not updated.serial_number:
            updated.serial_number = b_serial
            changed = True

        legible = badge.get("legible_text") or []
        if legible:
            tag = "Badge OCR: " + ", ".join(legible[:8])
            if tag not in (updated.description or ""):
                updated.description = f"{updated.description} | {tag}".strip(" |")
            changed = True

        if changed:
            updated.badge_verified = True
        return updated, badge

    def _mock_badge_read(self, path: str) -> Dict[str, Any]:
        """Synthetic badge reading strictly when backend='mock'."""
        fname = os.path.basename(path).lower()
        if "stumpjumper" in fname or "bike" in fname:
            return {
                "brand": "Specialized",
                "model": "Stumpjumper Comp Alloy",
                "serial_number": "WSBC-MOCK-001928",
                "legible_text": ["SPECIALIZED", "STUMPJUMPER", "S3", "ALLOY"],
                "confidence": 0.95,
            }
        if "alienware" in fname or "monitor" in fname:
            return {
                "brand": "Alienware",
                "model": "AW3423DW",
                "serial_number": "MOCK-G8HQ2K3",
                "legible_text": ["ALIENWARE", "QD-OLED", "AW3423DW"],
                "confidence": 0.98,
            }
        return {
            "brand": "SampleBrand",
            "model": "SampleModel",
            "serial_number": "SN-MOCK-12345",
            "legible_text": ["SAMPLE", "MODEL-1"],
            "confidence": 0.85,
        }
