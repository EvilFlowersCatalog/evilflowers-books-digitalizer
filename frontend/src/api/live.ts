/**
 * The one SSE connection of the app and the small live store fed by it.
 *
 * - Every event goes through `planEvent`/`applyPlan` (cache invalidation).
 * - The store keeps the activity feed and per-run progress for the status bar and
 *   the dashboard; it is not a cache of server state, just "what happened lately".
 * - When SSE is down, queries fall back to polling; when it comes back, everything
 *   is re-read once because events may have been missed (events.py contract).
 */

import type { QueryClient } from "@tanstack/react-query";
import { useSyncExternalStore } from "react";
import { applyPlan, parseEvent, planEvent } from "./events";
import { media } from "./media";
import { EVENT_TYPES, type RunKind, type RunStatus, type StudioEvent } from "./types";

export type SseStatus = "connecting" | "open" | "down";

export interface LiveRun {
  runId: number;
  bookId: number | null;
  project: string;
  kind?: RunKind;
  status: RunStatus;
  stage: string | null;
  done: number;
  total: number;
  updated: string;
  /** the failure a `run.finished` event carried */
  error?: string;
}

export interface LiveState {
  status: SseStatus;
  events: StudioEvent[];
  runs: Record<number, LiveRun>;
}

/** Subset of EventSource the app uses (mock mode supplies its own). */
export interface EventSourceLike {
  readonly readyState: number;
  onopen: ((ev: Event) => unknown) | null;
  onerror: ((ev: Event) => unknown) | null;
  addEventListener(type: string, listener: (ev: MessageEvent<string>) => void): void;
  close(): void;
}
export type EventSourceFactory = (url: string) => EventSourceLike;

const FEED_LIMIT = 150;
const POLL_MS = 15_000;
const CLOSED = 2;

let factory: EventSourceFactory = (url) => new EventSource(url) as unknown as EventSourceLike;
export function setEventSourceFactory(next: EventSourceFactory): void {
  factory = next;
}

// ------------------------------------------------------------------ store
let state: LiveState = { status: "connecting", events: [], runs: {} };
const subscribers = new Set<() => void>();

function setState(next: LiveState): void {
  state = next;
  subscribers.forEach((fn) => fn());
}

function subscribe(fn: () => void): () => void {
  subscribers.add(fn);
  return () => subscribers.delete(fn);
}

export function useLive<T>(select: (s: LiveState) => T): T {
  return useSyncExternalStore(subscribe, () => select(state));
}

/** Pure reducer: fold one event into the live state (unit-tested). */
export function reduceLive(prev: LiveState, event: StudioEvent): LiveState {
  const events = [event, ...prev.events].slice(0, FEED_LIMIT);
  const runId = typeof event.data.run_id === "number" ? event.data.run_id : null;
  if (runId === null || !event.type.startsWith("run.") || event.type === "run.log") {
    return { ...prev, events };
  }
  const current: LiveRun = prev.runs[runId] ?? {
    runId,
    bookId: event.book_id,
    project: event.project,
    status: "running",
    stage: null,
    done: 0,
    total: 0,
    updated: event.ts,
  };
  const d = event.data;
  const next: LiveRun = {
    ...current,
    updated: event.ts,
    kind: (d.kind as RunKind | undefined) ?? current.kind,
    stage: typeof d.stage === "string" ? d.stage : current.stage,
    done: typeof d.done === "number" ? d.done : current.done,
    total: typeof d.total === "number" ? d.total : typeof d.pages === "number" ? d.pages : current.total,
    status: event.type === "run.finished" ? ((d.status as RunStatus | undefined) ?? "succeeded") : "running",
    error: typeof d.error === "string" && d.error ? d.error : current.error,
  };
  return { ...prev, events, runs: { ...prev.runs, [runId]: next } };
}

// ------------------------------------------------------------------ connection
function setPolling(client: QueryClient, on: boolean): void {
  const defaults = client.getDefaultOptions();
  client.setDefaultOptions({
    ...defaults,
    queries: { ...defaults.queries, refetchInterval: on ? POLL_MS : false },
  });
}

/**
 * Open the app-wide stream; returns a disposer. Reconnects with back-off when the
 * browser gives up (readyState CLOSED); the browser's own retry handles the rest
 * and sends `Last-Event-ID` for us.
 */
export function connectEvents(client: QueryClient): () => void {
  let source: EventSourceLike | null = null;
  let retry: ReturnType<typeof setTimeout> | undefined;
  let attempt = 0;
  let wasDown = false;
  let disposed = false;

  const onMessage = (ev: MessageEvent<string>) => {
    const event = parseEvent(ev.data, ev.lastEventId || undefined);
    if (!event) return;
    applyPlan(client, planEvent(event));
    setState(reduceLive(state, event));
  };

  const open = () => {
    if (disposed) return;
    source = factory(media.eventsUrl({}));
    for (const type of EVENT_TYPES) source.addEventListener(type, onMessage);
    source.onopen = () => {
      attempt = 0;
      setPolling(client, false);
      if (wasDown) void client.invalidateQueries();
      wasDown = false;
      setState({ ...state, status: "open" });
    };
    source.onerror = () => {
      wasDown = true;
      setPolling(client, true);
      setState({ ...state, status: source?.readyState === CLOSED ? "down" : "connecting" });
      if (source?.readyState === CLOSED) {
        source.close();
        attempt += 1;
        retry = setTimeout(open, Math.min(30_000, 1000 * 2 ** attempt));
      }
    };
  };

  open();
  return () => {
    disposed = true;
    clearTimeout(retry);
    source?.close();
  };
}
