"""PaddleOCR-VL as a CPU HTTP service (``POST /v1/formulas``) for the studio's worker-ai.

Run: ``python -m vlm_service --model-dir /models/PaddleOCR-VL-1.6`` (the ``vlm``
image) or ``python -m evilflowers_books_digitalizer.vlm_service`` (dev). The
package imports only stdlib/Pillow/FastAPI; torch and transformers load lazily
in :class:`.engine.PaddleEngine`. All imports inside are relative so the same
directory works as the top-level package ``vlm_service``.
"""
