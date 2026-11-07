/**
 * Region rectangles on the cleaned layer: Annotorious for selection, drawing and
 * resize; plain OSD overlays (CSS dashed borders) for disputed outlines, because
 * the PIXI renderer cannot dash strokes.
 */

import { createOSDAnnotator, UserSelectAction, type ImageAnnotation, type OpenSeadragonAnnotator } from "@annotorious/openseadragon";
import "@annotorious/openseadragon/annotorious-openseadragon.css";
import OpenSeadragon from "openseadragon";
import { useEffect, useRef, useState } from "react";
import type { BBoxList, RegionOut } from "../../api/types";
import { KIND_COLOR, visibleRegions } from "../../lib/regions";
import { annotationBBox, regionStyle, regionToAnnotation } from "./annotations";
import s from "./studio.module.css";

export interface RegionLayerProps {
  viewer: OpenSeadragon.Viewer;
  regions: RegionOut[];
  selectedId: number | null;
  drawing: boolean;
  pageWidth: number | null;
  pageHeight: number | null;
  onSelect: (id: number | null) => void;
  onCreate: (bbox: BBoxList) => void;
  onMove: (id: number, bbox: BBoxList) => void;
}

/** Is the image-pixel box entirely inside the current viewport? */
function fullyVisible(viewer: OpenSeadragon.Viewer, [x0, y0, x1, y1]: BBoxList): boolean {
  if (viewer.world.getItemCount() === 0) return false;
  const box = viewer.viewport.imageToViewportRectangle(x0, y0, x1 - x0, y1 - y0);
  const view = viewer.viewport.getBounds(true);
  return box.x >= view.x && box.y >= view.y &&
    box.x + box.width <= view.x + view.width && box.y + box.height <= view.y + view.height;
}

function useLatest<T>(value: T) {
  const ref = useRef(value);
  ref.current = value;
  return ref;
}

export function RegionLayer(props: RegionLayerProps) {
  const { viewer, regions, selectedId, drawing } = props;
  const [anno, setAnno] = useState<OpenSeadragonAnnotator | null>(null);
  const latest = useLatest(props);
  const byId = useRef(new Map<string, RegionOut>());

  // one annotator per viewer
  useEffect(() => {
    const a = createOSDAnnotator(viewer, {
      drawingEnabled: false,
      drawingMode: "drag",
      userSelectAction: UserSelectAction.EDIT,
      style: (annotation, state) => regionStyle(byId.current.get(annotation.id), Boolean(state?.selected)),
    });
    a.setDrawingTool("rectangle");
    a.on("selectionChanged", (sel: ImageAnnotation[]) => {
      const first = sel[0];
      latest.current.onSelect(first && byId.current.has(first.id) ? Number(first.id) : null);
    });
    a.on("createAnnotation", (created: ImageAnnotation) => {
      const { pageWidth, pageHeight, onCreate } = latest.current;
      const bbox = annotationBBox(created, pageWidth, pageHeight);
      a.removeAnnotation(created.id); // the server's region replaces the sketch
      if (bbox) onCreate(bbox);
    });
    a.on("updateAnnotation", (updated: ImageAnnotation) => {
      const { pageWidth, pageHeight, onMove } = latest.current;
      const bbox = annotationBBox(updated, pageWidth, pageHeight);
      if (bbox && byId.current.has(updated.id)) onMove(Number(updated.id), bbox);
    });
    setAnno(a);
    return () => {
      setAnno(null);
      a.destroy();
    };
  }, [viewer, latest]);

  // regions -> annotations (rejected ones disappear)
  useEffect(() => {
    if (!anno) return;
    const shown = visibleRegions(regions);
    byId.current = new Map(shown.map((r) => [String(r.id), r]));
    anno.setAnnotations(shown.map(regionToAnnotation), true);
    anno.setStyle((annotation, state) => regionStyle(byId.current.get(annotation.id), Boolean(state?.selected)));
    if (selectedId !== null && byId.current.has(String(selectedId))) anno.setSelected(String(selectedId), true);
  }, [anno, regions, selectedId]);

  // external selection (list click / keyboard) -> annotator, and fit only when the
  // region is (partly) off screen: zooming in on a region the operator can already
  // see would throw away the page context they are judging it in
  useEffect(() => {
    if (!anno) return;
    if (selectedId === null) anno.cancelSelected();
    else {
      const region = byId.current.get(String(selectedId));
      if (!region) return;
      anno.setSelected(String(selectedId), true);
      if (!fullyVisible(viewer, region.bbox)) anno.fitBoundsWithConstraints(String(selectedId), { padding: 120 });
    }
  }, [anno, selectedId, viewer]);

  useEffect(() => {
    anno?.setDrawingEnabled(drawing);
  }, [anno, drawing]);

  // dashed outlines for disputed regions
  useEffect(() => {
    const elements: HTMLElement[] = [];
    const add = () => {
      const item = viewer.world.getItemAt(0);
      if (!item) return;
      for (const r of regions.filter((x) => x.status === "disputed")) {
        const [x0, y0, x1, y1] = r.bbox;
        const el = document.createElement("div");
        el.className = s.disputedOutline ?? "";
        el.style.setProperty("--kind", KIND_COLOR[r.kind]);
        el.title = r.reason ?? "disputed";
        viewer.addOverlay({ element: el, location: item.imageToViewportRectangle(x0, y0, x1 - x0, y1 - y0) });
        elements.push(el);
      }
    };
    const clear = () => {
      elements.forEach((el) => viewer.removeOverlay(el));
      elements.length = 0;
    };
    const refresh = () => {
      clear();
      add();
    };
    refresh();
    viewer.addHandler("open", refresh);
    return () => {
      viewer.removeHandler("open", refresh);
      clear();
    };
  }, [viewer, regions]);

  return null;
}
