/**
 * OpenSeadragon glue: viewer creation with studio defaults, tile swaps that keep
 * the operator's viewport, and a sync group so compare views pan/zoom together.
 */

import OpenSeadragon from "openseadragon";
import type { TileSourceSpec } from "../../api/media";

export function createViewer(element: HTMLElement): OpenSeadragon.Viewer {
  return new OpenSeadragon.Viewer({
    element,
    showNavigationControl: false,
    showNavigator: true,
    navigatorPosition: "BOTTOM_RIGHT",
    navigatorSizeRatio: 0.12,
    gestureSettingsMouse: { clickToZoom: false, dblClickToZoom: true },
    animationTime: 0.35,
    maxZoomPixelRatio: 3,
    visibilityRatio: 0.6,
    crossOriginPolicy: false,
    preserveImageSizeOnResize: true,
    timeout: 60_000,
  });
}

/** Open a new source; if one was open, keep the viewport (re-run tile swap). */
export function openKeepingViewport(viewer: OpenSeadragon.Viewer, source: TileSourceSpec): void {
  const hadImage = viewer.world.getItemCount() > 0;
  const center = viewer.viewport.getCenter();
  const zoom = viewer.viewport.getZoom();
  if (hadImage) {
    viewer.addOnceHandler("open", () => {
      viewer.viewport.zoomTo(zoom, undefined, true);
      viewer.viewport.panTo(center, true);
    });
  }
  viewer.open({ tileSource: source });
}

/**
 * Keeps several viewers on the same viewport (in OSD viewport coordinates, i.e.
 * normalised to image width, so layers of different pixel size still line up).
 */
export class ViewportSync {
  private readonly viewers = new Set<OpenSeadragon.Viewer>();
  private leader: OpenSeadragon.Viewer | null = null;

  add(viewer: OpenSeadragon.Viewer): () => void {
    const handler = () => this.follow(viewer);
    this.viewers.add(viewer);
    viewer.addHandler("viewport-change", handler);
    return () => {
      viewer.removeHandler("viewport-change", handler);
      this.viewers.delete(viewer);
    };
  }

  private follow(source: OpenSeadragon.Viewer): void {
    if (this.leader && this.leader !== source) return;
    this.leader = source;
    const zoom = source.viewport.getZoom();
    const center = source.viewport.getCenter();
    for (const other of this.viewers) {
      if (other === source || other.world.getItemCount() === 0) continue;
      const oc = other.viewport.getCenter();
      if (Math.abs(other.viewport.getZoom() - zoom) < 1e-6 && Math.abs(oc.x - center.x) < 1e-6 && Math.abs(oc.y - center.y) < 1e-6) continue;
      other.viewport.zoomTo(zoom, undefined, true);
      other.viewport.panTo(center, true);
    }
    queueMicrotask(() => {
      this.leader = null;
    });
  }
}
