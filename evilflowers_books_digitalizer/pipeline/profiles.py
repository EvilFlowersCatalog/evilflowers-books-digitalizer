"""PDF output profiles for the MRC renderer.

A :class:`PdfProfile` is a declarative recipe for one ``recode_pdf`` invocation.
Adding a new output flavour is a matter of declaring a profile — the renderer
(:class:`~evilflowers_books_digitalizer.pipeline.steps.render.RenderPdf`) is
profile-driven and never needs to change (Open/Closed).

Two presets ship, decided empirically (the encoder sweep, 2026-06-23):

* :data:`DISTRIBUTION` — Mixed Raster Content with **JPEG (DCTDecode)** colour
  layers and a downsampled background. Opens fast in every viewer (Preview,
  PDF.js, mobile); the JBIG2 text mask keeps full resolution so glyphs stay
  razor-sharp. ~7.6× faster to decode than the old JPEG2000 output and smaller.
* :data:`ARCHIVAL` — **JPEG2000** MRC preservation master, converted to
  **PDF/A-2b** (PDF/A-1 forbids JPEG2000; A-2/A-3 allow it). Opened rarely, so
  JPEG2000's slow decode is acceptable in exchange for better compression.

The old single-output pipeline emitted full-resolution JPEG2000 layers that
many viewers render slowly or blank — see the ``jpeg2000-output-bug`` note.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal

ImageFormat = Literal["jpeg", "jpeg2000"]
MaskCompression = Literal["jbig2", "ccitt"]
DenoiseMask = Literal["none", "fast", "bregman"]

#: ``recode_pdf`` requires this binary on ``PATH`` for ``--mrc-image-format jpeg``.
JPEG_REQUIRES = "jpegoptim"


@dataclass(frozen=True, slots=True)
class PdfProfile:
    """One ``recode_pdf`` rendering recipe.

    ``pdfa`` (e.g. ``"2b"``) requests a post-render PDF/A conversion; ``None``
    leaves the recode output as-is. ``hq_pages`` keeps the named pages (covers,
    endpapers) at full quality — ``recode_pdf`` accepts negative indices.
    """

    name: str
    image_format: ImageFormat = "jpeg"
    bg_downsample: int | None = None
    fg_downsample: int | None = None
    downsample: int | None = None
    mask_compression: MaskCompression = "jbig2"
    denoise_mask: DenoiseMask = "fast"
    jpeg2000_encoder: str = "pillow"  # -J; only meaningful for image_format="jpeg2000"
    hq_pages: str | None = "1,2,3,4,-4,-3,-2,-1"
    bg_compression_flags: str | None = None
    fg_compression_flags: str | None = None
    linearize: bool = True
    pdfa: str | None = None

    def recode_args(self) -> list[str]:
        """Profile-specific ``recode_pdf`` flags (the renderer adds I/O + dpi)."""
        args: list[str] = ["--mrc-image-format", self.image_format]
        if self.image_format == "jpeg2000":
            args += ["-J", self.jpeg2000_encoder]
        args += ["--mask-compression", self.mask_compression]
        args += ["--denoise-mask", self.denoise_mask]
        if self.downsample is not None:
            args += ["--downsample", str(self.downsample)]
        if self.bg_downsample is not None:
            args += ["--bg-downsample", str(self.bg_downsample)]
        if self.fg_downsample is not None:
            args += ["--fg-downsample", str(self.fg_downsample)]
        if self.hq_pages:
            args += ["--hq-pages", self.hq_pages]
        if self.bg_compression_flags:
            args += [f"--bg-compression-flags={self.bg_compression_flags}"]
        if self.fg_compression_flags:
            args += [f"--fg-compression-flags={self.fg_compression_flags}"]
        return args

    def with_overrides(self, overrides: dict[str, Any]) -> PdfProfile:
        """Return a copy with any recognised keys from ``overrides`` applied."""
        known = {f for f in self.__dataclass_fields__ if f != "name"}
        clean = {k: v for k, v in overrides.items() if k in known and v is not None}
        return replace(self, **clean) if clean else self


#: Fast-everywhere access copy (encoder sweep winner: smallest *and* fastest).
DISTRIBUTION = PdfProfile(
    name="distribution",
    image_format="jpeg",
    bg_downsample=3,
    # fg_downsample left unset: recode_pdf 1.5.7 corrupts the foreground/text
    # layer when --fg-downsample is passed (rolls it ~50% horizontally + clips),
    # wrecking every text page. bg downsampling is unaffected. See the
    # jpeg2000-output-bug note for the controlled repro.
    denoise_mask="fast",
    linearize=True,
    pdfa=None,
)

#: Preservation master: JPEG2000 MRC → PDF/A-2b.
ARCHIVAL = PdfProfile(
    name="archival",
    image_format="jpeg2000",
    bg_downsample=2,
    jpeg2000_encoder="pillow",
    linearize=False,
    pdfa="2b",
)

PRESETS: dict[str, PdfProfile] = {p.name: p for p in (DISTRIBUTION, ARCHIVAL)}


def profiles_from_config(config: dict[str, Any]) -> list[PdfProfile]:
    """Build the enabled output profiles from the ``[render]`` config block.

    Shape::

        [render]
        outputs = ["distribution", "archival"]   # which presets to emit
        [render.distribution]                     # optional per-profile overrides
        bg_downsample = 4
        [render.archival]
        pdfa = "2u"

    Defaults to both presets when ``outputs`` is omitted.
    """
    render_cfg = config.get("render", {})
    names = render_cfg.get("outputs") or list(PRESETS)
    profiles: list[PdfProfile] = []
    for name in names:
        preset = PRESETS.get(name)
        if preset is None:
            raise ValueError(f"unknown render profile {name!r}; known: {sorted(PRESETS)}")
        profiles.append(preset.with_overrides(render_cfg.get(name, {})))
    return profiles
