/**
 * Wire mock mode into the three seams of the API layer: HTTP transport, media URLs
 * and the EventSource factory. Imported dynamically from main.tsx only when
 * VITE_MOCK=1, so none of this ships in a normal build.
 */

import { setTransport } from "../api/client";
import { setEventSourceFactory } from "../api/live";
import { httpMedia, setMediaResolver } from "../api/media";
import { mockPageImage } from "./images";
import { MockServer } from "./server";

export function installMock(): MockServer {
  const server = new MockServer();
  const image = (pageId: number, layer: Parameters<typeof mockPageImage>[2]) => {
    const page = server.db.pages.find((p) => p.id === pageId);
    if (!page) return "";
    return mockPageImage(page, server.db.regions.filter((r) => r.page_id === pageId), layer);
  };
  setTransport(server.transport);
  setEventSourceFactory(server.bus.open);
  setMediaResolver({
    ...httpMedia,
    pageImage: (pageId, layer) => image(pageId, layer),
    tileSource: (pageId, layer) => ({ type: "image", url: image(pageId, layer) }),
    bookFile: () => "data:text/plain;charset=utf-8,mock%20artifact",
    iaExport: () => "data:text/plain;charset=utf-8,mock%20export",
  });
  server.startTicker();
  return server;
}
