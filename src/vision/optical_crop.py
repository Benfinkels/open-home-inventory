import logging
import os
import re
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageOps

from .detector import BoundingBox, DetectedItem

logger = logging.getLogger(__name__)


def sanitize_slug(text: str, max_len: int = 48) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", text.strip()).strip("_").lower()
    return (slug or "item")[:max_len]


def _save_exif_stripped(img: Image.Image, output_path: str, quality: int = 95) -> None:
    """Save RGB image with all EXIF/GPS metadata stripped."""
    rgb = img.convert("RGB") if img.mode != "RGB" else img
    # Creating a fresh copy without info/exif guarantees zero GPS or camera metadata leaks
    clean = Image.new("RGB", rgb.size)
    clean.paste(rgb)
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    clean.save(output_path, "JPEG", quality=quality)


class OpticalCropEngine:
    """
    Extracts 1:1 native-resolution pixel patches from full-resolution originals
    after applying EXIF orientation, and strips EXIF/GPS metadata on save.
    """

    def __init__(self, output_dir: str = "./data/optical_crops", padding_px: int = 40):
        self.output_dir = output_dir
        self.padding_px = padding_px
        os.makedirs(self.output_dir, exist_ok=True)

    def crop_item(
        self,
        image_path: str,
        box: Optional[BoundingBox],
        item_slug: str,
    ) -> Optional[str]:
        """Crop an item from the native-resolution EXIF-transposed source image using 0-1000 box coords."""
        if not os.path.exists(image_path):
            logger.warning(f"Source image not found: {image_path}")
            return None

        try:
            with Image.open(image_path) as raw:
                img = ImageOps.exif_transpose(raw)
                img_w, img_h = img.size

                if box:
                    b = box.clamped()
                    ymin = int((b.ymin / 1000.0) * img_h)
                    xmin = int((b.xmin / 1000.0) * img_w)
                    ymax = int((b.ymax / 1000.0) * img_h)
                    xmax = int((b.xmax / 1000.0) * img_w)
                else:
                    xmin = int(img_w * 0.2)
                    ymin = int(img_h * 0.2)
                    xmax = int(img_w * 0.8)
                    ymax = int(img_h * 0.8)

                xmin = max(0, xmin - self.padding_px)
                ymin = max(0, ymin - self.padding_px)
                xmax = min(img_w, xmax + self.padding_px)
                ymax = min(img_h, ymax + self.padding_px)

                if xmax <= xmin or ymax <= ymin:
                    logger.warning(f"Degenerate crop box on {image_path}: {(xmin, ymin, xmax, ymax)}")
                    return None

                crop = img.crop((xmin, ymin, xmax, ymax))
                safe_slug = sanitize_slug(item_slug)
                stem = sanitize_slug(os.path.splitext(os.path.basename(image_path))[0])
                output_filename = f"crop_{safe_slug}_{stem}.jpg"
                output_path = os.path.join(self.output_dir, output_filename)

                _save_exif_stripped(crop, output_path, quality=95)
                logger.info(f"Saved EXIF-stripped 1:1 crop: {output_path} ({crop.size[0]}x{crop.size[1]})")
                return output_path

        except Exception as e:
            logger.error(f"Error cropping {image_path}: {e}")
            return None

    def crop_badge_roi(
        self,
        cropped_image_path: str,
        roi_fraction: Tuple[float, float, float, float],
        badge_name: str,
    ) -> Optional[str]:
        """Extract and 2x upscale a localized badge/serial sub-region from an existing crop."""
        try:
            with Image.open(cropped_image_path) as raw:
                img = ImageOps.exif_transpose(raw)
                w, h = img.size
                ymin, xmin, ymax, xmax = roi_fraction
                box = (int(xmin * w), int(ymin * h), int(xmax * w), int(ymax * h))
                badge_img = img.crop(box)
                badge_img = badge_img.resize((badge_img.width * 2, badge_img.height * 2), Image.Resampling.BICUBIC)

                base_name = sanitize_slug(os.path.splitext(os.path.basename(cropped_image_path))[0])
                out_path = os.path.join(self.output_dir, f"{base_name}_badge_{sanitize_slug(badge_name)}.jpg")
                _save_exif_stripped(badge_img, out_path, quality=98)
                return out_path
        except Exception as e:
            logger.error(f"Error extracting badge ROI: {e}")
            return None

    def render_overlay(
        self,
        image_path: str,
        items: List[DetectedItem],
        output_path: str,
        max_dim: int = 1400,
    ) -> Optional[str]:
        """Save an EXIF-stripped annotated image showing Stage-1 bounding boxes and labels."""
        if not os.path.exists(image_path):
            return None
        try:
            with Image.open(image_path) as raw:
                img = ImageOps.exif_transpose(raw).convert("RGB")
                if max_dim and max(img.size) > max_dim:
                    img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
                w, h = img.size
                draw = ImageDraw.Draw(img)
                palette = [(16, 185, 129), (37, 99, 235), (245, 158, 11), (239, 68, 68), (139, 92, 246)]

                for idx, it in enumerate(items):
                    if not it.box:
                        continue
                    b = it.box.clamped()
                    x0, y0 = int(b.xmin / 1000.0 * w), int(b.ymin / 1000.0 * h)
                    x1, y1 = int(b.xmax / 1000.0 * w), int(b.ymax / 1000.0 * h)
                    col = palette[idx % len(palette)]
                    draw.rectangle([x0, y0, x1, y1], outline=col, width=4)
                    label = f"{it.brand} {it.model or it.item_name}".strip()
                    if it.estimated_replacement_value_usd > 0:
                        label += f" (${it.estimated_replacement_value_usd:,.0f})"
                    tx, ty = x0 + 4, max(4, y0 - 18)
                    bbox = draw.textbbox((tx, ty), label)
                    draw.rectangle([bbox[0] - 3, bbox[1] - 2, bbox[2] + 3, bbox[3] + 2], fill=col)
                    draw.text((tx, ty), label, fill=(255, 255, 255))

                _save_exif_stripped(img, output_path, quality=92)
                return output_path
        except Exception as e:
            logger.error(f"Error rendering overlay for {image_path}: {e}")
            return None
