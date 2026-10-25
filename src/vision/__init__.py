"""
Vision processing and multimodal object extraction modules.
"""

from .detector import VisionDetector, DetectedItem
from .optical_crop import OpticalCropEngine
from .badge_reader import BadgeReader

__all__ = ["VisionDetector", "DetectedItem", "OpticalCropEngine", "BadgeReader"]
