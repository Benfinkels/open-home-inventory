"""
Unit tests for Vision Subsystem: BoundingBox normalization,
DetectedItem schema, and OpticalCropEngine patch extraction.
"""

import os
import pytest
from PIL import Image
from src.vision.detector import BoundingBox, DetectedItem, VisionDetector
from src.vision.optical_crop import OpticalCropEngine


def test_bounding_box_and_detected_item():
    box = BoundingBox(ymin=100, xmin=150, ymax=450, xmax=600)
    assert box.ymin == 100
    assert box.xmin == 150
    assert box.ymax == 450
    assert box.xmax == 600

    item = DetectedItem(
        item_name="GeForce RTX 4090",
        category="Electronics",
        brand="NVIDIA",
        model="RTX 4090 FE",
        serial_number="SN-4090-TEST",
        estimated_replacement_value_usd=1599.99,
        box=box,
        confidence=0.98
    )
    assert item.item_name == "GeForce RTX 4090"
    assert item.brand == "NVIDIA"
    assert item.serial_number == "SN-4090-TEST"
    assert item.box.ymin == 100


def test_optical_crop_extraction(tmp_path):
    # Create synthetic test image (800x600)
    img_path = tmp_path / "test_room.jpg"
    img = Image.new("RGB", (800, 600), color=(73, 109, 137))
    img.save(img_path)

    crop_dir = tmp_path / "crops"
    engine = OpticalCropEngine(output_dir=str(crop_dir), padding_px=20)

    box = BoundingBox(ymin=100, xmin=100, ymax=300, xmax=400)
    item = DetectedItem(
        item_name="Monitor",
        category="Electronics",
        box=box,
        source_image=str(img_path)
    )

    crop_path = engine.crop_item(str(img_path), box, "test_monitor_crop")
    assert crop_path is not None
    assert os.path.exists(crop_path)

    cropped = Image.open(crop_path)
    assert cropped.width > 0
    assert cropped.height > 0
