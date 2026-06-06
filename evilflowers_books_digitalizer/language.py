"""Per-book OCR language detection.

Not every book on the shares is Slovak (English and Czech textbooks are
common in the collections), and OCRing with the wrong language model
measurably hurts recognition. Strategy: quick Tesseract pass over a few
middle pages, statistical language detection on the resulting text
(`langdetect`), then a Tesseract language combo for the real OCR run.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from langdetect import DetectorFactory, detect_langs
from langdetect.lang_detect_exception import LangDetectException

logger = logging.getLogger(__name__)

DetectorFactory.seed = 0  # deterministic detection

#: langdetect ISO 639-1 -> Tesseract traineddata codes (collection languages)
ISO_TO_TESSERACT = {"sk": "slk", "cs": "ces", "en": "eng", "de": "deu", "ru": "rus"}


def quick_ocr_text(image_path: Path, lang: str = "slk+eng", timeout: int = 120) -> str:
    """Fast single-page OCR purely for language detection."""
    result = subprocess.run(
        ["tesseract", str(image_path), "stdout", "-l", lang, "--psm", "3"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
    )
    return result.stdout if result.returncode == 0 else ""


def detect_book_language(
    pages: list[Path],
    sample: int = 3,
    default_iso: str = "sk",
) -> tuple[str, str]:
    """Detect a book's language from a few middle pages.

    Returns ``(iso_code, tesseract_langs)`` — e.g. ``("sk", "slk+eng")`` or
    ``("en", "eng+slk")``. The secondary language is always kept: Slovak books
    contain English abstracts/references and vice versa.
    """
    if not pages:
        return default_iso, _tesseract_combo(default_iso)

    middle = len(pages) // 2
    step = max(len(pages) // (sample + 1), 1)
    sampled = pages[middle::step][:sample] or pages[:sample]

    text = "\n".join(quick_ocr_text(page) for page in sampled)
    if len(text.strip()) < 200:
        logger.warning("too little text for language detection, defaulting to %s", default_iso)
        return default_iso, _tesseract_combo(default_iso)

    try:
        candidates = detect_langs(text)
    except LangDetectException:
        return default_iso, _tesseract_combo(default_iso)

    for candidate in candidates:
        if candidate.lang in ISO_TO_TESSERACT:
            logger.info("detected language %s (p=%.2f)", candidate.lang, candidate.prob)
            return candidate.lang, _tesseract_combo(candidate.lang)
    logger.warning("no supported language in %s, defaulting to %s", candidates, default_iso)
    return default_iso, _tesseract_combo(default_iso)


def _tesseract_combo(iso: str) -> str:
    """Tesseract language string: detected primary + safety secondary."""
    primary = ISO_TO_TESSERACT.get(iso, "slk")
    if primary == "eng":
        return "eng+slk"
    if primary == "slk":
        return "slk+eng"
    return f"{primary}+slk+eng"
