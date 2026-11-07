/**
 * Screen 1 — Dashboard: per-collection status bars, the live activity feed, running
 * runs with stage bars, and import buttons.
 *
 * Running runs come from REST (books with status running, for the first paint) and
 * are overlaid with live `run.*` events, since there is no `GET /runs` listing.
 */

import { useMemo } from "react";
import { Link } from "react-router-dom";
import { describeEvent } from "../../api/events";
import { useBooks, useImport } from "../../api/hooks";
import { useLive, type LiveRun } from "../../api/live";
import type { BookOut } from "../../api/types";
import { Badge, Button, Empty, ErrorNote, PageHeader, Panel, ProgressBar, SegmentedBar } from "../../components/ui";
import { clock, percent } from "../../lib/format";
import { useSession } from "../../lib/session";
import s from "../screen.module.css";

function Kpis() {
  const { project } = useSession();
  const c = project?.counts ?? {};
  const total = Object.values(c).reduce((n, v) => n + (v ?? 0), 0);
  const items: [string, number, string?][] = [
    ["Books", total],
    ["In review", c.review ?? 0, "var(--st-review)"],
    ["Running", (c.running ?? 0) + (c.queued ?? 0), "var(--st-running)"],
    ["Published", c.published ?? 0, "var(--st-published)"],
    ["Errors", c.error ?? 0, "var(--st-error)"],
  ];
  return (
    <div className={s.kpis}>
      {items.map(([label, n, color]) => (
        <div key={label} className={s.kpi}>
          <b style={{ color }}>{n}</b>
          <span>{label}</span>
        </div>
      ))}
    </div>
  );
}

function Collections() {
  const { project } = useSession();
  const importer = useImport();
  if (!project) return null;
  return (
    <Panel
      title="Collections"
      bodyClass=""
      actions={
        <Button size="small" onClick={() => importer.mutate({ project: project.slug })} disabled={importer.isPending}>
          Import all
        </Button>
      }
    >
      <ErrorNote error={importer.error} />
      {Object.entries(project.collections).map(([key, name]) => {
        const counts = project.collection_counts?.[key];
        return (
          <div key={key} className={s.collection}>
            <div className={s.collectionHead}>
              <Link to={`/books?collection=${key}`}>
                <strong>{name}</strong>
              </Link>
              <code>{key}</code>
              <span className={s.spacer} />
              <Button size="small" variant="ghost" onClick={() => importer.mutate({ project: project.slug, collection: key })}>
                Import
              </Button>
            </div>
            {counts ? <SegmentedBar counts={counts} /> : <span className="faint">no per-collection counts</span>}
          </div>
        );
      })}
      <div className={s.collection}>
        <div className={s.collectionHead}>
          <strong>Whole project</strong>
        </div>
        <SegmentedBar counts={project.counts} />
      </div>
    </Panel>
  );
}

interface RunView {
  key: string;
  bookId: number | null;
  title: string;
  stage: string | null;
  done: number;
  total: number;
}

function mergeRuns(books: BookOut[], live: Record<number, LiveRun>): RunView[] {
  const views = new Map<string, RunView>();
  for (const b of books) {
    views.set(`b${b.id}`, { key: `b${b.id}`, bookId: b.id, title: b.title ?? b.slug, stage: b.step ?? null, done: Math.round((b.progress ?? 0) * 100), total: 100 });
  }
  for (const r of Object.values(live)) {
    if (r.status !== "running") {
      if (r.bookId !== null) views.delete(`b${r.bookId}`);
      continue;
    }
    const fromBook = r.bookId !== null ? books.find((b) => b.id === r.bookId) : undefined;
    if (r.bookId !== null) views.delete(`b${r.bookId}`);
    views.set(`r${r.runId}`, { key: `r${r.runId}`, bookId: r.bookId, title: fromBook?.title ?? `run #${r.runId}`, stage: r.stage, done: r.done, total: r.total });
  }
  return [...views.values()];
}

function RunningRuns() {
  const { project } = useSession();
  const { data } = useBooks(project ? { project: project.slug, status: "running", limit: 50 } : null);
  const live = useLive((st) => st.runs);
  const runs = useMemo(() => mergeRuns(data?.items ?? [], live), [data, live]);
  return (
    <Panel title="Running" bodyClass="" actions={<Badge color="var(--st-running)">{runs.length}</Badge>}>
      {runs.length === 0 && <Empty>No runs in progress.</Empty>}
      {runs.map((r) => (
        <div key={r.key} className={s.runRow}>
          <div>
            {r.bookId !== null ? <Link to={`/books/${r.bookId}`}>{r.title}</Link> : r.title}
            <small>{r.stage ?? "starting"}</small>
          </div>
          <ProgressBar done={r.done} total={r.total} />
          <span className={`${s.num} mono`}>{percent(r.done, r.total)}%</span>
        </div>
      ))}
    </Panel>
  );
}

function ActivityFeed() {
  const events = useLive((st) => st.events);
  const visible = useMemo(() => events.filter((e) => e.type !== "run.progress").slice(0, 80), [events]);
  return (
    <Panel title="Live activity" bodyClass="">
      {visible.length === 0 ? (
        <Empty>Events appear here as workers report them.</Empty>
      ) : (
        <ul className={s.feed}>
          {visible.map((e, i) => (
            <li key={`${e.id ?? i}-${e.ts}`}>
              <time>{clock(e.ts)}</time>
              <span className={s.feedType}>{e.type}</span>
              <span>
                {e.book_id !== null ? <Link to={`/books/${e.book_id}`}>{describeEvent(e)}</Link> : describeEvent(e)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

export function DashboardScreen() {
  const { project } = useSession();
  return (
    <div className={s.screen}>
      <PageHeader title="Dashboard" sub={project ? project.name : "Loading projects…"} />
      <Kpis />
      <div className={s.grid2}>
        <div className={s.stack}>
          <Collections />
          <RunningRuns />
        </div>
        <ActivityFeed />
      </div>
    </div>
  );
}
