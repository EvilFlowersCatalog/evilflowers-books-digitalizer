/**
 * Region presentation and geometry helpers shared by the viewer, the region list
 * and the tests. Colours are hex because Annotorious (PIXI) only accepts hex/rgb.
 */

import type { BBoxList, RegionAction, RegionKind, RegionOut, RegionPatch } from "../api/types";

type Hex = `#${string}`;

export const KIND_COLOR: Record<RegionKind, Hex> = {
  text: "#3b82c4",
  title: "#7c4dcc",
  section_header: "#9b5de5",
  list_item: "#4f9fd8",
  caption: "#2fa39a",
  footnote: "#6b8fb0",
  formula: "#e0a100",
  table: "#1f9d55",
  picture: "#d9467a",
  page_header: "#8a8f98",
  page_footer: "#8a8f98",
  stamp: "#e5482f",
  sticker: "#f07b22",
  handwriting: "#b35c2e",
  other: "#7a7a7a",
};

export const KIND_LABEL: Record<RegionKind, string> = {
  text: "Text",
  title: "Title",
  section_header: "Section header",
  list_item: "List item",
  caption: "Caption",
  footnote: "Footnote",
  formula: "Formula",
  table: "Table",
  picture: "Picture",
  page_header: "Page header",
  page_footer: "Page footer",
  stamp: "Stamp",
  sticker: "Sticker",
  handwriting: "Handwriting",
  other: "Other",
};

export const ACTION_LABEL: Record<RegionAction, string> = {
  keep: "Keep",
  remove: "Remove",
  as_image: "As image",
  reocr: "Re-OCR",
};

const NON_CONTENT: ReadonlySet<RegionKind> = new Set(["stamp", "sticker", "handwriting"]);

/** Mirror of domain.default_action: relabelling also picks the sensible action. */
export function defaultAction(kind: RegionKind): RegionAction {
  if (NON_CONTENT.has(kind)) return "remove";
  if (kind === "formula" || kind === "table") return "reocr";
  if (kind === "picture") return "as_image";
  return "keep";
}

/** Relabel = new kind + its default action, confirmed (the operator decided). */
export const relabel = (kind: RegionKind): RegionPatch => ({ kind, action: defaultAction(kind), status: "confirmed" });

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export function bboxToRect([x0, y0, x1, y1]: BBoxList): Rect {
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
}

/** Annotorious rect -> integer bbox clamped to the page (domain BBox rejects empty boxes). */
export function rectToBBox(rect: Rect, width?: number | null, height?: number | null): BBoxList {
  const maxX = width ?? Number.POSITIVE_INFINITY;
  const maxY = height ?? Number.POSITIVE_INFINITY;
  const x0 = Math.max(0, Math.round(rect.x));
  const y0 = Math.max(0, Math.round(rect.y));
  const x1 = Math.min(maxX, Math.max(x0 + 1, Math.round(rect.x + rect.w)));
  const y1 = Math.min(maxY, Math.max(y0 + 1, Math.round(rect.y + rect.h)));
  return [x0, y0, x1, y1];
}

export const visibleRegions = (regions: RegionOut[]) => regions.filter((r) => r.status !== "rejected");
export const disputedRegions = (regions: RegionOut[]) => regions.filter((r) => r.status === "disputed");

/** The region `delta` steps from the selection in list order (rejected ones skipped, wraps). */
export function stepRegion(regions: RegionOut[], selectedId: number | null, delta: -1 | 1): number | null {
  const live = visibleRegions(regions);
  if (live.length === 0) return null;
  const at = live.findIndex((r) => r.id === selectedId);
  if (at < 0) return (delta > 0 ? live[0] : live[live.length - 1])!.id;
  return live[(at + delta + live.length) % live.length]!.id;
}

export type DisputeTarget = { region: number } | { page: number } | null;

/**
 * Where `d` goes: the next disputed region after the selection on this page, else
 * the next page (in leaf order, wrapping) that still has disputes, else nowhere.
 */
export function nextDispute(
  regions: RegionOut[],
  selectedId: number | null,
  pages: { id: number; index: number; disputed?: number }[],
  pageId: number,
): DisputeTarget {
  const disputed = disputedRegions(regions);
  const after = disputed.findIndex((r) => r.id === selectedId) + 1;
  if (after < disputed.length) return { region: disputed[after]!.id };
  const ordered = [...pages].sort((a, b) => a.index - b.index);
  const here = ordered.findIndex((p) => p.id === pageId);
  const rest = [...ordered.slice(here + 1), ...ordered.slice(0, Math.max(0, here))];
  const page = rest.find((p) => (p.disputed ?? 0) > 0);
  if (page) return { page: page.id };
  return disputed[0] ? { region: disputed[0].id } : null;   // wrap on the only page with disputes
}
