/**
 * Screen 4 — Page studio (the core). Viewer with layers and compare modes, regions
 * (draw / resize / relabel, disputes), page properties with OpenCV preview, text &
 * math, and an interactive re-run whose result swaps in when `page.updated` arrives.
 */

import { useCallback, useEffect, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { useBook, useCreateRun, usePage, usePages, usePatchPage, useRegionMutations } from "../../api/hooks";
import { useLive } from "../../api/live";
import type { ImageLayer, PageDetail, PagePatch, RegionKind, RegionPatch, RunStatus } from "../../api/types";
import { REGION_KINDS } from "../../api/types";
import { Badge, Button, Empty, ErrorNote, Segmented, Spinner } from "../../components/ui";
import { KIND_LABEL, defaultAction, nextDispute, relabel, stepRegion } from "../../lib/regions";
import { keyLike, studioCommand, type StudioCommand } from "../../lib/shortcuts";
import { CompareStage, LAYER_LABEL, type CompareMode } from "./CompareStage";
import { PropertiesPanel } from "./PropertiesPanel";
import { RegionLayer } from "./RegionLayer";
import { RegionsPanel } from "./RegionsPanel";
import s from "./studio.module.css";
import { TextPanel } from "./TextPanel";

type Tab = "regions" | "properties" | "text";
const LAYERS: ImageLayer[] = ["original", "spread", "clean", "pdf"];

function useNeighbours(detail: PageDetail | undefined) {
  const { data: pages = [] } = usePages(detail?.page.book_id);
  if (!detail) return { prev: null, next: null, count: 0, pages };
  const i = pages.findIndex((p) => p.id === detail.page.id);
  return {
    prev: detail.prev_id ?? pages[i - 1]?.id ?? null,
    next: detail.next_id ?? pages[i + 1]?.id ?? null,
    count: pages.length,
    pages,
  };
}

/** Router state of a `d` jump to another page: select its first dispute on arrival. */
interface StudioLocationState {
  selectDisputed?: boolean;
}

/** A re-run that never reports back (worker gone, events missed) stops blocking the button. */
const RERUN_GIVE_UP_MS = 10 * 60_000;
const TERMINAL: ReadonlySet<RunStatus> = new Set(["succeeded", "failed", "cancelled"]);

/**
 * Keeps "re-running…" visible until the page version moves past the one we re-ran,
 * or until the run itself ends: a failed or cancelled run sends no `page.updated`,
 * so its `run.finished` (live store) is what releases the button and shows why.
 */
function useRerun(detail: PageDetail | undefined) {
  const createRun = useCreateRun();
  const [waitingFor, setWaitingFor] = useState<number | null>(null);
  const [runId, setRunId] = useState<number | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const live = useLive((st) => (runId !== null ? st.runs[runId] : undefined));
  const version = detail?.page.version ?? 0;
  useEffect(() => {
    if (waitingFor !== null && version > waitingFor) setWaitingFor(null);
  }, [version, waitingFor]);
  useEffect(() => {
    if (waitingFor === null || !live || !TERMINAL.has(live.status)) return;
    setWaitingFor(null);
    if (live.status !== "succeeded") setFailure(`Re-run ${live.status}${live.error ? `: ${live.error}` : ""}`);
  }, [live, waitingFor]);
  useEffect(() => {
    if (waitingFor === null) return;
    const timer = setTimeout(() => setWaitingFor(null), RERUN_GIVE_UP_MS);
    return () => clearTimeout(timer);
  }, [waitingFor]);
  const start = () => {
    if (!detail) return;
    setFailure(null);
    setRunId(null);          // the previous run's terminal status must not end this wait
    setWaitingFor(detail.page.version);
    createRun.mutate(
      { bookId: detail.page.book_id, body: { kind: "rerun", pages: [detail.page.index], interactive: true } },
      { onSuccess: (run) => setRunId(run.id), onError: () => setWaitingFor(null) },
    );
  };
  return { start, running: waitingFor !== null, error: createRun.error ?? failure };
}

export default function PageStudio() {
  const pageId = Number(useParams().pageId);
  const navigate = useNavigate();
  const { data: detail, isLoading, error } = usePage(pageId);
  const { data: book } = useBook(detail?.page.book_id ?? 0);
  const nav = useNeighbours(detail);
  const regionsApi = useRegionMutations(pageId);
  const patchPage = usePatchPage(pageId);
  const rerun = useRerun(detail);

  const [tab, setTab] = useState<Tab>("regions");
  const [layer, setLayer] = useState<ImageLayer>("clean");
  const [compareLayer, setCompareLayer] = useState<ImageLayer>("original");
  const [mode, setMode] = useState<CompareMode>("single");
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [drawing, setDrawing] = useState(false);
  const [drawKind, setDrawKind] = useState<RegionKind>("text");

  const location = useLocation();
  const arriveOnDispute = Boolean((location.state as StudioLocationState | null)?.selectDisputed);
  useEffect(() => {
    setSelectedId(null);
    setDrawing(false);
  }, [pageId]);
  useEffect(() => {
    if (!arriveOnDispute || detail?.page.id !== pageId) return;
    const first = detail.regions.find((r) => r.status === "disputed");
    if (first) setSelectedId(first.id);
    navigate(".", { replace: true, state: null });     // once per jump
  }, [arriveOnDispute, detail, pageId, navigate]);

  const patchRegion = useCallback((id: number, patch: RegionPatch) => regionsApi.patch.mutate({ id, patch }), [regionsApi.patch]);
  const rejectRegion = useCallback(
    (id: number) => {
      regionsApi.reject.mutate(id);
      setSelectedId(null);
    },
    [regionsApi.reject],
  );

  const run = useCallback(
    (cmd: StudioCommand) => {
      switch (cmd.type) {
        case "page": {
          const target = cmd.delta < 0 ? nav.prev : nav.next;
          if (target) navigate(`/pages/${target}`);
          return;
        }
        case "deselect":
          setSelectedId(null);
          return;
        case "cancelDraw":
          setDrawing(false);
          return;
        case "region":
          setSelectedId(stepRegion(detail?.regions ?? [], selectedId, cmd.delta));
          return;
        case "nextDisputed": {
          const target = nextDispute(detail?.regions ?? [], selectedId, nav.pages, pageId);
          if (target && "region" in target) setSelectedId(target.region);
          if (target && "page" in target) navigate(`/pages/${target.page}`, { state: { selectDisputed: true } });
          return;
        }
      }
      if (selectedId === null) return;
      if (cmd.type === "relabel") patchRegion(selectedId, relabel(cmd.kind));
      if (cmd.type === "confirm") patchRegion(selectedId, { status: "confirmed" });
      if (cmd.type === "reject") rejectRegion(selectedId);
    },
    [nav.prev, nav.next, nav.pages, navigate, selectedId, patchRegion, rejectRegion, detail?.regions, pageId],
  );

  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      const cmd = studioCommand(keyLike(ev), { hasSelection: selectedId !== null, drawing });
      if (!cmd) return;
      ev.preventDefault();
      run(cmd);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [run, selectedId, drawing]);

  if (isLoading) return <Empty><Spinner /> Loading leaf…</Empty>;
  if (!detail) return <Empty><ErrorNote error={error ?? "Page not found"} /></Empty>;

  const { page } = detail;
  const showsClean = layer === "clean" || (mode !== "single" && compareLayer === "clean");
  const savePage = (patch: PagePatch, andRerun: boolean) =>
    patchPage.mutate(patch, { onSuccess: () => andRerun && rerun.start() });

  return (
    <div className={s.studio}>
      <div className={s.toolbar}>
        <div className={s.crumb}>
          <Link to={`/books/${page.book_id}`} title={book?.book.item_id ?? undefined}>
            {book?.book.title || book?.book.slug || "Book"}
          </Link>
        </div>
        <span className={s.leafNav}>
          <Button size="small" disabled={!nav.prev} onClick={() => run({ type: "page", delta: -1 })} title="Previous leaf (←)">←</Button>
          <b>leaf {page.index}</b>
          <span className="faint">/ {nav.count || "?"}</span>
          <Button size="small" disabled={!nav.next} onClick={() => run({ type: "page", delta: 1 })} title="Next leaf (→)">→</Button>
        </span>
        <Badge>{page.page_type}</Badge>
        {page.page_number && <span className="muted">p. {page.page_number}</span>}
        {!page.add_to_access && <Badge color="var(--text-faint)">not in access</Badge>}
        <span className="faint mono">v{page.version}</span>
        <span className={s.sep} />
        <Segmented label="Layer" value={layer} onChange={setLayer} options={LAYERS.map((l) => ({ value: l, label: LAYER_LABEL[l].split(" ")[0]!, title: LAYER_LABEL[l] }))} />
        <Segmented
          label="Compare"
          value={mode}
          onChange={setMode}
          options={[
            { value: "single", label: "Single" },
            { value: "side", label: "Side by side" },
            { value: "swipe", label: "Swipe" },
          ]}
        />
        {mode !== "single" && (
          <select className="select" style={{ width: "auto" }} value={compareLayer} onChange={(e) => setCompareLayer(e.target.value as ImageLayer)} aria-label="Compare with">
            {LAYERS.map((l) => <option key={l} value={l}>vs {LAYER_LABEL[l]}</option>)}
          </select>
        )}
        <span className={s.sep} />
        <Button size="small" variant={drawing ? "primary" : "default"} disabled={!showsClean} onClick={() => setDrawing(!drawing)} title="Drag on the cleaned page to draw a region">
          {drawing ? "Drawing…" : "Draw region"}
        </Button>
        <select className="select" style={{ width: "auto" }} value={drawKind} onChange={(e) => setDrawKind(e.target.value as RegionKind)} aria-label="Kind of new regions">
          {REGION_KINDS.map((k) => <option key={k} value={k}>{KIND_LABEL[k]}</option>)}
        </select>
        <span style={{ flex: 1 }} />
        <Button variant="primary" size="small" disabled={rerun.running} onClick={rerun.start} title="Re-run this leaf on the interactive queue">
          {rerun.running ? "Re-running…" : "Re-run page"}
        </Button>
      </div>

      <div className={s.stage}>
        <CompareStage
          pageId={page.id}
          version={page.version}
          mode={mode}
          layer={layer}
          compareLayer={compareLayer}
          regions={(viewer) => (
            <RegionLayer
              viewer={viewer}
              regions={detail.regions}
              selectedId={selectedId}
              drawing={drawing}
              pageWidth={page.width}
              pageHeight={page.height}
              onSelect={setSelectedId}
              onCreate={(bbox) => {
                regionsApi.create.mutate({ kind: drawKind, bbox, action: defaultAction(drawKind) });
                setDrawing(false);
              }}
              onMove={(id, bbox) => patchRegion(id, { bbox })}
            />
          )}
        />
        {!showsClean && <div className={s.overlayHint}>Regions are shown on the cleaned layer.</div>}
        {rerun.running && (
          <div className={s.rerunning}>
            <Spinner /> Re-running leaf {page.index}; the tile swaps when it is done
          </div>
        )}
      </div>

      <aside className={s.side}>
        <div className={s.tabs} role="tablist">
          {(["regions", "properties", "text"] as Tab[]).map((t) => (
            <button key={t} type="button" role="tab" aria-selected={tab === t} onClick={() => setTab(t)}>
              {t === "regions" ? `Regions${page.disputed ? ` · ${page.disputed} open` : ""}` : t === "properties" ? "Page" : "Text & math"}
            </button>
          ))}
        </div>
        <div className={s.tabBody}>
          <ErrorNote error={regionsApi.patch.error ?? regionsApi.create.error ?? regionsApi.reject.error ?? rerun.error} />
          {tab === "regions" && (
            <RegionsPanel
              bookId={page.book_id}
              pageId={page.id}
              regions={detail.regions}
              selectedId={selectedId}
              onSelect={setSelectedId}
              onPatch={patchRegion}
              onReject={rejectRegion}
            />
          )}
          {tab === "properties" && <PropertiesPanel detail={detail} saving={patchPage.isPending} error={patchPage.error} onSave={savePage} />}
          {tab === "text" && (
            <TextPanel detail={detail} selected={detail.regions.find((r) => r.id === selectedId)} onSave={(id, text) => patchRegion(id, { text })} />
          )}
        </div>
      </aside>
    </div>
  );
}
