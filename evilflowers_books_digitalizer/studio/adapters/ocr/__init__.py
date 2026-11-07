"""OcrEngine adapters: Tesseract, one page per call.

The classic pipeline OCRs a whole book in one Tesseract run; the studio needs
single pages (per-page fan-out, operator re-runs), so this adapter runs one image
and returns its ``ocr_page``. Book-level hOCR assembly lives in :mod:`.hocr`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from evilflowers_books_digitalizer.studio.adapters.ocr.hocr import extract_page, page_text
from evilflowers_books_digitalizer.studio.ports import OcrPage, PortError


class TesseractOcr:
    def __init__(self, binary: str = "tesseract", timeout_s: int = 600) -> None:
        self.binary = binary
        self.timeout_s = timeout_s

    def recognize(self, image: Path, *, language: str, dpi: int) -> OcrPage:
        # resolve(): Leptonica/Tesseract choke on paths through macOS /tmp symlinks
        cmd = [self.binary, str(image.resolve()), "stdout", "-l", language, "--dpi", str(dpi),
               "hocr"]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=self.timeout_s)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PortError(f"tesseract could not run on {image.name}: {exc}") from exc
        if proc.returncode != 0:
            raise PortError(f"tesseract failed on {image.name} (exit {proc.returncode}): "
                            f"{proc.stderr.decode(errors='replace')[-1000:]}")
        page = extract_page(proc.stdout.decode("utf-8", errors="replace"))
        text, conf = page_text(page)
        return OcrPage(hocr=page, text=text, mean_conf=conf)


__all__ = ["TesseractOcr"]
