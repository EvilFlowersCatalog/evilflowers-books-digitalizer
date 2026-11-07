/**
 * In-memory implementation of the REST table for mock mode. It behaves like the
 * real API where the UI can tell: state transitions, 404/409/422 `{detail}` errors,
 * runs that progress and emit SSE events, re-runs that bump the page version.
 */

import type { Transport } from "../api/client";
import type {
  BookDetail,
  BookOut,
  BookStatus,
  ItemMetadata,
  NoteCreate,
  PageDetail,
  PagePatch,
  RegionCreate,
  RegionOut,
  RegionPatch,
  ReviewCreate,
  RunCreate,
  RunOut,
} from "../api/types";
import { defaultAction } from "../lib/regions";
import { MockBus } from "./bus";
import { FRAME_H, FRAME_W, PROJECT, createDb, type MockBook, type MockDb, type MockPage } from "./fixtures";

class HttpError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(detail);
  }
}

const json = (status: number, body: unknown) =>
  new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

const now = () => new Date().toISOString();
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

type Handler = (params: string[], body: unknown, query: URLSearchParams, operator: string) => unknown;

export class MockServer {
  readonly db: MockDb = createDb();
  readonly bus = new MockBus();
  private readonly routes: [string, RegExp, Handler][] = [];

  constructor() {
    this.registerRoutes();
  }

  // ---------------------------------------------------------------- helpers
  private book(id: number): MockBook {
    const b = this.db.books.find((x) => x.id === id);
    if (!b) throw new HttpError(404, `book ${id} not found`);
    return b;
  }

  private page(id: number): MockPage {
    const p = this.db.pages.find((x) => x.id === id);
    if (!p) throw new HttpError(404, `page ${id} not found`);
    return p;
  }

  private region(id: number): RegionOut {
    const r = this.db.regions.find((x) => x.id === id);
    if (!r) throw new HttpError(404, `region ${id} not found`);
    return r;
  }

  private run(id: number): RunOut {
    const r = this.db.runs.find((x) => x.id === id);
    if (!r) throw new HttpError(404, `run ${id} not found`);
    return r;
  }

  private bookOut(b: MockBook): BookOut {
    const { metadata: _m, quality: _q, ...out } = b;
    return out;
  }

  private setStatus(b: MockBook, status: BookStatus): void {
    if (b.status === status) return;
    const previous = b.status;
    b.status = status;
    b.updated_at = now();
    this.bus.emit("book.status", b.id, { status, previous });
  }

  private pageDetail(p: MockPage): PageDetail {
    const pages = this.db.pages.filter((x) => x.book_id === p.book_id);
    const regions = this.db.regions.filter((r) => r.page_id === p.id);
    const disputed = regions.filter((r) => r.status === "disputed").length;
    p.disputed = disputed;
    const { override, ocr_text, ...page } = p;
    return {
      page: { ...page, disputed },
      regions,
      notes: this.db.notes.filter((n) => n.page_id === p.id),
      override,
      ocr_text,
      frame: {
        path: `/data/raw-scans/fei/E2E_FYZIKA_VEKTORY/scan_${String(Math.floor(p.index / 2) + 1).padStart(4, "0")}.tif`,
        width: FRAME_W,
        height: FRAME_H,
        side: p.index % 2,
        n_sides: 2,
        x0: p.index % 2 ? FRAME_W / 2 : 0,
        x1: p.index % 2 ? FRAME_W : FRAME_W / 2,
        crop_box: [(p.index % 2 ? FRAME_W / 2 : 0) + 60, 66, 2480, 3508],
      },
      prev_id: pages.find((x) => x.index === p.index - 1)?.id ?? null,
      next_id: pages.find((x) => x.index === p.index + 1)?.id ?? null,
    };
  }

  private refreshDisputes(p: MockPage): void {
    const disputed = this.db.regions.filter((r) => r.page_id === p.id && r.status === "disputed").length;
    p.disputed = disputed;
    if (p.status === "disputed" && !disputed) p.status = "analyzed";
    if (disputed) p.status = "disputed";
    this.bus.emit("regions.updated", p.book_id, { page_id: p.id, index: p.index, disputed });
  }

  // ---------------------------------------------------------------- runs
  private createRun(b: MockBook, body: RunCreate, operator: string): RunOut {
    if (b.status === "running" && body.kind === "full") throw new HttpError(409, "book cannot go from running to queued");
    const pages = body.pages ?? null;
    const total = pages?.length ?? b.n_pages ?? 12;
    const run: RunOut = {
      id: ++this.db.seq,
      book_id: b.id,
      kind: body.kind,
      status: "queued",
      interactive: body.interactive ?? false,
      pages,
      stage: null,
      done: 0,
      total,
      error: null,
      log: [],
      created_by: operator || null,
      created_at: now(),
      started_at: null,
      finished_at: null,
      book_title: b.title,
      book_slug: b.slug,
    };
    this.db.runs.push(run);
    if (body.kind !== "rerun") this.setStatus(b, "queued");
    this.bus.emit("book.queued", b.id, { run_id: run.id, kind: run.kind });
    void this.simulate(run, b);
    return run;
  }

  private log(run: RunOut, level: string, message: string): void {
    run.log = [...run.log, { ts: now(), level, message }].slice(-200);
    this.bus.emit("run.log", run.book_id, { run_id: run.id, level, message });
  }

  private progress(run: RunOut, stage: string, done: number): void {
    run.stage = stage;
    run.done = done;
    this.bus.emit("run.progress", run.book_id, { run_id: run.id, stage, done, total: run.total });
  }

  private async simulate(run: RunOut, b: MockBook): Promise<void> {
    const cancelled = () => (run.status as RunOut["status"]) === "cancelled"; // set by the cancel route meanwhile
    await sleep(run.interactive ? 300 : 900);
    if (cancelled()) return;
    run.status = "running";
    run.started_at = now();
    if (run.kind !== "rerun") this.setStatus(b, "running");
    this.bus.emit("run.started", b.id, { run_id: run.id, kind: run.kind, pages: run.total });
    const stages = run.kind === "rerun" ? ["clean", "analyze"] : run.kind === "render" ? ["render"] : run.kind === "publish" ? ["publish"] : ["clean", "ocr", "analyze", "render", "gate"];
    for (const stage of stages) {
      this.log(run, "info", `${stage}: started`);
      for (let done = 1; done <= run.total; done += Math.max(1, Math.ceil(run.total / 4))) {
        await sleep(run.interactive ? 250 : 500);
        if (cancelled()) return;
        this.progress(run, stage, Math.min(done, run.total));
      }
      this.progress(run, stage, run.total);
    }
    this.finishRun(run, b);
  }

  private finishRun(run: RunOut, b: MockBook): void {
    run.status = "succeeded";
    run.finished_at = now();
    for (const p of this.db.pages.filter((x) => x.book_id === b.id && (run.pages ?? []).includes(x.index))) {
      p.version += 1;
      p.status = p.disputed ? "disputed" : "analyzed";
      this.bus.emit("page.updated", b.id, { page_id: p.id, index: p.index, version: p.version, status: p.status });
    }
    this.bus.emit("run.finished", b.id, { run_id: run.id, status: "succeeded" });
    this.log(run, "info", "done");
    if (run.kind === "full") this.gate(b);
    if (run.kind === "publish") this.publish(b, run.created_by);
  }

  private gate(b: MockBook): void {
    const disputed = this.db.regions.filter((r) => this.db.pages.some((p) => p.id === r.page_id && p.book_id === b.id) && r.status === "disputed").length;
    const score = Math.max(0.3, 0.98 - disputed * 0.12);
    b.quality_score = score;
    b.flags = disputed ? [`${disputed} disputed region(s)`] : [];
    b.flagged = disputed > 0;
    b.quality = { ...b.quality, passed: !disputed, flags: b.flags, score };
    if (disputed) {
      this.setStatus(b, "review");
      this.bus.emit("book.review_needed", b.id, { flags: b.flags, score });
    } else {
      this.setStatus(b, "approved");
      this.bus.emit("book.ready", b.id, { score });
    }
  }

  private publish(b: MockBook, author: string | null): void {
    const prev = this.db.publications.filter((p) => p.book_id === b.id).at(-1);
    const entry = prev?.entry_id ?? crypto.randomUUID();
    const pub = { id: ++this.db.seq, book_id: b.id, target: "evilflowers", entry_id: entry, version: (prev?.version ?? 0) + 1, url: `https://dev.evilflowers.elvira.stuba.sk/entries/${entry.slice(0, 8)}`, author, published_at: now() };
    this.db.publications.push(pub);
    this.setStatus(b, "published");
    this.bus.emit("book.published", b.id, { publication_id: pub.id, entry_id: entry, version: pub.version });
  }

  /** Background activity so the dashboard and status bar look alive. */
  startTicker(): void {
    const stages = ["clean", "ocr", "analyze", "render"];
    setInterval(() => {
      for (const run of this.db.runs.filter((r) => r.status === "running" && !r.interactive && r.id < 1000)) {
        let done = run.done + 1 + Math.floor(Math.random() * 3);
        let stage = run.stage ?? "clean";
        if (done >= run.total) {
          done = 0;
          stage = stages[(stages.indexOf(stage) + 1) % stages.length]!;
          this.log(run, "info", `${stage}: started`);
        }
        this.progress(run, stage, done);
        if (Math.random() < 0.15) this.log(run, "info", `${stage}: leaf ${done} ok (${(70 + Math.random() * 25).toFixed(1)} conf)`);
      }
    }, 1400);
  }

  // ---------------------------------------------------------------- routes
  private on(method: string, pattern: string, handler: Handler): void {
    const re = new RegExp(`^${pattern.replace(/:\w+/g, "([^/]+)")}$`);
    this.routes.push([method, re, handler]);
  }

  private registerRoutes(): void {
    const n = Number;
    this.on("GET", "/projects", () => {
      const counts: Partial<Record<BookStatus, number>> = {};
      const collection_counts: Record<string, Partial<Record<BookStatus, number>>> = {};
      for (const b of this.db.books) {
        counts[b.status] = (counts[b.status] ?? 0) + 1;
        const cc = (collection_counts[b.collection ?? ""] ??= {});
        cc[b.status] = (cc[b.status] ?? 0) + 1;
      }
      return [{ ...PROJECT, counts, collection_counts }];
    });
    this.on("GET", "/projects/:p/config", ([p]) => ({
      slug: p,
      name: PROJECT.name,
      book_pattern: "{collection}/{book}",
      frames_glob: "scan_*.tif",
      item_id: "stu-{collection}-{book}",
      language: "slk",
      ocr_language: "slk+eng",
      layout_detectors: ["heuristic"],
      formula_recognizer: "paddleocr-vl",
      outputs: ["pdf", "pdfa", "epub"],
      auto_publish: "gate",
      quality: { min_ocr_conf: 80, max_disputed: 0, max_formula_fallback_ratio: 0.25, max_vlm_rejected: 2 },
      publish: { target: "evilflowers", base_url: "https://dev.evilflowers.elvira.stuba.sk/api/v1", catalog: "{collection}" },
      metadata: { providers: ["excel", "opac"] },
    }));
    this.on("POST", "/projects/:p/imports", (_p, body) => {
      const collection = (body as { collection?: string } | undefined)?.collection ?? null;
      this.bus.emit("import.started", null, { collection });
      setTimeout(() => this.bus.emit("import.finished", null, { collection, books: this.db.books.length, new: 0 }), 1500);
      return { task_id: crypto.randomUUID() };
    });

    this.on("GET", "/books", (_p, _b, q) => this.listBooks(q));
    this.on("GET", "/books/:id", ([id]) => this.bookDetail(this.book(n(id))));
    this.on("PATCH", "/books/:id/metadata", ([id], body) => {
      const b = this.book(n(id));
      const patch = body as Partial<ItemMetadata>;
      if (patch.identifier && patch.identifier !== b.metadata.identifier) throw new HttpError(422, "identifier is permanent");
      Object.assign(b.metadata, patch);
      if (patch.title) b.title = patch.title;
      return b.metadata;
    });
    this.on("GET", "/books/:id/metadata/lookup", ([id]) => {
      const b = this.book(n(id));
      return { title: b.title ?? "", publisher: "Bratislava : Alfa", date: "1983", oclc_id: ["39921187"], language: ["slo"], subject: ["fyzika", "vektorová analýza"] };
    });
    this.on("POST", "/books/:id/runs", ([id], body, _q, op) => this.createRun(this.book(n(id)), body as RunCreate, op));
    this.on("GET", "/runs/:id", ([id]) => this.run(n(id)));
    this.on("POST", "/runs/:id/cancel", ([id]) => {
      const run = this.run(n(id));
      if (run.status !== "queued" && run.status !== "running") throw new HttpError(409, `run is ${run.status}`);
      run.status = "cancelled";
      run.finished_at = now();
      this.bus.emit("run.finished", run.book_id, { run_id: run.id, status: "cancelled" });
      return run;
    });

    this.on("GET", "/books/:id/pages", ([id]) =>
      this.db.pages.filter((p) => p.book_id === n(id)).map((p) => {
        const { override: _o, ocr_text: _t, ...out } = p;
        return out;
      }),
    );
    this.on("GET", "/pages/:id", ([id]) => this.pageDetail(this.page(n(id))));
    this.on("PATCH", "/pages/:id", ([id], body) => {
      const p = this.page(n(id));
      const patch = body as PagePatch;
      if (patch.page_type) p.page_type = patch.page_type;
      if (patch.page_number !== undefined) p.page_number = patch.page_number;
      if (patch.include_in_access !== undefined) p.add_to_access = patch.include_in_access;
      if (patch.override) p.override = patch.override;
      return this.pageDetail(p);
    });
    this.on("POST", "/pages/:id/regions", ([id], body, _q, op) => {
      const p = this.page(n(id));
      const c = body as RegionCreate;
      const region: RegionOut = {
        id: ++this.db.seq,
        page_id: p.id,
        kind: c.kind,
        bbox: c.bbox,
        source: "operator",
        confidence: 1,
        status: "confirmed",
        action: c.action ?? defaultAction(c.kind),
        text: c.text ?? null,
        alternatives: null,
        reason: null,
        author: op || null,
        updated_at: now(),
      };
      this.db.regions.push(region);
      this.refreshDisputes(p);
      return region;
    });
    this.on("PATCH", "/regions/:id", ([id], body, _q, op) => {
      const r = this.region(n(id));
      const patch = body as RegionPatch;
      if (patch.bbox && (patch.bbox[2] <= patch.bbox[0] || patch.bbox[3] <= patch.bbox[1])) throw new HttpError(422, "empty bbox");
      Object.assign(r, patch, { author: op || r.author, updated_at: now() });
      if (patch.kind || patch.bbox) {
        r.source = "operator";
        if (!patch.status) r.status = "confirmed";
      }
      this.refreshDisputes(this.page(r.page_id));
      return r;
    });
    this.on("DELETE", "/regions/:id", ([id]) => {
      const r = this.region(n(id));
      r.status = "rejected";
      this.refreshDisputes(this.page(r.page_id));
      return undefined;
    });
    this.on("POST", "/notes", (_p, body, _q, op) => {
      const c = body as NoteCreate;
      const page = c.page_id ? this.page(c.page_id) : null;
      const bookId = c.book_id ?? page?.book_id;
      if (!bookId) throw new HttpError(422, "book_id or page_id required");
      const note = { id: ++this.db.seq, book_id: bookId, page_id: c.page_id ?? null, region_id: c.region_id ?? null, author: op || null, text: c.text, created_at: now() };
      this.db.notes.push(note);
      return note;
    });
    this.on("POST", "/books/:id/reviews", ([id], body, _q, op) => this.review(this.book(n(id)), body as ReviewCreate, op));
  }

  private review(b: MockBook, body: ReviewCreate, op: string) {
    if (body.decision === "approved" && !["review", "approved", "ok", "published"].includes(b.status))
      throw new HttpError(409, `book cannot go from ${b.status} to approved`);
    const review = { id: ++this.db.seq, book_id: b.id, decision: body.decision, author: op || null, note: body.note ?? null, created_at: now() };
    this.db.reviews.push(review);
    if (body.decision === "approved") {
      this.setStatus(b, "approved");
      b.flagged = false;
      setTimeout(() => this.createRun(b, { kind: "publish" }, op), 800);
    }
    return review;
  }

  private listBooks(q: URLSearchParams): { items: BookOut[]; total: number } {
    const text = (q.get("q") ?? "").toLowerCase();
    let items = this.db.books.filter(
      (b) =>
        (!q.get("collection") || b.collection === q.get("collection")) &&
        (!q.get("status") || b.status === q.get("status")) &&
        (q.get("flagged") !== "true" || b.flagged) &&
        (!text || `${b.title} ${b.slug} ${b.item_id}`.toLowerCase().includes(text)),
    );
    const sort = q.get("sort") ?? "-updated_at";
    const key = sort.replace(/^-/, "") as keyof BookOut;
    const dir = sort.startsWith("-") ? -1 : 1;
    items = [...items].sort((a, c) => {
      const av = a[key] ?? (dir > 0 ? Infinity : -Infinity);
      const cv = c[key] ?? (dir > 0 ? Infinity : -Infinity);
      return (av < cv ? -1 : av > cv ? 1 : 0) * dir;
    });
    const offset = Number(q.get("offset") ?? 0);
    const limit = Number(q.get("limit") ?? 50);
    return { items: items.slice(offset, offset + limit).map((b) => this.bookOut(b)), total: items.length };
  }

  private bookDetail(b: MockBook): BookDetail {
    const kinds = ["pdf", "pdfa", "epub", "text", "hocr", "meta_xml", "scandata_xml", "page_numbers", "images", "files_xml"];
    const built = !["discovered", "queued", "error"].includes(b.status);
    const dir = `/data/digitized/stu-local/${b.item_id}`;
    const name: Record<string, string> = { pdf: ".pdf", pdfa: "_archival.pdf", epub: ".epub", text: "_djvu.txt", hocr: "_hocr.html", meta_xml: "_meta.xml", scandata_xml: "_scandata.xml", page_numbers: "_page_numbers.json", images: "_jp2.zip", files_xml: "_files.xml" };
    return {
      book: this.bookOut(b),
      item_metadata: b.metadata,
      quality: b.quality,
      runs: this.db.runs.filter((r) => r.book_id === b.id).slice(-10).reverse(),
      publications: this.db.publications.filter((p) => p.book_id === b.id),
      reviews: this.db.reviews.filter((r) => r.book_id === b.id),
      notes: this.db.notes.filter((n) => n.book_id === b.id),
      artifacts: kinds.map((k, i) => ({ kind: k, path: `${dir}/${b.item_id}${name[k]}`, exists: built && (k !== "epub" || b.id !== 1), size: built ? 40_000 * (i + 3) ** 3 : null })),
      paths: { source_dir: `/data/raw-scans/${b.slug}`, work_dir: `/data/cache/studio/${b.item_id}`, output_dir: dir },
    };
  }

  // ---------------------------------------------------------------- transport
  transport: Transport = async (url, init) => {
    await sleep(60 + Math.random() * 120);
    const parsed = new URL(url, "http://mock");
    const path = parsed.pathname.replace(/^\/api\/v1/, "");
    const method = (init.method ?? "GET").toUpperCase();
    const headers = (init.headers ?? {}) as Record<string, string>;
    const operator = decodeURIComponent(headers["X-Operator"] ?? "");
    const body: unknown = typeof init.body === "string" ? JSON.parse(init.body) : undefined;
    for (const [m, re, handler] of this.routes) {
      const match = m === method ? re.exec(path) : null;
      if (!match) continue;
      try {
        const result = handler(match.slice(1), body, parsed.searchParams, operator);
        const status = method === "DELETE" ? 204 : method === "POST" && /\/(runs|imports)$/.test(path) ? 202 : 200;
        return json(status, result);
      } catch (e) {
        if (e instanceof HttpError) return json(e.status, { detail: e.detail });
        throw e;
      }
    }
    return json(404, { detail: `no mock route for ${method} ${path}` });
  };
}
