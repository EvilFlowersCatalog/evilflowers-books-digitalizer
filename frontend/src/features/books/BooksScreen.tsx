/**
 * Screen 2 — Books table: filters (status, flagged, collection, search) kept in the
 * URL so a filtered view can be shared, sort by quality score, bulk actions.
 */

import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useBooks, useBulkReview, useBulkRun } from "../../api/hooks";
import { BOOK_STATUSES, type BookQuery, type BookSort, type BookStatus } from "../../api/types";
import { Badge, Button, Empty, ErrorNote, PageHeader, Panel, Spinner, StatusBadge, Toast } from "../../components/ui";
import { confirmApproval } from "../../lib/approval";
import { relativeTime, score } from "../../lib/format";
import { useSession } from "../../lib/session";
import s from "../screen.module.css";

const PAGE_SIZE = 50;

function useFilters(project: string | undefined): [BookQuery | null, (patch: Record<string, string | null>) => void] {
  const [params, setParams] = useSearchParams();
  const query = useMemo<BookQuery | null>(() => {
    if (!project) return null;
    const q: BookQuery = {
      project,
      limit: PAGE_SIZE,
      offset: Number(params.get("offset") ?? 0),
      sort: (params.get("sort") as BookSort | null) ?? "-updated_at",
    };
    const collection = params.get("collection");
    const status = params.get("status");
    const text = params.get("q");
    if (collection) q.collection = collection;
    if (status) q.status = status as BookStatus;
    if (text) q.q = text;
    if (params.get("flagged") === "1") q.flagged = true;
    return q;
  }, [params, project]);
  const update = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (v) next.set(k, v);
      else next.delete(k);
    }
    if (!("offset" in patch)) next.delete("offset");
    setParams(next, { replace: true });
  };
  return [query, update];
}

function SortHeader({ label, field, sort, onSort }: { label: string; field: string; sort: string; onSort: (s: string) => void }) {
  const active = sort.replace(/^-/, "") === field;
  const desc = sort.startsWith("-");
  return (
    <th aria-sort={active ? (desc ? "descending" : "ascending") : "none"}>
      <button type="button" style={{ all: "unset", cursor: "pointer" }} onClick={() => onSort(active && !desc ? `-${field}` : field)}>
        {label} {active ? (desc ? "↓" : "↑") : ""}
      </button>
    </th>
  );
}

export function BooksScreen() {
  const { project } = useSession();
  const [query, update] = useFilters(project?.slug);
  const { data, isLoading, error, isFetching } = useBooks(query);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [search, setSearch] = useState(query?.q ?? "");
  const bulkRun = useBulkRun();
  const bulkReview = useBulkReview();
  const [toast, setToast] = useState("");

  useEffect(() => setSelected(new Set()), [query]);
  useEffect(() => {
    const t = setTimeout(() => {
      if ((query?.q ?? "") !== search) update({ q: search || null });
    }, 300);
    return () => clearTimeout(t);
  }, [search]); // debounce only on typing; `query`/`update` change with every URL write
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(""), 3500);
    return () => clearTimeout(t);
  }, [toast]);

  const items = data?.items ?? [];
  const total = data?.total ?? 0;
  const offset = query?.offset ?? 0;
  const sort = query?.sort ?? "-updated_at";
  const allChecked = items.length > 0 && items.every((b) => selected.has(b.id));
  const toggle = (id: number) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSelected(next);
  };
  const report = (label: string) => (results: PromiseSettledResult<unknown>[]) => {
    const failed = results.filter((r) => r.status === "rejected").length;
    setToast(`${label}: ${results.length - failed} ok${failed ? `, ${failed} refused (state)` : ""}`);
    setSelected(new Set());
  };

  return (
    <div className={s.screen}>
      <PageHeader title="Books" sub={`${total} ${total === 1 ? "book" : "books"}${isFetching ? " · refreshing…" : ""}`} />
      <Panel bodyClass="">
        <div className={s.toolbar}>
          <input
            className="input"
            style={{ width: 260 }}
            placeholder="Search title, slug, identifier…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            aria-label="Search"
          />
          <select className="select" value={query?.status ?? ""} onChange={(e) => update({ status: e.target.value || null })} aria-label="Status">
            <option value="">Any status</option>
            {BOOK_STATUSES.map((st) => (
              <option key={st} value={st}>
                {st}
              </option>
            ))}
          </select>
          <select className="select" value={query?.collection ?? ""} onChange={(e) => update({ collection: e.target.value || null })} aria-label="Collection">
            <option value="">All collections</option>
            {Object.entries(project?.collections ?? {}).map(([k, name]) => (
              <option key={k} value={k}>
                {name}
              </option>
            ))}
          </select>
          <label className="row" style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
            <input type="checkbox" checked={query?.flagged ?? false} onChange={(e) => update({ flagged: e.target.checked ? "1" : null })} />
            Flagged only
          </label>
          <span className={s.spacer} />
          <Button size="small" variant="ghost" onClick={() => update({ sort: sort === "quality_score" ? "-quality_score" : "quality_score" })}>
            Sort by score {sort === "quality_score" ? "↑" : sort === "-quality_score" ? "↓" : ""}
          </Button>
        </div>
        {selected.size > 0 && (
          <div className={s.bulk}>
            <b>{selected.size} selected</b>
            <Button size="small" disabled={bulkRun.isPending} onClick={() => bulkRun.mutate({ ids: [...selected], body: { kind: "full" } }, { onSuccess: report("Queued") })}>
              Queue full run
            </Button>
            <Button size="small" disabled={bulkRun.isPending} onClick={() => bulkRun.mutate({ ids: [...selected], body: { kind: "render" } }, { onSuccess: report("Render queued") })}>
              Re-render
            </Button>
            <Button
              size="small"
              variant="primary"
              disabled={bulkReview.isPending}
              onClick={() =>
                confirmApproval(items.filter((b) => selected.has(b.id))) &&
                bulkReview.mutate({ ids: [...selected], body: { decision: "approved" } }, { onSuccess: report("Approved") })
              }
            >
              Approve
            </Button>
            <Button size="small" variant="ghost" onClick={() => setSelected(new Set())}>
              Clear
            </Button>
          </div>
        )}
        <ErrorNote error={error} />
        {isLoading ? (
          <Empty>
            <Spinner />
          </Empty>
        ) : items.length === 0 ? (
          <Empty>No books match these filters.</Empty>
        ) : (
          <table className={s.table}>
            <thead>
              <tr>
                <th style={{ width: 28 }}>
                  <input type="checkbox" aria-label="Select all" checked={allChecked} onChange={() => setSelected(allChecked ? new Set() : new Set(items.map((b) => b.id)))} />
                </th>
                <SortHeader label="Title" field="title" sort={sort} onSort={(v) => update({ sort: v })} />
                <th>Collection</th>
                <th>Status</th>
                <SortHeader label="Score" field="quality_score" sort={sort} onSort={(v) => update({ sort: v })} />
                <th className={s.num}>Leaves</th>
                <th className={s.num}>Disputed</th>
                <th>Flags</th>
                <SortHeader label="Updated" field="updated_at" sort={sort} onSort={(v) => update({ sort: v })} />
              </tr>
            </thead>
            <tbody>
              {items.map((b) => (
                <tr key={b.id} aria-selected={selected.has(b.id)}>
                  <td>
                    <input type="checkbox" aria-label={`Select ${b.title ?? b.slug}`} checked={selected.has(b.id)} onChange={() => toggle(b.id)} />
                  </td>
                  <td className={s.titleCell}>
                    <Link to={`/books/${b.id}`}>{b.title || b.slug}</Link>
                    <small>{b.item_id ?? b.slug}</small>
                  </td>
                  <td>{b.collection ?? "—"}</td>
                  <td>
                    <StatusBadge status={b.status} />
                  </td>
                  <td className={`${s.num} mono`}>{score(b.quality_score)}</td>
                  <td className={s.num}>{b.n_pages ?? "—"}</td>
                  <td className={s.num}>
                    {b.disputed ? <Badge color="var(--st-review)" title="open disputed regions now">{b.disputed}</Badge> : <span className="faint">0</span>}
                  </td>
                  <td className={s.flags}>{(b.flags ?? []).join(" · ")}</td>
                  <td className="muted">{relativeTime(b.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <div className={s.pager}>
          {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}
          <Button size="small" disabled={offset === 0} onClick={() => update({ offset: String(Math.max(0, offset - PAGE_SIZE)) })}>
            Previous
          </Button>
          <Button size="small" disabled={offset + PAGE_SIZE >= total} onClick={() => update({ offset: String(offset + PAGE_SIZE) })}>
            Next
          </Button>
        </div>
      </Panel>
      {toast && <Toast>{toast}</Toast>}
    </div>
  );
}
