"""Render step: cleaned pages + hOCR -> one searchable MRC PDF per profile.

``recode_pdf`` (Internet Archive's ``archive-pdf-tools``) assembles a Mixed
Raster Content PDF: a JBIG2 1-bit text mask over compressed foreground/
background layers, with the Tesseract hOCR as the hidden text layer.

The single OCR pass (:class:`~.ocr.OcrPages`) feeds every profile, so producing
both a fast **distribution** PDF and a **archival** master costs one Tesseract
run plus one cheap ``recode_pdf`` per profile. Each profile's output is exposed
as ``artifacts['pdf_<name>']``; ``artifacts['pdf']`` aliases the distribution
copy (or the first profile) as the default open target.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path

from evilflowers_books_digitalizer.pipeline.base import BookContext, PipelineStep
from evilflowers_books_digitalizer.pipeline.layout import Layout, pdf_path
from evilflowers_books_digitalizer.pipeline.profiles import JPEG_REQUIRES, PdfProfile

logger = logging.getLogger(__name__)


class RenderPdf(PipelineStep):
    """``ctx.tiffs`` + ``artifacts['hocr']`` -> one MRC PDF per profile."""

    name = "render"

    def __init__(
        self,
        profiles: list[PdfProfile],
        dpi: int = 300,
        layout: Layout = "flat",
        creator: str = "EvilFlowers Books Digitalizer",
        recode: str | None = None,
    ) -> None:
        if not profiles:
            raise ValueError("RenderPdf needs at least one PdfProfile")
        self.profiles = profiles
        self.dpi = dpi
        self.layout = layout
        self.creator = creator
        self._recode_override = recode

    def _metadata_args(self, ctx: BookContext) -> list[str]:
        """Bake document metadata into recode_pdf's XMP.

        Doing it here (rather than rewriting the XMP with pikepdf afterwards)
        keeps the archival PDF's PDF/A conformance intact — pikepdf XMP edits
        break recode_pdf's PDF/A metadata packet (verified with veraPDF).
        """
        args = ["--metadata-creatortool", self.creator]
        title = ctx.metadata.get("title")
        if title:
            args += ["--metadata-title", str(title)]
        authors = ctx.metadata.get("authors") or []
        if authors:
            args += ["--metadata-author", "; ".join(authors)]
        language = ctx.metadata.get("language")
        if language:
            args += ["--metadata-language", str(language)]
        return args

    def _recode_bin(self) -> str:
        if self._recode_override:
            return self._recode_override
        candidate = Path(sys.executable).parent / "recode_pdf"
        if candidate.exists():
            return str(candidate)
        path = shutil.which("recode_pdf")
        if path is None:
            raise RuntimeError("recode_pdf not found — install the archive-pdf-tools dependency")
        return path

    def _page_glob(self, ctx: BookContext) -> tuple[str, list[Path]]:
        """Validate the page set shares one dir + extension; return the glob."""
        # resolve(): Leptonica chokes on macOS /tmp symlink paths
        pages = sorted(p.resolve() for p in ctx.tiffs)
        if not pages:
            raise ValueError(f"no pages for {ctx.slug} — run the scantailor step first")
        pages_dir, suffix = pages[0].parent, pages[0].suffix
        if any(p.parent != pages_dir or p.suffix != suffix for p in pages):
            raise ValueError(f"pages of {ctx.slug} must share one directory and extension")
        stray = set(pages_dir.glob(f"*{suffix}")) - set(pages)
        if stray:
            raise ValueError(f"stray images next to pages of {ctx.slug}: {sorted(stray)[:3]}")
        return str(pages_dir / f"*{suffix}"), pages

    def run(self, ctx: BookContext) -> BookContext:
        hocr = ctx.artifacts.get("hocr")
        if hocr is None or not hocr.exists():
            raise ValueError(f"no hOCR for {ctx.slug} — run the ocr step first")
        if any(p.image_format == "jpeg" for p in self.profiles) and not shutil.which(JPEG_REQUIRES):
            raise RuntimeError(
                f"{JPEG_REQUIRES!r} not found — required by recode_pdf for JPEG MRC output "
                f"(brew install {JPEG_REQUIRES})"
            )

        glob, pages = self._page_glob(ctx)
        recode = self._recode_bin()
        meta_args = self._metadata_args(ctx)
        outputs: dict[str, dict[str, object]] = {}

        for profile in self.profiles:
            pdf = pdf_path(ctx.output_dir, ctx.source, ctx.slug, profile.name, self.layout)
            pdf.parent.mkdir(parents=True, exist_ok=True)
            cmd = [recode, "-I", glob, "-T", str(hocr), "-o", str(pdf), "-D", str(self.dpi)]
            cmd += profile.recode_args() + meta_args
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0 or not pdf.exists():
                raise RuntimeError(
                    f"recode_pdf ({profile.name}) failed for {ctx.slug} "
                    f"(exit {result.returncode}): {result.stderr[-2000:]}"
                )
            ctx.artifacts[f"pdf_{profile.name}"] = pdf
            outputs[profile.name] = {
                "path": str(pdf),
                "mb": round(pdf.stat().st_size / 1e6, 3),
                "image_format": profile.image_format,
                "pdfa": profile.pdfa,
                "linearize": profile.linearize,
            }
            logger.info(
                "%s: %s PDF %.2f MB (%s)",
                ctx.slug, profile.name, pdf.stat().st_size / 1e6, profile.image_format,
            )

        ctx.metadata["outputs"] = outputs
        ctx.metadata.setdefault("n_pages", len(pages))
        # default open target: distribution if present, else the first profile
        default = "distribution" if "pdf_distribution" in ctx.artifacts else self.profiles[0].name
        ctx.artifacts["pdf"] = ctx.artifacts[f"pdf_{default}"]
        return ctx
