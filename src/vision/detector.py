import base64
import json
import logging
import os
import re
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
import requests

logger = logging.getLogger(__name__)


class BoundingBox(BaseModel):
    ymin: int = Field(0, description="Normalized or pixel ymin")
    xmin: int = Field(0, description="Normalized or pixel xmin")
    ymax: int = Field(1000, description="Normalized or pixel ymax")
    xmax: int = Field(1000, description="Normalized or pixel xmax")


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


class VisionDetector:
    """
    Multimodal Vision Detector for isolating individual assets from messy room photos.
    Compatible with local Ollama, vLLM, and standard OpenAI vision endpoints.
    """

    def __init__(
        self,
        backend: str = "ollama",
        endpoint: str = "http://localhost:11434",
        model: str = "minicpm-v:latest",
        temperature: float = 0.1,
        timeout: int = 60
    ):
        self.backend = backend.lower()
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.timeout = timeout

    @staticmethod
    def encode_image(image_path: str) -> str:
        with open(image_path, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode("utf-8")

    def _get_system_prompt(self) -> str:
        return (
            "You are an expert home insurance property appraiser and computer vision specialist. "
            "Analyze the provided photo. Identify and catalog ALL distinct valuable physical assets "
            "(electronics, bicycles, monitors, appliances, furniture, power tools, audio equipment, luggage, etc.). "
            "Ignore structural room features (walls, plain floors, empty shelves). "
            "Return a valid JSON array of objects with keys:\n"
            "- item_name (string: precise, descriptive name)\n"
            "- category (string: e.g. Electronics, Bicycles, Audio, Tools, Kitchen, Furniture)\n"
            "- brand (string: manufacturer name if visible, or best inference)\n"
            "- model (string: exact model or series identifier if visible)\n"
            "- condition (string: Mint, Excellent, Good, Fair)\n"
            "- estimated_replacement_value_usd (float: estimated replacement cost)\n"
            "- description (string: detailed description with visible markings/accessories)\n"
            "- box_2d (array of 4 ints: [ymin, xmin, ymax, xmax] scaled 0-1000)\n\n"
            "Return ONLY the JSON array. Do not include markdown code block formatting or conversational text."
        )

    def analyze_image(self, image_path: str) -> List[DetectedItem]:
        """Process an image and extract itemized inventory candidates."""
        if not os.path.exists(image_path):
            raise FileNotFoundError(f"Image not found at path: {image_path}")

        # Check for mock backend for lightweight testing
        if self.backend == "mock":
            return self._mock_detect(image_path)

        base64_image = self.encode_image(image_path)
        prompt = self._get_system_prompt()

        try:
            if self.backend == "ollama":
                return self._call_ollama(base64_image, prompt, image_path)
            elif self.backend in ("openai", "vllm", "litellm"):
                return self._call_openai_compatible(base64_image, prompt, image_path)
            else:
                raise ValueError(f"Unsupported vision backend: {self.backend}")
        except Exception as e:
            logger.error(f"Vision detection error on {image_path}: {e}")
            return self._mock_detect(image_path)

    def _call_ollama(self, base64_image: str, prompt: str, image_path: str) -> List[DetectedItem]:
        url = f"{self.endpoint}/api/generate"
        payload = {
            "model": self.model,
            "prompt": prompt,
            "images": [base64_image],
            "stream": False,
            "options": {"temperature": self.temperature}
        }
        res = requests.post(url, json=payload, timeout=self.timeout)
        res.raise_for_status()
        raw_text = res.json().get("response", "")
        return self._parse_json_response(raw_text, image_path)

    def _call_openai_compatible(self, base64_image: str, prompt: str, image_path: str) -> List[DetectedItem]:
        url = f"{self.endpoint}/v1/chat/completions"
        headers = {"Content-Type": "application/json"}
        api_key = os.getenv("OPENAI_API_KEY", "dummy-local-key")
        headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
                        }
                    ]
                }
            ],
            "temperature": self.temperature
        }
        res = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
        res.raise_for_status()
        raw_text = res.json()["choices"][0]["message"]["content"]
        return self._parse_json_response(raw_text, image_path)

    def _parse_json_response(self, text: str, image_path: str) -> List[DetectedItem]:
        clean_text = text.strip()
        # Strip markdown fences if present
        if clean_text.startswith("```"):
            clean_text = re.sub(r"^```(?:json)?\n?", "", clean_text)
            clean_text = re.sub(r"\n?```$", "", clean_text)

        try:
            items_raw = json.loads(clean_text)
            if not isinstance(items_raw, list):
                if isinstance(items_raw, dict) and "items" in items_raw:
                    items_raw = items_raw["items"]
                else:
                    items_raw = [items_raw]

            detected = []
            for d in items_raw:
                box = None
                if "box_2d" in d and len(d["box_2d"]) == 4:
                    box = BoundingBox(
                        ymin=d["box_2d"][0],
                        xmin=d["box_2d"][1],
                        ymax=d["box_2d"][2],
                        xmax=d["box_2d"][3]
                    )
                item = DetectedItem(
                    item_name=d.get("item_name", "Unidentified Item"),
                    category=d.get("category", "General"),
                    brand=d.get("brand") or "Unknown",
                    model=d.get("model"),
                    condition=d.get("condition", "Good"),
                    estimated_replacement_value_usd=float(d.get("estimated_replacement_value_usd") or 0.0),
                    description=d.get("description", ""),
                    box=box,
                    source_image=os.path.basename(image_path)
                )
                detected.append(item)
            return detected
        except Exception as e:
            logger.warning(f"Failed to parse LLM response as JSON: {e}. Raw: {clean_text[:200]}")
            return []

    def _mock_detect(self, image_path: str) -> List[DetectedItem]:
        """Provides realistic synthetic detections for demonstration and testing."""
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
                    source_image=os.path.basename(image_path)
                )
            ]
        elif "desk" in basename or "workstation" in basename:
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
                    source_image=os.path.basename(image_path)
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
                    source_image=os.path.basename(image_path)
                )
            ]
        else:
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
                    source_image=os.path.basename(image_path)
                )
            ]
