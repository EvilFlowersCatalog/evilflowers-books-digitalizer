"""Cover templates: pluggable layouts rendered with Pillow.

A template turns a :class:`CoverSpec` + :class:`Palette` (+ the faculty logo)
into a PIL image. Add a new look by subclassing :class:`CoverTemplate` and
registering it in ``TEMPLATES`` — no other code changes.

One ships by default:

* ``stu`` — a quiet, neutral page: the black STU/faculty logo at the top, the
  title set large in the middle for maximum legibility, the author below a short
  faculty-coloured hairline, and a small publisher/year footer. No EvilFlowers
  branding — the only mark is STU's, used gently.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from PIL import Image, ImageDraw

from evilflowers_books_digitalizer.covers.fonts import draw_block, fit_text, load_font
from evilflowers_books_digitalizer.covers.palette import Palette


@dataclass
class CoverSpec:
    """Everything a template needs to draw one cover."""

    title: str
    subtitle: str | None = None
    authors: str = ""  # already-joined author line
    year: int | None = None
    publisher: str | None = None
    isbn: str | None = None
    faculty: str = ""

    @property
    def footer(self) -> str:
        bits = [str(b) for b in (self.publisher, self.year) if b]
        return "   ·   ".join(bits)


class CoverTemplate(ABC):
    """Base class for cover layouts."""

    name = "template"

    @abstractmethod
    def render(
        self,
        spec: CoverSpec,
        palette: Palette,
        size: tuple[int, int],
        fonts: dict[str, str],
        logo: Image.Image | None = None,
    ) -> Image.Image: ...


def _paste_logo(
    img: Image.Image, logo: Image.Image, *, center_x: int, top: int, target_w: int
) -> int:
    """Paste ``logo`` scaled to ``target_w`` centred at ``center_x``; return its bottom y."""
    scale = target_w / logo.width
    w, h = target_w, max(1, round(logo.height * scale))
    resized = logo.resize((w, h), Image.LANCZOS)
    x = round(center_x - w / 2)
    img.paste(resized, (x, top), resized)
    return top + h


class StuTemplate(CoverTemplate):
    """Neutral page · top logo · large centred title · accent rule · author."""

    name = "stu"

    def render(self, spec, palette, size, fonts, logo=None):
        width, height = size
        margin = round(width * 0.11)
        inner_w = width - 2 * margin
        center_x = width // 2

        img = Image.new("RGB", size, palette.bg)
        draw = ImageDraw.Draw(img)

        # --- faculty / STU logo, gently, at the top -------------------------
        logo_bottom = round(height * 0.11)
        if logo is not None:
            logo_w = min(round(width * 0.46), logo.width)
            logo_bottom = _paste_logo(
                img, logo, center_x=center_x, top=round(height * 0.10), target_w=logo_w
            )

        # --- footer first (so the title block knows its lower bound) --------
        foot_baseline = height - margin
        if spec.isbn:
            isbn_font = load_font(fonts["sans"], round(width * 0.019))
            draw.text(
                (center_x, foot_baseline),
                f"ISBN {spec.isbn}",
                font=isbn_font,
                fill=palette.muted,
                anchor="md",
            )
            foot_baseline -= round(height * 0.035)
        footer_top = foot_baseline
        if spec.footer:
            foot_font = load_font(fonts["sans"], round(width * 0.024))
            draw.text(
                (center_x, foot_baseline),
                spec.footer,
                font=foot_font,
                fill=palette.muted,
                anchor="md",
            )
            footer_top = foot_baseline - round(height * 0.03)

        # --- title + subtitle + author, centred in the open middle ---------
        content_top = logo_bottom + round(height * 0.10)
        content_bottom = footer_top - round(height * 0.04)
        avail_h = content_bottom - content_top

        title_lines, title_font, title_lh = fit_text(
            draw,
            spec.title,
            fonts["sans_bold"],
            max_width=inner_w,
            max_height=round(avail_h * 0.6),
            max_size=round(width * 0.082),
            min_size=round(width * 0.032),
            line_spacing=1.16,
        )
        block_h = len(title_lines) * title_lh

        sub_lines: list[str] = []
        sub_font = sub_lh = None
        if spec.subtitle:
            sub_lines, sub_font, sub_lh = fit_text(
                draw,
                spec.subtitle,
                fonts["serif_italic"],
                max_width=inner_w,
                max_height=round(avail_h * 0.18),
                max_size=round(width * 0.038),
                min_size=round(width * 0.022),
            )
            block_h += round(height * 0.022) + len(sub_lines) * sub_lh

        rule_gap = round(height * 0.032)
        block_h += 2 * rule_gap  # accent rule above the author

        author_lines: list[str] = []
        author_font = author_lh = None
        if spec.authors:
            author_lines, author_font, author_lh = fit_text(
                draw,
                spec.authors,
                fonts["sans"],
                max_width=inner_w,
                max_height=round(avail_h * 0.18),
                max_size=round(width * 0.036),
                min_size=round(width * 0.020),
            )
            block_h += len(author_lines) * author_lh

        # vertically centre the whole block in the open area
        y = content_top + max(0, (avail_h - block_h) // 2)

        y = draw_block(
            draw, title_lines, title_font, top=y, line_height=title_lh,
            color=palette.text, center_x=center_x,
        )
        if sub_lines:
            y += round(height * 0.022)
            y = draw_block(
                draw, sub_lines, sub_font, top=y, line_height=sub_lh,
                color=palette.muted, center_x=center_x,
            )

        # short faculty-coloured hairline between title and author
        y += rule_gap
        rule_w = round(width * 0.085)
        draw.line(
            [center_x - rule_w, y, center_x + rule_w, y],
            fill=palette.accent_ink,
            width=max(2, round(height * 0.0022)),
        )
        y += rule_gap

        if author_lines:
            draw_block(
                draw, author_lines, author_font, top=y, line_height=author_lh,
                color=palette.text, center_x=center_x,
            )
        return img


TEMPLATES: dict[str, CoverTemplate] = {
    StuTemplate.name: StuTemplate(),
}


def get_template(name: str) -> CoverTemplate:
    if name not in TEMPLATES:
        raise ValueError(f"unknown cover template {name!r}; have {sorted(TEMPLATES)}")
    return TEMPLATES[name]
