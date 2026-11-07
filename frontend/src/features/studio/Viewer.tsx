/**
 * One deep-zoom pane. Owns an OpenSeadragon viewer for its lifetime; a new tile
 * source (layer switch, or a re-run bumping the page version) swaps the image in
 * place without resetting the operator's zoom.
 */

import type OpenSeadragon from "openseadragon";
import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import type { TileSourceSpec } from "../../api/media";
import { createViewer, openKeepingViewport, type ViewportSync } from "./osd";
import s from "./studio.module.css";

export interface ViewerProps {
  source: TileSourceSpec;
  sync?: ViewportSync;
  label?: string;
  style?: CSSProperties;
  className?: string;
  /** render-prop for overlays (regions) that need the viewer instance */
  children?: (viewer: OpenSeadragon.Viewer) => ReactNode;
  onViewer?: (viewer: OpenSeadragon.Viewer | null) => void;
}

const sourceKey = (src: TileSourceSpec) => (typeof src === "string" ? src : src.url);

export function Viewer({ source, sync, label, style, className, children, onViewer }: ViewerProps) {
  const host = useRef<HTMLDivElement>(null);
  const [viewer, setViewer] = useState<OpenSeadragon.Viewer | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!host.current) return;
    const v = createViewer(host.current);
    v.addHandler("open-failed", () => setFailed(true));
    v.addHandler("open", () => setFailed(false));
    setViewer(v);
    onViewer?.(v);
    return () => {
      onViewer?.(null);
      setViewer(null);
      v.destroy();
    };
  }, []); // the viewer lives as long as the pane; callbacks are read once

  useEffect(() => (viewer && sync ? sync.add(viewer) : undefined), [viewer, sync]);

  const key = sourceKey(source);
  useEffect(() => {
    if (viewer) openKeepingViewport(viewer, source);
  }, [viewer, key]); // keyed by URL: a fresh object literal with the same URL must not reload

  return (
    <div className={`${s.pane} ${className ?? ""}`} style={style}>
      <div ref={host} className={s.osd} />
      {label && <span className={s.paneLabel}>{label}</span>}
      {failed && <div className={s.paneError}>This layer is not available yet.</div>}
      {viewer && children?.(viewer)}
    </div>
  );
}
