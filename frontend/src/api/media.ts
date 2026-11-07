/**
 * Every URL the browser loads directly (images, Deep Zoom, downloads) goes through
 * here, never through `fetch`. One place lets mock mode swap them for generated
 * images, and puts `?v={page.version}` on page media so a `page.updated` event
 * really swaps the tile instead of hitting the browser cache.
 */

import { API_BASE } from "./client";
import type { ImageLayer } from "./types";

/** What OpenSeadragon accepts: a DZI URL, or a single image. */
export type TileSourceSpec = string | { type: "image"; url: string; buildPyramid?: boolean };

export interface MediaResolver {
  pageImage(pageId: number, layer: ImageLayer, width: number, version: number): string;
  tileSource(pageId: number, layer: ImageLayer, version: number): TileSourceSpec;
  bookFile(bookId: number, kind: string): string;
  iaExport(bookId: number): string;
  eventsUrl(filter: { project?: string | undefined; book?: number | undefined }): string;
}

export const httpMedia: MediaResolver = {
  pageImage: (pageId, layer, width, version) => `${API_BASE}/pages/${pageId}/image/${layer}?w=${width}&v=${version}`,
  // Only the cleaned page has a pyramid; the other layers are single renditions.
  // The DZI is opened by URL so OSD resolves `dzi_files/…` next to it. The `.xml`
  // extension matters: only then does OSD carry `?v=` over to every tile URL
  // (without it the tiles resolve to `…/dzi?v=1_files/…` and 404).
  tileSource: (pageId, layer, version) =>
    layer === "clean"
      ? `${API_BASE}/pages/${pageId}/dzi.xml?v=${version}`
      : { type: "image", url: `${API_BASE}/pages/${pageId}/image/${layer}?w=3000&v=${version}`, buildPyramid: false },
  bookFile: (bookId, kind) => `${API_BASE}/books/${bookId}/files/${encodeURIComponent(kind)}`,
  iaExport: (bookId) => `${API_BASE}/books/${bookId}/export/ia`,
  eventsUrl: ({ project, book }) => {
    const params = new URLSearchParams();
    if (project) params.set("project", project);
    if (book !== undefined) params.set("book", String(book));
    const qs = params.toString();
    return `${API_BASE}/events${qs ? `?${qs}` : ""}`;
  },
};

let resolver: MediaResolver = httpMedia;

export function setMediaResolver(next: MediaResolver): void {
  resolver = next;
}

export const media: MediaResolver = {
  pageImage: (...args) => resolver.pageImage(...args),
  tileSource: (...args) => resolver.tileSource(...args),
  bookFile: (...args) => resolver.bookFile(...args),
  iaExport: (...args) => resolver.iaExport(...args),
  eventsUrl: (...args) => resolver.eventsUrl(...args),
};
