/**
 * RegionOut <-> Annotorious ImageAnnotation, and the per-region drawing style.
 *
 * Regions are `[x0, y0, x1, y1]` in cleaned-page pixels, which is exactly the
 * image coordinate system Annotorious uses on the cleaned layer, so no scaling.
 * Annotorious (PIXI) cannot dash a stroke; disputed outlines are drawn by the
 * viewer's own SVG overlay, here they only get a stronger fill.
 */

import type { DrawingStyle, ImageAnnotation, Rectangle } from "@annotorious/openseadragon";
import type { BBoxList, RegionOut } from "../../api/types";
import { bboxToRect, KIND_COLOR, rectToBBox } from "../../lib/regions";

export function regionToAnnotation(region: RegionOut): ImageAnnotation {
  const { x, y, w, h } = bboxToRect(region.bbox);
  const id = String(region.id);
  // ShapeType is a runtime enum; its string value keeps this module free of the
  // Annotorious runtime (unit tests run in node without a canvas).
  const selector: Rectangle = {
    type: "RECTANGLE" as Rectangle["type"],
    geometry: { x, y, w, h, bounds: { minX: x, minY: y, maxX: x + w, maxY: y + h } },
  };
  return { id, bodies: [], target: { annotation: id, selector } };
}

/** The bbox of an edited/drawn annotation (null for non-rectangles). */
export function annotationBBox(a: ImageAnnotation, width?: number | null, height?: number | null): BBoxList | null {
  const g = a.target.selector.geometry as Partial<{ x: number; y: number; w: number; h: number }>;
  if (g.x === undefined || g.y === undefined || g.w === undefined || g.h === undefined) return null;
  return rectToBBox({ x: g.x, y: g.y, w: g.w, h: g.h }, width, height);
}

export function regionStyle(region: RegionOut | undefined, selected: boolean): DrawingStyle {
  if (!region) return { fill: "#2f6fd6", fillOpacity: 0.15, stroke: "#2f6fd6", strokeWidth: 2 };
  const color = KIND_COLOR[region.kind];
  const disputed = region.status === "disputed";
  return {
    fill: color,
    fillOpacity: selected ? 0.28 : disputed ? 0.18 : 0.08,
    stroke: color,
    strokeOpacity: disputed ? 0 : 0.95,
    strokeWidth: selected ? 3 : 2,
  };
}
