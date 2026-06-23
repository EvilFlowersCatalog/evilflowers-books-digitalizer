"""Cover templates: pluggable layouts rendered with Pillow.

A template turns a :class:`CoverSpec` + :class:`Palette` into a PIL image. Add a
new look by subclassing :class:`CoverTemplate` and registering it in
``TEMPLATES`` — no other code changes. Two ship by default:

* ``banner``  — coloured faculty band, large serif title, author, footer rule.
  A clean academic-library look (the default).
* ``minimal`` — centred serif title on a tinted ground with a single accent
  rule; understated.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from PIL import Image, ImageDraw

from evilflowers_books_digitalizer.covers.fonts import draw_block, fit_text, load_font
from evilflowers_books_digitalizer.covers.palette import RGB, Palette, shade


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
        return "  •  ".join(bits)


def _gradient(size: tuple[int, int], top: RGB, bottom: RGB) -> Image.Image:
    """Vertical gradient image from ``top`` to ``bottom``."""
    width, height = size
    column = [
        tuple(round(top[i] + (bottom[i] - top[i]) * (y / max(height - 1, 1))) for i in range(3))
        for y in range(height)
    ]
    base = Image.new("RGB", (1, height))
    base.putdata(column)
    return base.resize(size)


def _tracked(text: str, spaces: int = 1) -> str:
    return (" " * spaces).join(text.upper())


class CoverTemplate(ABC):
    """Base class for cover layouts."""

    name = "template"

    @abstractmethod
    def render(
        self, spec: CoverSpec, palette: Palette, size: tuple[int, int], fonts: dict[str, str]
    ) -> Image.Image: ...


class BannerTemplate(CoverTemplate):
    """Coloured faculty band on top, serif title, author, footer rule."""

    name = "banner"

    def render(self, spec, palette, size, fonts):
        width, height = size
        margin = round(width * 0.085)
        img = _gradient(size, palette.bg, shade(palette.bg, 0.93))
        draw = ImageDraw.Draw(img)

        # inset frame — subtle catalogue polish
        draw.rectangle(
            [margin // 2, margin // 2, width - margin // 2, height - margin // 2],
            outline=shade(palette.accent, 1.0),
            width=2,
        )

        # top accent band with the faculty name
        band_h = round(height * 0.20)
        draw.rectangle([0, 0, width, band_h], fill=palette.accent)
        eyebrow = load_font(fonts["sans_bold"], round(width * 0.022))
        draw.text(
            (width / 2, band_h * 0.34),
            _tracked("EvilFlowers Digital Library", 2),
            font=eyebrow,
            fill=shade(palette.accent, 1.6),
            anchor="mm",
        )
        if spec.faculty:
            fac_font = load_font(fonts["sans_bold"], round(width * 0.05))
            draw.text(
                (width / 2, band_h * 0.66),
                _tracked(spec.faculty, 1),
                font=fac_font,
                fill=palette.banner_text,
                anchor="mm",
            )

        # double rule under the band
        ry = band_h + round(height * 0.012)
        draw.line([margin, ry, width - margin, ry], fill=palette.accent, width=4)
        draw.line([margin, ry + 10, width - margin, ry + 10], fill=palette.accent, width=1)

        # footer first (so the title block knows its lower bound)
        footer_top = height - margin - round(height * 0.07)
        if spec.footer:
            draw.line(
                [margin, footer_top, width - margin, footer_top],
                fill=palette.muted,
                width=1,
            )
            foot_font = load_font(fonts["sans"], round(width * 0.026))
            draw.text(
                (width / 2, footer_top + round(height * 0.02)),
                spec.footer,
                font=foot_font,
                fill=palette.text,
                anchor="ma",
            )
        if spec.isbn:
            isbn_font = load_font(fonts["sans"], round(width * 0.02))
            draw.text(
                (width / 2, height - margin),
                f"ISBN {spec.isbn}",
                font=isbn_font,
                fill=palette.muted,
                anchor="md",
            )

        # title + subtitle + author, vertically centred in the open area
        content_top = ry + round(height * 0.05)
        content_bottom = footer_top - round(height * 0.04)
        inner_w = width - 2 * margin

        title_lines, title_font, title_lh = fit_text(
            draw,
            spec.title,
            fonts["serif_bold"],
            max_width=inner_w,
            max_height=round((content_bottom - content_top) * 0.6),
            max_size=round(width * 0.095),
            min_size=round(width * 0.03),
        )
        block_h = len(title_lines) * title_lh
        author_lines: list[str] = []
        if spec.authors:
            author_lines, author_font, author_lh = fit_text(
                draw,
                spec.authors,
                fonts["sans"],
                max_width=inner_w,
                max_height=round(height * 0.12),
                max_size=round(width * 0.04),
                min_size=round(width * 0.022),
            )
            block_h += round(height * 0.045) + len(author_lines) * author_lh
        sub_lines: list[str] = []
        if spec.subtitle:
            sub_lines, sub_font, sub_lh = fit_text(
                draw,
                spec.subtitle,
                fonts["serif_italic"],
                max_width=inner_w,
                max_height=round(height * 0.1),
                max_size=round(width * 0.04),
                min_size=round(width * 0.02),
            )
            block_h += round(height * 0.02) + len(sub_lines) * sub_lh

        y = content_top + max(0, (content_bottom - content_top - block_h) // 2)
        y = draw_block(
            draw,
            title_lines,
            title_font,
            top=y,
            line_height=title_lh,
            color=palette.text,
            center_x=width // 2,
        )
        if sub_lines:
            y += round(height * 0.02)
            y = draw_block(
                draw,
                sub_lines,
                sub_font,
                top=y,
                line_height=sub_lh,
                color=palette.muted,
                center_x=width // 2,
            )
        if author_lines:
            y += round(height * 0.045)
            draw_block(
                draw,
                author_lines,
                author_font,
                top=y,
                line_height=author_lh,
                color=palette.accent,
                center_x=width // 2,
            )
        return img


class MinimalTemplate(CoverTemplate):
    """Centred serif title on a tinted ground with one accent rule."""

    name = "minimal"

    def render(self, spec, palette, size, fonts):
        width, height = size
        margin = round(width * 0.1)
        img = Image.new("RGB", size, shade(palette.bg, 0.99))
        draw = ImageDraw.Draw(img)
        inner_w = width - 2 * margin

        if spec.faculty:
            eyebrow = load_font(fonts["sans_bold"], round(width * 0.026))
            draw.text(
                (width / 2, margin),
                _tracked(spec.faculty, 2),
                font=eyebrow,
                fill=palette.accent,
                anchor="ma",
            )

        title_lines, title_font, title_lh = fit_text(
            draw,
            spec.title,
            fonts["serif_bold"],
            max_width=inner_w,
            max_height=round(height * 0.4),
            max_size=round(width * 0.1),
            min_size=round(width * 0.035),
        )
        block_h = len(title_lines) * title_lh
        y = (height - block_h) // 2 - round(height * 0.04)
        y = draw_block(
            draw,
            title_lines,
            title_font,
            top=y,
            line_height=title_lh,
            color=palette.text,
            center_x=width // 2,
        )

        y += round(height * 0.03)
        draw.line(
            [width / 2 - round(width * 0.08), y, width / 2 + round(width * 0.08), y],
            fill=palette.accent,
            width=3,
        )
        y += round(height * 0.03)

        if spec.authors:
            author_font = load_font(fonts["sans"], round(width * 0.032))
            draw.text(
                (width / 2, y), spec.authors, font=author_font, fill=palette.muted, anchor="ma"
            )

        if spec.footer:
            foot_font = load_font(fonts["sans"], round(width * 0.024))
            draw.text(
                (width / 2, height - margin),
                spec.footer,
                font=foot_font,
                fill=palette.muted,
                anchor="md",
            )
        return img


TEMPLATES: dict[str, CoverTemplate] = {
    BannerTemplate.name: BannerTemplate(),
    MinimalTemplate.name: MinimalTemplate(),
}


def get_template(name: str) -> CoverTemplate:
    if name not in TEMPLATES:
        raise ValueError(f"unknown cover template {name!r}; have {sorted(TEMPLATES)}")
    return TEMPLATES[name]
