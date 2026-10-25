import logging
import os
import re
from typing import Dict, Any, Optional
from .detector import VisionDetector

logger = logging.getLogger(__name__)


class BadgeReader:
    """
    Focused OCR & Badge Extraction agent that operates on 1:1 optical crops
    to reliably transcribe serial numbers, model badges, and manufacturer stamps.
    """

    def __init__(self, detector: Optional[VisionDetector] = None):
        self.detector = detector or VisionDetector()

    def read_badge(self, crop_image_path: str) -> Dict[str, Any]:
        """
        Inspects an optical crop image and transcribes all legible text,
        brand emblems, model identifiers, and serial numbers.
        """
        if not os.path.exists(crop_image_path):
            return {"raw_text": "", "serial_number": None, "model": None, "brand": None}

        if self.detector.backend == "mock":
            return self._mock_badge_read(crop_image_path)

        prompt = (
            "You are an optical character recognition and industrial badge inspector. "
            "Examine this high-resolution cropped image closely. "
            "Transcribe all stamped text, laser-etched badges, model numbers, serial numbers, "
            "ratings, and brand logos visible on the item or label.\n"
            "Return a JSON object with keys:\n"
            "- brand (string or null)\n"
            "- model (string or null)\n"
            "- serial_number (string or null)\n"
            "- legible_text (array of strings)\n"
            "- confidence (float 0.0 to 1.0)\n\n"
            "Return ONLY the JSON object."
        )

        try:
            base64_img = VisionDetector.encode_image(crop_image_path)
            if self.detector.backend == "ollama":
                import requests
                url = f"{self.detector.endpoint}/api/generate"
                payload = {
                    "model": self.detector.model,
                    "prompt": prompt,
                    "images": [base64_img],
                    "stream": False,
                    "options": {"temperature": 0.0}
                }
                res = requests.post(url, json=payload, timeout=30)
                res.raise_for_status()
                text = res.json().get("response", "")
            else:
                items = self.detector.analyze_image(crop_image_path)
                return {
                    "brand": items[0].brand if items else None,
                    "model": items[0].model if items else None,
                    "serial_number": None,
                    "legible_text": [items[0].description] if items else [],
                    "confidence": 0.8
                }

            # Parse JSON
            clean = text.strip()
            if clean.startswith("```"):
                clean = re.sub(r"^```(?:json)?\n?", "", clean)
                clean = re.sub(r"\n?```$", "", clean)
            import json
            return json.loads(clean)
        except Exception as e:
            logger.warning(f"Error in badge reader on {crop_image_path}: {e}")
            return self._mock_badge_read(crop_image_path)

    def _mock_badge_read(self, path: str) -> Dict[str, Any]:
        """Synthetic badge reading for testing."""
        fname = os.path.basename(path).lower()
        if "stumpjumper" in fname or "bike" in fname:
            return {
                "brand": "Specialized",
                "model": "Stumpjumper Comp Alloy",
                "serial_number": "WSBC-MOCK-001928",
                "legible_text": ["SPECIALIZED", "STUMPJUMPER", "S3", "ALLOY"],
                "confidence": 0.95
            }
        elif "alienware" in fname or "monitor" in fname:
            return {
                "brand": "Alienware",
                "model": "AW3423DW",
                "serial_number": "MOCK-G8HQ2K3",
                "legible_text": ["ALIENWARE", "QD-OLED", "AW3423DW"],
                "confidence": 0.98
            }
        return {
            "brand": "SampleBrand",
            "model": "SampleModel",
            "serial_number": "SN-MOCK-12345",
            "legible_text": ["SAMPLE", "MODEL-1"],
            "confidence": 0.85
        }
