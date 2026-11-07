/**
 * SSE events -> TanStack Query cache effects.
 *
 * Events are notifications, not data (studio/events.py): most just say "re-read X".
 * The two high-rate ones (`run.progress`, `run.log`) are patched straight into the
 * cached run instead, so a busy book does not refetch on every progress tick.
 *
 * `planEvent` is pure (unit-tested); `applyPlan` performs it on a QueryClient.
 */

import type { QueryClient } from "@tanstack/react-query";
import { qk, type QueryKey } from "./queryKeys";
import { EVENT_TYPES, type BookDetail, type EventType, type LogLine, type RunOut, type StudioEvent } from "./types";

const RUN_LOG_LIMIT = 200;

export interface RunPatch {
  runId: number;
  bookId: number | null;
  fields?: Partial<Pick<RunOut, "stage" | "done" | "total" | "status" | "error">>;
  log?: LogLine;
}

export interface EventPlan {
  invalidate: QueryKey[];
  runPatch?: RunPatch;
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function str(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

/** Keys that describe a book everywhere it is listed. */
function bookFamily(bookId: number | null): QueryKey[] {
  const keys: QueryKey[] = [qk.projects(), qk.booksAll()];
  if (bookId !== null) keys.push(qk.book(bookId));
  return keys;
}

function pageFamily(event: StudioEvent): QueryKey[] {
  const keys: QueryKey[] = [];
  const pageId = num(event.data.page_id);
  if (pageId !== null) keys.push(qk.page(pageId));
  if (event.book_id !== null) keys.push(qk.pages(event.book_id));
  return keys;
}

function runPatch(event: StudioEvent): RunPatch | undefined {
  const runId = num(event.data.run_id);
  if (runId === null) return undefined;
  const base = { runId, bookId: event.book_id };
  if (event.type === "run.log") {
    const log: LogLine = {
      ts: event.ts,
      level: str(event.data.level) ?? "info",
      message: str(event.data.message) ?? "",
    };
    return { ...base, log };
  }
  const fields: RunPatch["fields"] = { status: "running" };
  const stage = str(event.data.stage);
  const done = num(event.data.done);
  const total = num(event.data.total);
  if (stage !== null) fields.stage = stage;
  if (done !== null) fields.done = done;
  if (total !== null) fields.total = total;
  return { ...base, fields };
}

export function planEvent(event: StudioEvent): EventPlan {
  const runId = num(event.data.run_id);
  const runKey: QueryKey[] = runId !== null ? [qk.run(runId)] : [];
  switch (event.type) {
    case "import.started":
      return { invalidate: [] };
    case "import.finished":
    case "book.discovered":
      return { invalidate: [qk.projects(), qk.booksAll()] };
    case "run.progress":
    case "run.log": {
      const patch = runPatch(event);
      return patch ? { invalidate: [], runPatch: patch } : { invalidate: runKey };
    }
    case "run.started":
    case "book.queued":
      return { invalidate: [...runKey, ...bookFamily(event.book_id)] };
    case "run.finished": {
      const pages = event.book_id !== null ? [qk.pages(event.book_id)] : [];
      return { invalidate: [...runKey, ...bookFamily(event.book_id), ...pages] };
    }
    case "page.updated":
    case "regions.updated":
      return { invalidate: pageFamily(event) };
    case "book.status":
    case "book.review_needed":
    case "book.ready":
    case "book.published":
      return { invalidate: bookFamily(event.book_id) };
  }
}

function patchRun(run: RunOut, patch: RunPatch): RunOut {
  const next: RunOut = { ...run, ...patch.fields };
  if (patch.log) next.log = [...(run.log ?? []), patch.log].slice(-RUN_LOG_LIMIT);
  return next;
}

export function applyPlan(client: QueryClient, plan: EventPlan): void {
  const { runPatch: patch } = plan;
  if (patch) {
    client.setQueryData<RunOut>(qk.run(patch.runId), (run) => (run ? patchRun(run, patch) : run));
    if (patch.bookId !== null) {
      client.setQueryData<BookDetail>(qk.book(patch.bookId), (detail) =>
        detail
          ? { ...detail, runs: detail.runs.map((r) => (r.id === patch.runId ? patchRun(r, patch) : r)) }
          : detail,
      );
    }
  }
  for (const queryKey of plan.invalidate) void client.invalidateQueries({ queryKey });
}

/** Parse one SSE `data:` payload; returns null for anything malformed. */
export function parseEvent(raw: string, id?: string): StudioEvent | null {
  try {
    const d: unknown = JSON.parse(raw);
    if (!d || typeof d !== "object") return null;
    const o = d as Record<string, unknown>;
    if (!isEventType(o.type) || typeof o.project !== "string") return null;
    const event: StudioEvent = {
      type: o.type,
      project: o.project,
      book_id: typeof o.book_id === "number" ? o.book_id : null,
      data: o.data && typeof o.data === "object" ? (o.data as Record<string, unknown>) : {},
      ts: typeof o.ts === "string" ? o.ts : new Date().toISOString(),
    };
    if (id) event.id = id;
    return event;
  } catch {
    return null;
  }
}

export function isEventType(value: unknown): value is EventType {
  return typeof value === "string" && (EVENT_TYPES as readonly string[]).includes(value);
}

/** One-line operator-readable summary for the activity feed. */
export function describeEvent(event: StudioEvent): string {
  const d = event.data;
  switch (event.type) {
    case "import.started":
      return `Import started${d.collection ? ` · ${String(d.collection)}` : ""}`;
    case "import.finished":
      return `Import finished · ${String(d.books ?? 0)} books, ${String(d.new ?? 0)} new`;
    case "book.discovered":
      return `Discovered ${String(d.slug ?? "")}`;
    case "book.queued":
      return `Queued ${String(d.kind ?? "")} run #${String(d.run_id ?? "")}`;
    case "run.started":
      return `Run #${String(d.run_id ?? "")} started (${String(d.kind ?? "")})`;
    case "run.progress":
      return `Run #${String(d.run_id ?? "")} · ${String(d.stage ?? "")} ${String(d.done ?? 0)}/${String(d.total ?? 0)}`;
    case "run.log":
      return String(d.message ?? "");
    case "run.finished":
      return `Run #${String(d.run_id ?? "")} ${String(d.status ?? "finished")}${d.error ? `: ${String(d.error)}` : ""}`;
    case "page.updated":
      return `Leaf ${String(d.index ?? "")} updated (v${String(d.version ?? "")})`;
    case "regions.updated":
      return `Leaf ${String(d.index ?? "")} regions · ${String(d.disputed ?? 0)} disputed`;
    case "book.status":
      return `Status ${String(d.previous ?? "")} → ${String(d.status ?? "")}`;
    case "book.review_needed":
      return `Needs review · ${Array.isArray(d.flags) ? d.flags.join("; ") : ""}`;
    case "book.ready":
      return `Passed the quality gate (score ${String(d.score ?? "")})`;
    case "book.published":
      return `Published v${String(d.version ?? "")} · entry ${String(d.entry_id ?? "")}`;
  }
}
