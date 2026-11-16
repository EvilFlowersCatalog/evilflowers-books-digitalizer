"""Imaging adapters: leaf planning (spread detection) and the API's renditions (DZI, PDF pages).

Cleaning, erasing and the page previews are evilflowers-scan-service's (#6).
"""

from evilflowers_books_digitalizer.studio.adapters.imaging.spreads import SpreadPlanner

__all__ = ["SpreadPlanner"]
