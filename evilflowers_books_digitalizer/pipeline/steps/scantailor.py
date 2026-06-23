"""ScanTailor step: raw scanner frames -> clean, uniform single-page TIFFs.

ScanTailor's content detection reliably removes facing-page slivers and binding
shadows, ``--normalize-illumination`` flattens gray backgrounds, and the
page-layout stage gives every page of a book identical physical size with real
margins. Spread pre-splitting still uses our own ``imaging.analyze_spread``.

Spread splitting stays in our code (``imaging.analyze_spread``): the
``scantailor-deviant-cli`` single-pass batch mode detects the gutter
correctly but fails to propagate the cut to content detection (the right
page receives the whole spread, the left page comes out blank), so frames
are pre-split at the detected gutter and ScanTailor runs with ``--layout=1``.

Requires the ``scantailor-deviant-cli`` binary (see ``Dockerfile`` or the
build notes in the README); the path is configurable via ``pipeline.toml``.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

import cv2

from evilflowers_books_digitalizer.imaging import analyze_spread, load_bgr
from evilflowers_books_digitalizer.pipeline.base import BookContext, PipelineStep

logger = logging.getLogger(__name__)


class ScanTailorScans(PipelineStep):
    """``ctx.tiffs`` (raw frames) -> ScanTailor-cleaned pages in ``work_dir/st_pages``.

    ``color_mode``:
    * ``"mixed"`` — text binarized (crisp black-on-white, kills bleed-through),
      detected picture zones keep grayscale/color; best default for text books
    * ``"color_grayscale"`` — tones kept everywhere; pair with the DocRes step
      for covers / photo-heavy books
    """

    name = "scantailor"

    def __init__(
        self,
        binary: str = "scantailor-deviant-cli",
        color_mode: str = "mixed",
        margins_mm: float = 8.0,
        dewarping: str = "auto",
        normalize_illumination: bool = True,
        despeckle: str = "cautious",
        dpi: int = 300,
        output_dpi: int | None = None,
        split: bool = True,
    ):
        if color_mode not in ("mixed", "color_grayscale", "black_and_white"):
            raise ValueError(f"unknown color_mode: {color_mode!r}")
        self.binary = binary
        self.color_mode = color_mode
        self.margins_mm = margins_mm
        self.dewarping = dewarping
        self.normalize_illumination = normalize_illumination
        self.despeckle = despeckle
        self.dpi = dpi
        # supersampled binarization: 600dpi output from 300dpi input gives much
        # smoother glyph edges in mixed/B&W modes (notebook 07); pointless for
        # grayscale output. None -> same as input dpi.
        self.output_dpi = output_dpi or dpi
        self.split = split

    def _resolve_binary(self) -> str:
        path = shutil.which(self.binary)
        if path is None:
            raise RuntimeError(
                f"ScanTailor binary not found: {self.binary!r} — install it "
                "(see Dockerfile / README) or set [scantailor] binary in pipeline.toml"
            )
        return path

    def _split_frames(self, ctx: BookContext) -> tuple[list, int]:
        """Pre-split spreads at the detected gutter (see module docstring).

        Unreadable frames (truncated/corrupt scans — there are some in the
        corpus) are skipped with a warning rather than aborting the whole book;
        the dropped frame names are recorded in ``metadata['skipped_frames']``.
        """
        halves_dir = ctx.work_dir / "halves"
        halves_dir.mkdir(parents=True, exist_ok=True)
        outputs, n_spreads = [], 0
        skipped: list[str] = []
        for frame_path in sorted(ctx.tiffs):
            try:
                frame = load_bgr(frame_path)
            except ValueError:
                logger.warning("%s: skipping unreadable frame %s", ctx.slug, frame_path.name)
                skipped.append(frame_path.name)
                continue
            if self.split:
                layout = analyze_spread(frame)
                if layout.is_spread:
                    gx = layout.gutter_x
                    halves = [frame[:, :gx], frame[:, gx:]]
                    n_spreads += 1
                else:
                    halves = [frame]
            else:
                halves = [frame]
            for side, half in enumerate(halves):
                out = halves_dir / f"{frame_path.stem}_{side}.tif"
                cv2.imwrite(str(out), half)
                outputs.append(out)
        if skipped:
            ctx.metadata["skipped_frames"] = skipped
        if not outputs:
            raise ValueError(
                f"no readable frames for {ctx.slug} — all {len(ctx.tiffs)} were unreadable"
            )
        return outputs, n_spreads

    def run(self, ctx: BookContext) -> BookContext:
        if not ctx.tiffs:
            raise ValueError(f"no scans for {ctx.slug} — run the download step first")
        binary = self._resolve_binary()

        halves, n_spreads = self._split_frames(ctx)
        pages_dir = ctx.work_dir / "st_pages"
        if pages_dir.exists():
            shutil.rmtree(pages_dir)
        pages_dir.mkdir(parents=True)

        cmd = [
            binary,
            "--layout=1",  # frames are pre-split, see module docstring
            "--deskew=auto",
            "--content-detection=normal",
            f"--margins={self.margins_mm}",
            "--alignment=center",
            f"--dpi={self.dpi}",
            f"--output-dpi={self.output_dpi}",
            f"--color-mode={self.color_mode}",
            "--white-margins",  # mandatory: margins show raw scanner bed otherwise
            f"--despeckle={self.despeckle}",
            f"--dewarping={self.dewarping}",
            "--start-filter=1",
            "--end-filter=6",
            "--tiff-compression=deflate",
        ]
        if self.normalize_illumination:
            cmd.append("--normalize-illumination")
        cmd += [str(halves[0].parent), str(pages_dir)]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"scantailor failed for {ctx.slug} (exit {result.returncode}): "
                f"{result.stderr[-2000:]}"
            )

        pages = sorted(pages_dir.glob("*.tif"))
        if len(pages) != len(halves):
            raise RuntimeError(
                f"scantailor page count mismatch for {ctx.slug}: "
                f"{len(halves)} inputs -> {len(pages)} outputs"
            )

        logger.info(
            "%s: %d frames -> %d pages (%d spreads split, %s)",
            ctx.slug,
            len(ctx.tiffs),
            len(pages),
            n_spreads,
            self.color_mode,
        )
        ctx.metadata["n_frames"] = len(ctx.tiffs)
        ctx.metadata["n_pages"] = len(pages)
        ctx.tiffs = pages
        return ctx
