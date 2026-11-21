import base64
import io
import json
import logging
import mimetypes
import os
import re
from typing import Any, Dict, List, Optional

from PIL import Image, ImageOps
from pydantic import BaseModel, Field
import requests

logger = logging.getLogger(__name__)


class BoundingBox(BaseModel):
    ymin: int = Field(0, description="Normalized ymin (0-1000)")
    xmin: int = Field(0, description="Normalized xmin (0-1000)")
    ymax: int = Field(1000, description="Normalized ymax (0-1000)")
    xmax: int = Field(1000, description="Normalized xmax (0-1000)")

    def clamped(self) -> "BoundingBox":
        y0 = max(0, min(1000, int(self.ymin)))
        x0 = max(0, min(1000, int(self.xmin)))
        y1 = max(0, min(1000, int(self.ymax)))
        x1 = max(0, min(1000, int(self.xmax)))
        if y1 <= y0:
            y0, y1 = 0, 1000
        if x1 <= x0:
            x0, x1 = 0, 1000
        return BoundingBox(ymin=y0, xmin=x0, ymax=y1, xmax=x1)


class DetectedItem(BaseModel):
    item_name: str
    category: str
    brand: Optional[str] = "Unknown"
    model: Optional[str] = None
    serial_number: Optional[str] = None
    room: Optional[str] = "General"
    condition: str = "Good"
    estimated_replacement_value_usd: float = 0.0
    description: str = ""
    box: Optional[BoundingBox] = None
    confidence: float = 0.85
    source_image: str = ""
    original_image: str = ""
    badge_verified: bool = False


def strip_json_fences(text: str) -> str:
    """Strip optional <think>...</think> blocks and markdown code fences."""
    clean = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*\n?", "", clean)
        clean = re.sub(r"\n?```\s*$", "", clean)
    return clean.strip()


class VisionDetector:
    """
    Multimodal Vision Detector for isolating individual assets from messy room photos.
    Supports local Ollama (default Gemma 4 E2B with keep_alive & think=False),
    OpenAI-compatible / vLLM endpoints, Gemini REST API, and explicit 'mock' mode.
    """

    def __init__(
        self,
        backend: str = "ollama",
        endpoint: str = "http://localhost:11434",
        model: str = "gemma4:e2b-it-q4_K_M",
        temperature: float = 0.1,
        timeout: int = 90,
        keep_alive: str = "15m",
        max_detect_dim: int = 1280,
    ):
        self.backend = backend.lower()
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.keep_alive = keep_alive
        self.max_detect_dim = max_detect_dim

    @staticmethod
    def encode_image(image_path: str, max_dim: Optional[int] = None) -> tuple[str, str]:
        """
        Load an image with EXIF orientation applied, strip EXIF metadata,
        optionally downscale its longest side to max_dim for Stage-1 detection,
        and return (base64_str, mime_type).
        """
        with Image.open(image_path) as raw:
            img = ImageOps.exif_transpose(raw)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            elif img.mode == "L":
                img = img.convert("RGB")

            if max_dim and max(img.size) > max_dim:
                img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)

            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=90)
            return base64.b64encode(buf.getvalue()).decode("utf-8"), "image/jpeg"

    @staticmethod
    def encode_raw_bytes(image_path: str) -> tuple[str, str]:
        mime, _ = mimetypes.guess_type(image_path)
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8"), mime or "image/jpeg"

    def _get_system_prompt(self) -> str:
        return (
            "You are an expert home insurance property appraiser and computer vision specialist. "
            "Analyze the provided photo and catalog ALL distinct valuable physical assets "
            "(electronics, bicycles, monitors, appliances, furniture, power tools, audio gear, cameras, luggage). "
            "Ignore structural room features (walls, plain floors, empty shelves). "
            'Return ONLY a valid JSON object of the form {"items": [...]} where each item has keys:\n'
            "- item_name (string: precise, descriptive name)\n"
            "- category (string: e.g. Electronics, Bicycles, Audio, Tools, Kitchen, Furniture, Photography)\n"
            "- brand (string: manufacturer name if visible, else 'Unknown')\n"
            "- model (string or null: exact model/series if legible)\n"
            "- condition (string: Mint, Excellent, Good, Fair)\n"
            "- estimated_replacement_value_usd (float: realistic replacement cost in USD)\n"
            "- description (string: concise visual details, visible markings, accessories)\n"
            "- confidence (float 0.0 to 1.0)\n"
            "- box_2d (array of 4 ints [ymin, xmin, ymax, xmax] scaled 0-1000)\n\n"
            "Return ONLY the JSON object. No prose, no markdown fences."
        )

    def complete_vision(
        self,
        image_b64: str,
        prompt: str,
        mime_type: str = "image/jpeg",
        temperature: Optional[float] = None,
        expect_json: bool = True,
        max_tokens: int = 1024,
    ) -> str:
        """Unified vision completion call across ollama, openai-compat/vllm, and gemini."""
        temp = self.temperature if temperature is None else temperature

        if self.backend == "ollama":
            url = f"{self.endpoint}/api/generate"
            payload: Dict[str, Any] = {
                "model": self.model,
                "prompt": prompt,
                "images": [image_b64],
                "stream": False,
                "keep_alive": self.keep_alive,
                "think": False,
                "options": {
                    "temperature": temp,
                    "num_predict": max_tokens,
                    "repeat_penalty": 1.1,
                },
            }
            if expect_json:
                payload["format"] = "json"
            res = requests.post(url, json=payload, timeout=self.timeout)
            res.raise_for_status()
            return res.json().get("response", "")

        if self.backend in ("openai", "openai-compat", "vllm", "litellm"):
            url = f"{self.endpoint}/v1/chat/completions"
            api_key = os.getenv("OPENAI_API_KEY", "dummy-local-key")
            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
            payload = {
                "model": self.model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_b64}"}},
                        ],
                    }
                ],
                "temperature": temp,
                "max_tokens": max_tokens,
            }
            if expect_json:
                payload["response_format"] = {"type": "json_object"}
            res = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
            res.raise_for_status()
            return res.json()["choices"][0]["message"]["content"]

        if self.backend == "gemini":
            api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
            if not api_key:
                raise ValueError("GEMINI_API_KEY is required when backend='gemini'.")
            base = self.endpoint if "generativelanguage.googleapis.com" in self.endpoint else "https://generativelanguage.googleapis.com"
            url = f"{base}/v1beta/models/{self.model}:generateContent?key={api_key}"
            gen_cfg: Dict[str, Any] = {"temperature": temp, "maxOutputTokens": max_tokens}
            if expect_json:
                gen_cfg["responseMimeType"] = "application/json"
            payload = {
                "contents": [
                    {
                        "parts": [
                            {"text": prompt},
                            {"inline_data": {"mime_type": mime_type, "data": image_b64}},
                        ]
                    }
                ],
                "generationConfig": gen_cfg,
            }
            res = requests.post(url, json=payload, timeout=self.timeout)
            res.raise_for_status()
            data = res.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]

        raise ValueError(f"Unsupported vision backend: {self.backend}")

    def analyze_image(self, image_path: str, room: str = "General") -> List[DetectedItem]:
        """Process an image and extract itemized inventory candidates. Never fabricates mock data on error."""
        if not os.path.exists(image_path):
            raise FileNotFoundError(f"Image not found at path: {image_path}")

        if self.backend == "mock":
            items = self._mock_detect(image_path)
            for it in items:
                it.room = room
                it.original_image = image_path
            return items

        image_b64, mime_type = self.encode_image(image_path, max_dim=self.max_detect_dim)
        raw_text = self.complete_vision(image_b64, self._get_system_prompt(), mime_type=mime_type, expect_json=True)
        return self._parse_json_response(raw_text, image_path, room=room)

    def _parse_json_response(self, text: str, image_path: str, room: str = "General") -> List[DetectedItem]:
        clean_text = strip_json_fences(text)
        try:
            items_raw = json.loads(clean_text)
        except json.JSONDecodeError as e:
            # Attempt recovery if model wrapped JSON in leading/trailing prose
            m = re.search(r"(\{.*\}|\[.*\])", clean_text, flags=re.DOTALL)
            if not m:
                raise ValueError(f"Vision model returned invalid JSON on {image_path}: {e}. Raw: {clean_text[:200]}") from e
            items_raw = json.loads(m.group(1))

        if isinstance(items_raw, dict):
            if "items" in items_raw and isinstance(items_raw["items"], list):
                items_raw = items_raw["items"]
            elif "item_name" in items_raw:
                items_raw = [items_raw]
            else:
                items_raw = []
        elif not isinstance(items_raw, list):
            items_raw = []

        detected: List[DetectedItem] = []
        for d in items_raw:
            if not isinstance(d, dict):
                continue
            box = None
            raw_box = d.get("box_2d")
            if isinstance(raw_box, list) and len(raw_box) == 4:
                try:
                    box = BoundingBox(
                        ymin=int(raw_box[0]),
                        xmin=int(raw_box[1]),
                        ymax=int(raw_box[2]),
                        xmax=int(raw_box[3]),
                    ).clamped()
                except (TypeError, ValueError):
                    box = None

            val_raw = d.get("estimated_replacement_value_usd")
            try:
                est_val = float(str(val_raw).replace("$", "").replace(",", "")) if val_raw not in (None, "") else 0.0
            except ValueError:
                est_val = 0.0

            conf_raw = d.get("confidence", 0.85)
            try:
                conf = max(0.0, min(1.0, float(conf_raw)))
            except (TypeError, ValueError):
                conf = 0.85

            detected.append(
                DetectedItem(
                    item_name=str(d.get("item_name") or "Unidentified Item").strip(),
                    category=str(d.get("category") or "General").strip(),
                    brand=str(d.get("brand") or "Unknown").strip(),
                    model=str(d["model"]).strip() if d.get("model") not in (None, "", "null") else None,
                    serial_number=str(d["serial_number"]).strip() if d.get("serial_number") not in (None, "", "null") else None,
                    room=room,
                    condition=str(d.get("condition") or "Good").strip(),
                    estimated_replacement_value_usd=est_val,
                    description=str(d.get("description") or "").strip(),
                    box=box,
                    confidence=conf,
                    source_image=os.path.basename(image_path),
                    original_image=image_path,
                )
            )
        return self._suppress_duplicate_boxes(detected)

    @staticmethod
    def _box_iou(a: BoundingBox, b: BoundingBox) -> float:
        inter_x0 = max(a.xmin, b.xmin)
        inter_y0 = max(a.ymin, b.ymin)
        inter_x1 = min(a.xmax, b.xmax)
        inter_y1 = min(a.ymax, b.ymax)
        inter = max(0, inter_x1 - inter_x0) * max(0, inter_y1 - inter_y0)
        if inter == 0:
            return 0.0
        area_a = max(1, (a.xmax - a.xmin) * (a.ymax - a.ymin))
        area_b = max(1, (b.xmax - b.xmin) * (b.ymax - b.ymin))
        return inter / float(area_a + area_b - inter)

    @classmethod
    def _suppress_duplicate_boxes(
        cls,
        items: List[DetectedItem],
        iou_threshold: float = 0.85,
        max_items_per_image: int = 15,
    ) -> List[DetectedItem]:
        """Drop repetition-loop duplicate boxes (common on bookshelves with small SLMs)."""
        kept: List[DetectedItem] = []
        for it in items:
            is_dup = False
            if it.box is not None:
                for prev in kept:
                    if prev.box is not None and cls._box_iou(it.box, prev.box) >= iou_threshold:
                        is_dup = True
                        break
            if not is_dup:
                kept.append(it)
            if len(kept) >= max_items_per_image:
                break
        return kept

    def _mock_detect(self, image_path: str) -> List[DetectedItem]:
        """Provides realistic synthetic detections strictly when backend='mock'."""
        basename = os.path.basename(image_path).lower()
        if "bike" in basename:
            return [
                DetectedItem(
                    item_name="Mountain Bike (Full-Suspension)",
                    category="Sports & Recreation | Bicycles",
                    brand="Specialized",
                    model="Stumpjumper Comp Alloy",
                    condition="Excellent",
                    estimated_replacement_value_usd=3200.0,
                    description="Green full-suspension mountain bike with dropper post and disc brakes.",
                    box=BoundingBox(ymin=150, xmin=100, ymax=850, xmax=900),
                    source_image=os.path.basename(image_path),
                )
            ]
        if "desk" in basename or "workstation" in basename:
            return [
                DetectedItem(
                    item_name="Ultrawide Curved Gaming Monitor",
                    category="Electronics | Displays & Monitors",
                    brand="Alienware",
                    model="AW3423DW 34-Inch OLED",
                    condition="Mint",
                    estimated_replacement_value_usd=899.99,
                    description="34-inch curved QD-OLED gaming monitor on monitor arm.",
                    box=BoundingBox(ymin=200, xmin=150, ymax=600, xmax=800),
                    source_image=os.path.basename(image_path),
                ),
                DetectedItem(
                    item_name="Flagship Desktop AI Workstation",
                    category="Electronics | Computers & Workstations",
                    brand="Custom Build",
                    model="RTX 4090 / Ryzen 7",
                    condition="Excellent",
                    estimated_replacement_value_usd=4800.0,
                    description="High-tower chassis with high-end GPU and cooling suite.",
                    box=BoundingBox(ymin=300, xmin=700, ymax=900, xmax=950),
                    source_image=os.path.basename(image_path),
                ),
            ]
        return [
            DetectedItem(
                item_name="Electronics Asset",
                category="Electronics",
                brand="Generic Brand",
                model="Model X",
                condition="Good",
                estimated_replacement_value_usd=250.0,
                description="Asset identified from photo.",
                box=BoundingBox(ymin=100, xmin=100, ymax=900, xmax=900),
                source_image=os.path.basename(image_path),
            )
        ]
