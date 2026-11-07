import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";
import { applyPlan, parseEvent, planEvent } from "./events";
import { reduceLive, type LiveState } from "./live";
import { qk } from "./queryKeys";
import type { BookDetail, EventType, RunOut, StudioEvent } from "./types";

const ev = (type: EventType, data: Record<string, unknown> = {}, book_id: number | null = 1): StudioEvent => ({
  type,
  project: "stu-local",
  book_id,
  data,
  ts: "2026-11-07T10:00:00+00:00",
});

const run = (over: Partial<RunOut> = {}): RunOut => ({
  id: 5,
  book_id: 1,
  kind: "full",
  status: "running",
  interactive: false,
  pages: null,
  stage: "clean",
  done: 1,
  total: 10,
  error: null,
  log: [],
  created_by: null,
  created_at: "",
  started_at: null,
  finished_at: null,
  ...over,
});

describe("parseEvent", () => {
  it("parses the events.py wire format", () => {
    const raw = JSON.stringify({ type: "page.updated", project: "stu", book_id: 3, data: { page_id: 9 }, ts: "t" });
    expect(parseEvent(raw, "42")).toEqual({ type: "page.updated", project: "stu", book_id: 3, data: { page_id: 9 }, ts: "t", id: "42" });
  });

  it("rejects unknown types and garbage", () => {
    expect(parseEvent(JSON.stringify({ type: "nope", project: "x" }))).toBeNull();
    expect(parseEvent("{not json")).toBeNull();
  });
});

describe("planEvent", () => {
  it("page.updated invalidates the page and the book's pages grid", () => {
    expect(planEvent(ev("page.updated", { page_id: 9, index: 6, version: 2 })).invalidate).toEqual([qk.page(9), qk.pages(1)]);
  });

  it("regions.updated invalidates the same page family", () => {
    expect(planEvent(ev("regions.updated", { page_id: 9, disputed: 0 })).invalidate).toEqual([qk.page(9), qk.pages(1)]);
  });

  it("book outcomes refresh projects, every books list and the book", () => {
    for (const type of ["book.status", "book.review_needed", "book.ready", "book.published"] as const) {
      expect(planEvent(ev(type)).invalidate).toEqual([qk.projects(), qk.booksAll(), qk.book(1)]);
    }
  });

  it("run.finished also refreshes the run and the pages", () => {
    expect(planEvent(ev("run.finished", { run_id: 5, status: "succeeded" })).invalidate).toEqual([
      qk.run(5),
      qk.projects(),
      qk.booksAll(),
      qk.book(1),
      qk.pages(1),
    ]);
  });

  it("run.progress and run.log patch instead of refetching", () => {
    const progress = planEvent(ev("run.progress", { run_id: 5, stage: "ocr", done: 4, total: 10 }));
    expect(progress.invalidate).toEqual([]);
    expect(progress.runPatch).toEqual({ runId: 5, bookId: 1, fields: { status: "running", stage: "ocr", done: 4, total: 10 } });
    const log = planEvent(ev("run.log", { run_id: 5, level: "warning", message: "leaf 6 disputed" }));
    expect(log.runPatch?.log).toEqual({ ts: "2026-11-07T10:00:00+00:00", level: "warning", message: "leaf 6 disputed" });
  });

  it("project-level events without a book do not touch book keys", () => {
    expect(planEvent(ev("import.finished", { books: 3 }, null)).invalidate).toEqual([qk.projects(), qk.booksAll()]);
    expect(planEvent(ev("import.started", {}, null)).invalidate).toEqual([]);
  });
});

describe("applyPlan", () => {
  it("patches the cached run and the run inside the cached book detail", () => {
    const client = new QueryClient();
    client.setQueryData(qk.run(5), run());
    client.setQueryData<Partial<BookDetail>>(qk.book(1), { runs: [run()] });
    applyPlan(client, planEvent(ev("run.progress", { run_id: 5, stage: "ocr", done: 7, total: 10 })));
    applyPlan(client, planEvent(ev("run.log", { run_id: 5, level: "info", message: "hello" })));
    const cached = client.getQueryData<RunOut>(qk.run(5))!;
    expect([cached.stage, cached.done, cached.log.map((l) => l.message)]).toEqual(["ocr", 7, ["hello"]]);
    expect(client.getQueryData<BookDetail>(qk.book(1))!.runs[0]!.done).toBe(7);
  });

  it("invalidates by prefix, so every filtered books list goes stale", () => {
    const client = new QueryClient();
    client.setQueryData(qk.books({ project: "a", status: "review" }), { items: [], total: 0 });
    client.setQueryData(qk.books({ project: "a" }), { items: [], total: 0 });
    client.setQueryData(qk.page(9), {});
    applyPlan(client, planEvent(ev("book.status", { status: "approved", previous: "review" })));
    const states = client.getQueryCache().findAll({ queryKey: qk.booksAll() }).map((q) => q.state.isInvalidated);
    expect(states).toEqual([true, true]);
    expect(client.getQueryCache().find({ queryKey: qk.page(9) })!.state.isInvalidated).toBe(false);
  });
});

describe("reduceLive", () => {
  const empty: LiveState = { status: "open", events: [], runs: {} };

  it("tracks run progress until the run finishes", () => {
    let st = reduceLive(empty, ev("run.started", { run_id: 5, kind: "full", pages: 12 }));
    st = reduceLive(st, ev("run.progress", { run_id: 5, stage: "ocr", done: 3, total: 12 }));
    expect(st.runs[5]).toMatchObject({ status: "running", stage: "ocr", done: 3, total: 12, kind: "full" });
    st = reduceLive(st, ev("run.finished", { run_id: 5, status: "failed", error: "boom" }));
    expect(st.runs[5]!.status).toBe("failed");
    expect(st.events).toHaveLength(3);
  });

  it("keeps the feed bounded and newest first", () => {
    let st = empty;
    for (let i = 0; i < 200; i++) st = reduceLive(st, ev("book.discovered", { slug: `b${i}` }));
    expect(st.events).toHaveLength(150);
    expect(st.events[0]!.data.slug).toBe("b199");
  });
});
