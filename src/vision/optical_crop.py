import os
import logging
from typing import Optional, Tuple
from PIL import Image
from .detector import BoundingBox

logger = logging.getLogger(__name__)


class OpticalCropEngine:
    """
    Extracts 1:1 lossless pixel patches from high-resolution originals.
    Prevents multimodal downsampling distortion that obliterates stamped badges.
    """

    def __init__(self, output_dir: str = "./data/optical_crops", padding_px: int = 40):
        self.output_dir = output_dir
        self.padding_px = padding_px
        os.makedirs(self.output_dir, exist_ok=True)

    def crop_item(
        self,
        image_path: str,
        box: Optional[BoundingBox],
        item_slug: str
    ) -> Optional[str]:
        """
        Crops an item from the native-resolution source image using normalized (0-1000)
        or pixel coordinates.
        """
        if not os.path.exists(image_path):
            logger.warning(f"Source image not found: {image_path}")
            return None

        try:
            with Image.open(image_path) as img:
                img_w, img_h = img.size

                if box:
                    # Convert normalized (0-1000) coordinates to actual pixel bounds
                    ymin = int((box.ymin / 1000.0) * img_h)
                    xmin = int((box.xmin / 1000.0) * img_w)
                    ymax = int((box.ymax / 1000.0) * img_h)
                    xmax = int((box.xmax / 1000.0) * img_w)
                else:
                    # Default center 60% crop if box not provided
                    xmin = int(img_w * 0.2)
                    ymin = int(img_h * 0.2)
                    xmax = int(img_w * 0.8)
                    ymax = int(img_h * 0.8)

                # Apply padding while respecting image boundaries
                xmin = max(0, xmin - self.padding_px)
                ymin = max(0, ymin - self.padding_px)
                xmax = min(img_w, xmax + self.padding_px)
                ymax = min(img_h, ymax + self.padding_px)

                crop = img.crop((xmin, ymin, xmax, ymax))

                output_filename = f"crop_{item_slug}_{os.path.splitext(os.path.basename(image_path))[0]}.jpg"
                output_path = os.path.join(self.output_dir, output_filename)

                # Save with high quality JPEG
                crop.save(output_path, "JPEG", quality=95)
                logger.info(f"Saved lossless optical crop: {output_path} ({crop.size[0]}x{crop.size[1]})")
                return output_path

        except Exception as e:
            logger.error(f"Error cropping {image_path}: {e}")
            return None

    def crop_badge_roi(
        self,
        cropped_image_path: str,
        roi_fraction: Tuple[float, float, float, float],
        badge_name: str
    ) -> Optional[str]:
        """
        Extracts a localized sub-region (e.g. bottom bracket, headtube, serial sticker)
        from an existing item crop.
        roi_fraction: (ymin_pct, xmin_pct, ymax_pct, xmax_pct) each between 0.0 and 1.0.
        """
        try:
            with Image.open(cropped_image_path) as img:
                w, h = img.size
                ymin, xmin, ymax, xmax = roi_fraction
                box = (int(xmin * w), int(ymin * h), int(xmax * w), int(ymax * h))
                badge_img = img.crop(box)

                # 2x bicubic upscale for enhanced OCR readability
                badge_img = badge_img.resize((badge_img.width * 2, badge_img.height * 2), Image.Resampling.BICUBIC)

                base_name = os.path.splitext(os.path.basename(cropped_image_path))[0]
                out_path = os.path.join(self.output_dir, f"{base_name}_badge_{badge_name}.jpg")
                badge_img.save(out_path, "JPEG", quality=98)
                return out_path
        except Exception as e:
            logger.error(f"Error extracting badge ROI: {e}")
            return None
