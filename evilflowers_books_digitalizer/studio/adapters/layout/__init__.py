"""LayoutDetector adapters: reflow heuristics (default) and DocLayout-YOLO on ONNX (opt-in)."""

from evilflowers_books_digitalizer.studio.adapters.layout.heuristic import HeuristicLayout
from evilflowers_books_digitalizer.studio.adapters.layout.onnx import OnnxLayout

__all__ = ["HeuristicLayout", "OnnxLayout"]
