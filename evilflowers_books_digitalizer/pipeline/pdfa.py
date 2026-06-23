"""PDF/A conformance helpers for the archival output.

``recode_pdf`` already emits a file that validates as **PDF/A-3b** (it declares
``pdfaid:part=3``). PDF/A-3 differs from PDF/A-2 only in allowing embedded
arbitrary files; a book PDF embeds none, so it satisfies every PDF/A-2b rule
already. :func:`to_pdfa2b` therefore reaches PDF/A-2b by correcting just the
XMP part identifier — **no lossy re-encode**. Converting via Ghostscript or
OCRmyPDF instead would rasterise the JPEG2000 layers to PNG and inflate the
file ~2.7× (and Ghostscript's output failed validation), so we avoid them.

Verified with veraPDF 1.30.2 (encoder experiment, 2026-06-23).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pikepdf

logger = logging.getLogger(__name__)


def _has_embedded_files(pdf: pikepdf.Pdf) -> bool:
    names = pdf.Root.get("/Names")
    return names is not None and pikepdf.Name.EmbeddedFiles in names


def to_pdfa2b(pdf_path: Path) -> bool:
    """Re-declare a PDF/A-3b file as PDF/A-2b in place (lossless).

    Returns ``True`` when the part identifier was set, ``False`` when the file
    embeds attachments (a genuine PDF/A-3 feature that must stay part 3).
    """
    with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
        if _has_embedded_files(pdf):
            logger.warning("%s embeds files — keeping PDF/A-3b", pdf_path.name)
            return False
        with pdf.open_metadata() as meta:
            meta["pdfaid:part"] = "2"
            meta["pdfaid:conformance"] = "B"
        pdf.save(pdf_path)
    return True


def validate_pdfa(pdf_path: Path, flavour: str = "2b", verapdf: str = "verapdf") -> bool | None:
    """Validate with veraPDF if available; ``None`` when veraPDF isn't installed.

    Fail-safe: a missing validator must never fail the pipeline — validation is
    a quality gate run in batch/CI, not a hard production dependency.
    """
    try:
        result = subprocess.run(
            [verapdf, "--flavour", flavour, str(pdf_path)],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        logger.info("veraPDF not installed — skipping PDF/A validation of %s", pdf_path.name)
        return None
    compliant = 'isCompliant="true"' in result.stdout
    if not compliant:
        logger.warning("%s is NOT PDF/A-%s compliant", pdf_path.name, flavour)
    return compliant
