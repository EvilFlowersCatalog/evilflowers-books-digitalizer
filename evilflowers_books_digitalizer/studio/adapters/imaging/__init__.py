"""Imaging adapters: PageCleaner (ScanTailor) and ImageOps (Pillow)."""

from evilflowers_books_digitalizer.studio.adapters.imaging.pillow_ops import PillowImageOps
from evilflowers_books_digitalizer.studio.adapters.imaging.scantailor import ScanTailorCleaner

__all__ = ["PillowImageOps", "ScanTailorCleaner"]
