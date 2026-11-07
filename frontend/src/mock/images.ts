/**
 * Mock page renditions as SVG data URLs, drawn from the fixture regions so the
 * viewer overlays line up. Layers differ the way real ones do: the original frame
 * half is tinted and skewed, the spread shows both halves on the scanner bed, the
 * PDF render drops regions whose action is "remove" once a re-run applied it.
 */

import type { ImageLayer, RegionOut } from "../api/types";
import { FRAME_H, FRAME_W, PAGE_H, PAGE_W, type MockPage } from "./fixtures";

type Box = readonly [number, number, number, number];
const INK = "#2b2722";
const GREY = "#55504a";

function lines([x0, y0, x1, y1]: Box, seed: number, opts: { bullet?: boolean; size?: number } = {}): string {
  const size = opts.size ?? 34;
  const step = size + 24;
  const out: string[] = [];
  let i = 0;
  for (let y = y0 + 8; y + size <= y1; y += step, i++) {
    const last = y + step + size > y1;
    const indent = opts.bullet ? 70 : i === 0 ? 60 : 0;
    const w = (x1 - x0 - indent) * (last ? 0.35 + ((seed + i) % 5) / 10 : 0.93 + ((seed * 3 + i) % 7) / 100);
    if (opts.bullet && i === 0) out.push(`<circle cx="${x0 + 22}" cy="${y + size / 2}" r="9" fill="${INK}"/>`);
    out.push(`<rect x="${x0 + indent}" y="${y}" width="${Math.max(40, w)}" height="${size * 0.62}" rx="4" fill="${GREY}" opacity="0.78"/>`);
  }
  return out.join("");
}

function arrow(x: number, y: number, dx: number, dy: number, color: string): string {
  const len = Math.hypot(dx, dy);
  const ux = dx / len;
  const uy = dy / len;
  const hx = x + dx - ux * 50;
  const hy = y + dy - uy * 50;
  return (
    `<line x1="${x}" y1="${y}" x2="${hx}" y2="${hy}" stroke="${color}" stroke-width="12"/>` +
    `<polygon points="${x + dx},${y + dy} ${hx - uy * 26},${hy + ux * 26} ${hx + uy * 26},${hy - ux * 26}" fill="${color}"/>`
  );
}

function picture([x0, y0, x1, y1]: Box, colorCard: boolean): string {
  const w = x1 - x0;
  const h = y1 - y0;
  if (colorCard) {
    const colors = ["#1b1b1b", "#5b5b5b", "#9a9a9a", "#d8d8d8", "#c0392b", "#27ae60", "#2980b9", "#f1c40f", "#8e44ad", "#e67e22", "#16a085", "#ecf0f1"];
    return colors
      .map((c, i) => `<rect x="${x0 + (i % 4) * (w / 4)}" y="${y0 + Math.floor(i / 4) * (h / 3)}" width="${w / 4 - 20}" height="${h / 3 - 20}" fill="${c}"/>`)
      .join("");
  }
  const cx = x0 + w * 0.3;
  const cy = y0 + h * 0.75;
  return (
    `<rect x="${x0}" y="${y0}" width="${w}" height="${h}" fill="#f3efe6" stroke="${INK}" stroke-width="4"/>` +
    `<line x1="${x0 + 60}" y1="${cy}" x2="${x1 - 60}" y2="${cy}" stroke="${GREY}" stroke-width="5" stroke-dasharray="20 14"/>` +
    arrow(cx, cy, w * 0.45, -h * 0.5, "#1f4e8c") +
    arrow(cx, cy, w * 0.5, 0, "#8c1f1f") +
    `<text x="${cx + w * 0.22}" y="${cy - h * 0.3}" font-family="Georgia, serif" font-style="italic" font-size="90" fill="${INK}">a</text>` +
    `<text x="${cx + w * 0.25}" y="${cy + 100}" font-family="Georgia, serif" font-style="italic" font-size="90" fill="${INK}">b</text>`
  );
}

function table([x0, y0, x1, y1]: Box): string {
  const rows = 6;
  const cols = 4;
  const out = [`<rect x="${x0}" y="${y0}" width="${x1 - x0}" height="${y1 - y0}" fill="none" stroke="${INK}" stroke-width="5"/>`];
  for (let r = 1; r < rows; r++) out.push(`<line x1="${x0}" x2="${x1}" y1="${y0 + ((y1 - y0) * r) / rows}" y2="${y0 + ((y1 - y0) * r) / rows}" stroke="${INK}" stroke-width="${r === 1 ? 5 : 2}"/>`);
  for (let c = 1; c < cols; c++) out.push(`<line y1="${y0}" y2="${y1}" x1="${x0 + ((x1 - x0) * c) / cols}" x2="${x0 + ((x1 - x0) * c) / cols}" stroke="${INK}" stroke-width="2"/>`);
  for (let r = 0; r < rows; r++)
    for (let c = 0; c < cols; c++) {
      const cx = x0 + ((x1 - x0) * c) / cols + 40;
      const cy = y0 + ((y1 - y0) * r) / rows + 60;
      out.push(`<rect x="${cx}" y="${cy}" width="${(x1 - x0) / cols - 120 - ((r * c * 37) % 90)}" height="30" rx="4" fill="${GREY}" opacity="${r === 0 ? 1 : 0.7}"/>`);
    }
  return out.join("");
}

function stamp([x0, y0, x1, y1]: Box): string {
  const cx = (x0 + x1) / 2;
  const cy = (y0 + y1) / 2;
  const r = Math.min(x1 - x0, y1 - y0) / 2 - 20;
  return (
    `<g opacity="0.82" transform="rotate(-14 ${cx} ${cy})">` +
    `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="#b3261e" stroke-width="16"/>` +
    `<circle cx="${cx}" cy="${cy}" r="${r - 48}" fill="none" stroke="#b3261e" stroke-width="6"/>` +
    `<text x="${cx}" y="${cy - 20}" text-anchor="middle" font-family="Arial, sans-serif" font-weight="700" font-size="64" fill="#b3261e">KNIŽNICA</text>` +
    `<text x="${cx}" y="${cy + 60}" text-anchor="middle" font-family="Arial, sans-serif" font-weight="700" font-size="56" fill="#b3261e">FEI STU</text>` +
    `</g>`
  );
}

function regionSvg(r: RegionOut, colorCard: boolean, seed: number): string {
  const b = r.bbox;
  switch (r.kind) {
    case "title":
      return lines(b, seed, { size: 120 });
    case "section_header":
      return lines(b, seed, { size: 70 });
    case "list_item":
      return lines(b, seed, { bullet: true });
    case "page_header":
    case "page_footer":
      return lines(b, seed, { size: 30 });
    case "formula":
      return `<text x="${(b[0] + b[2]) / 2}" y="${(b[1] + b[3]) / 2 + 40}" text-anchor="middle" font-family="Georgia, 'Times New Roman', serif" font-style="italic" font-size="120" fill="${INK}">${
        r.id % 2 ? "|a × b| = a b sin α" : "F = m a = m dv/dt"
      }</text>`;
    case "table":
      return table(b);
    case "picture":
      return picture(b, colorCard);
    case "stamp":
    case "sticker":
      return stamp(b);
    case "handwriting":
      return `<path d="M${b[0]} ${b[3] - 60} q 80 -120 160 0 t 160 0 t 160 0 t 160 0" fill="none" stroke="#2741a8" stroke-width="10" opacity="0.8"/>`;
    default:
      return lines(b, seed);
  }
}

function pageSvg(page: MockPage, regions: RegionOut[], layer: "clean" | "pdf"): string {
  const removed = (r: RegionOut) => r.status === "rejected" || (r.action === "remove" && (layer === "pdf" || page.version > 1) && r.status === "confirmed");
  const body = regions
    .filter((r) => !removed(r))
    .map((r, i) => regionSvg(r, page.page_type === "Color Card", page.index * 31 + i))
    .join("");
  const paper = layer === "pdf" ? "#ffffff" : "#fdfcf8";
  const mark = `<text x="${PAGE_W - 60}" y="${PAGE_H - 60}" text-anchor="end" font-family="monospace" font-size="34" fill="#b9b2a4">leaf ${page.index} · v${page.version}${layer === "pdf" ? " · pdf" : ""}</text>`;
  return `<rect width="${PAGE_W}" height="${PAGE_H}" fill="${paper}"/>${body}${mark}`;
}

function wrap(w: number, h: number, inner: string): string {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">${inner}</svg>`;
}

function frameHalf(page: MockPage, regions: RegionOut[]): string {
  const hw = FRAME_W / 2;
  const ox = (hw - PAGE_W) / 2;
  const oy = (FRAME_H - PAGE_H) / 2;
  const content = pageSvg(page, regions, "clean").replace('fill="#fdfcf8"', 'fill="#efe5cf"');
  return (
    `<rect width="${hw}" height="${FRAME_H}" fill="#3a3632"/>` +
    `<g transform="translate(${ox} ${oy}) rotate(${page.skew_angle * 2} ${PAGE_W / 2} ${PAGE_H / 2})">${content}` +
    `<rect width="${PAGE_W}" height="${PAGE_H}" fill="#7a5a20" opacity="0.06"/></g>`
  );
}

const cache = new Map<string, string>();

export function mockPageImage(page: MockPage, regions: RegionOut[], layer: ImageLayer): string {
  const key = `${page.id}:${layer}:${page.version}:${regions.map((r) => `${r.id}${r.status}${r.action}`).join(",")}`;
  const hit = cache.get(key);
  if (hit) return hit;
  let svg: string;
  if (layer === "original") svg = wrap(FRAME_W / 2, FRAME_H, frameHalf(page, regions));
  else if (layer === "spread") {
    const half = frameHalf(page, regions);
    svg = wrap(FRAME_W, FRAME_H, `${half}<g transform="translate(${FRAME_W / 2} 0)">${half}</g><line x1="${FRAME_W / 2}" x2="${FRAME_W / 2}" y1="0" y2="${FRAME_H}" stroke="#111" stroke-width="18"/>`);
  } else svg = wrap(PAGE_W, PAGE_H, pageSvg(page, regions, layer));
  const url = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
  cache.set(key, url);
  return url;
}
