"""MRC step: cleaned page images -> compact searchable PDF (Tesseract + MRC).

Replaces the legacy ``AssemblePdf`` + ``OcrPdf`` pair for the ScanTailor
engine. Two stages:

1. **Tesseract** OCRs the pages into a multi-page hOCR file (and a plain-text
   sidecar derived from it — the input for enrichment/classification).
2. **archive-pdf-tools** (``recode_pdf``, the Internet Archive's production
   tool) assembles a Mixed Raster Content PDF: a JBIG2 1-bit text mask over
   smoothed, downsampled JPEG2000 foreground/background layers. Text stays
   razor sharp at a fraction of the size of plain-JPEG pages (notebook 06:
   10.1 MB -> 1.8 MB on the fad sample), and the background layer visually
   suppresses residual show-through.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

from evilflowers_books_digitalizer.pipeline.base import BookContext, PipelineStep

logger = logging.getLogger(__name__)


class _HocrTextExtractor(HTMLParser):
    """Plain text out of hOCR: words joined per line, pages separated by \\f."""

    def __init__(self) -> None:
        super().__init__()
        self.pages: list[list[str]] = []
        self._line: list[str] = []
        self._depth_in_word = 0

    def handle_starttag(self, tag, attrs):
        classes = dict(attrs).get("class", "")
        if "ocr_page" in classes:
            self.pages.append([])
        elif "ocr_line" in classes or "ocr_header" in classes or "ocr_caption" in classes:
            self._flush_line()
        elif "ocrx_word" in classes:
            self._depth_in_word = 1

    def handle_endtag(self, tag):
        if self._depth_in_word:
            self._depth_in_word = 0

    def handle_data(self, data):
        if self._depth_in_word and data.strip():
            self._line.append(data.strip())

    def _flush_line(self):
        if self._line and self.pages:
            self.pages[-1].append(" ".join(self._line))
        self._line = []

    def text(self) -> str:
        self._flush_line()
        return "\f".join("\n".join(lines) for lines in self.pages)


class MrcPdf(PipelineStep):
    """``ctx.tiffs`` -> searchable MRC ``artifacts['pdf']`` + ``artifacts['text']``."""

    name = "mrc"

    def __init__(
        self,
        language: str | None = None,  # None -> metadata['ocr_language'] or "slk"
        dpi: int = 300,
        mask_compression: str = "jbig2",
        jpeg2000_encoder: str = "pillow",
        bg_downsample: int | None = None,
        tesseract: str = "tesseract",
    ):
        self.language = language
        self.dpi = dpi
        self.mask_compression = mask_compression
        self.jpeg2000_encoder = jpeg2000_encoder
        self.bg_downsample = bg_downsample
        self.tesseract = tesseract

    def _recode_pdf(self) -> str:
        """recode_pdf lives in the same environment (archive-pdf-tools dep)."""
        candidate = Path(sys.executable).parent / "recode_pdf"
        if candidate.exists():
            return str(candidate)
        path = shutil.which("recode_pdf")
        if path is None:
            raise RuntimeError("recode_pdf not found — install the archive-pdf-tools dependency")
        return path

    def run(self, ctx: BookContext) -> BookContext:
        if not ctx.tiffs:
            raise ValueError(f"no pages for {ctx.slug} — run the scantailor step first")
        # resolve() matters: Leptonica chokes on paths through macOS symlinks (/tmp)
        pages = sorted(p.resolve() for p in ctx.tiffs)
        pages_dir = pages[0].parent
        suffix = pages[0].suffix
        if any(p.parent != pages_dir or p.suffix != suffix for p in pages):
            raise ValueError(f"pages of {ctx.slug} must share one directory and extension")
        # recode_pdf consumes a glob — it must resolve to exactly our page set
        stray = set(pages_dir.glob(f"*{suffix}")) - set(pages)
        if stray:
            raise ValueError(f"stray images next to pages of {ctx.slug}: {sorted(stray)[:3]}")

        ctx.output_dir.mkdir(parents=True, exist_ok=True)
        language = self.language or ctx.metadata.get("ocr_language", "slk")

        # 1. Tesseract -> multi-page hOCR
        list_file = ctx.work_dir / "pagelist.txt"
        list_file.write_text("\n".join(str(p) for p in pages))
        hocr_base = ctx.work_dir / "book"
        result = subprocess.run(
            [
                self.tesseract,
                str(list_file),
                str(hocr_base),
                "-l",
                language,
                "--dpi",
                str(self.dpi),
                "hocr",
            ],
            capture_output=True,
            text=True,
        )
        hocr = hocr_base.with_suffix(".hocr")
        if result.returncode != 0 or not hocr.exists():
            raise RuntimeError(
                f"tesseract failed for {ctx.slug} (exit {result.returncode}): "
                f"{result.stderr[-2000:]}"
            )

        # 2. plain-text sidecar from the hOCR
        sidecar = ctx.output_dir / f"{ctx.slug}.txt"
        extractor = _HocrTextExtractor()
        extractor.feed(re.sub(r"<\?xml[^>]*\?>", "", hocr.read_text()))
        sidecar.write_text(extractor.text())

        # 3. recode_pdf -> MRC PDF
        pdf = ctx.output_dir / f"{ctx.slug}.pdf"
        cmd = [
            self._recode_pdf(),
            "-I",
            str(pages_dir / f"*{suffix}"),
            "-T",
            str(hocr),
            "-o",
            str(pdf),
            "-D",
            str(self.dpi),
            "--mask-compression",
            self.mask_compression,
            "-J",
            self.jpeg2000_encoder,
        ]
        if self.bg_downsample:
            cmd += ["--bg-downsample", str(self.bg_downsample)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 or not pdf.exists():
            raise RuntimeError(
                f"recode_pdf failed for {ctx.slug} (exit {result.returncode}): "
                f"{result.stderr[-2000:]}"
            )

        logger.info(
            "%s: MRC PDF %.1f MB (%d pages, lang=%s)",
            ctx.slug,
            pdf.stat().st_size / 1e6,
            len(pages),
            language,
        )
        ctx.artifacts["pdf"] = pdf
        ctx.artifacts["text"] = sidecar
        ctx.artifacts["hocr"] = hocr  # downstream: FinalizePdf bookmarks/page labels
        return ctx
