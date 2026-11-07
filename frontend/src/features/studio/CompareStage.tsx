/**
 * The viewer area: a single layer, two layers side by side, or a swipe between
 * them. Panes share one ViewportSync so pan/zoom stays aligned; the region layer
 * is attached to whichever pane shows the cleaned page (regions live in its pixels).
 */

import { useMemo, useState, type ReactNode } from "react";
import type OpenSeadragon from "openseadragon";
import { media } from "../../api/media";
import type { ImageLayer } from "../../api/types";
import { ViewportSync } from "./osd";
import s from "./studio.module.css";
import { Viewer } from "./Viewer";

export type CompareMode = "single" | "side" | "swipe";

export const LAYER_LABEL: Record<ImageLayer, string> = {
  original: "Original frame",
  spread: "Whole spread",
  clean: "Cleaned page",
  pdf: "PDF render",
};

interface Props {
  pageId: number;
  version: number;
  mode: CompareMode;
  layer: ImageLayer;
  compareLayer: ImageLayer;
  regions: (viewer: OpenSeadragon.Viewer) => ReactNode;
}

export function CompareStage({ pageId, version, mode, layer, compareLayer, regions }: Props) {
  const sync = useMemo(() => new ViewportSync(), []);
  const [split, setSplit] = useState(50);
  const pane = (l: ImageLayer, extra?: { style?: React.CSSProperties }) => (
    <Viewer key={`${mode}-${l}`} source={media.tileSource(pageId, l, version)} sync={sync} label={LAYER_LABEL[l]} style={extra?.style}>
      {l === "clean" ? regions : undefined}
    </Viewer>
  );

  if (mode === "single") return pane(layer);
  if (mode === "side")
    return (
      <>
        {pane(compareLayer)}
        {pane(layer)}
      </>
    );
  return (
    <div className={s.swipe}>
      {pane(compareLayer)}
      {pane(layer, { style: { clipPath: `inset(0 0 0 ${split}%)` } })}
      <div className={s.swipeHandle} style={{ left: `${split}%` }} />
      <input className={s.swipeRange} type="range" min={0} max={100} value={split} onChange={(e) => setSplit(Number(e.target.value))} aria-label="Swipe position" />
    </div>
  );
}
