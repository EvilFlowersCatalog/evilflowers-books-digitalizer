import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { ApiError, buildUrl, setOperatorSource, setTransport, type Transport } from "./client";
import { api } from "./endpoints";
import { operatorHeader } from "./operator";

interface Call {
  url: string;
  init: RequestInit;
}

function recorder(status = 200, body: unknown = {}): { calls: Call[]; transport: Transport } {
  const calls: Call[] = [];
  const transport: Transport = async (url, init) => {
    calls.push({ url, init });
    return new Response(status === 204 ? null : JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  };
  return { calls, transport };
}

const headers = (c: Call) => c.init.headers as Record<string, string>;

describe("buildUrl", () => {
  it("prefixes /api/v1 and drops empty query values", () => {
    expect(buildUrl("/books", { project: "stu", status: undefined, q: "", flagged: true, offset: 0 })).toBe(
      "/api/v1/books?project=stu&flagged=true&offset=0",
    );
  });
});

describe("request", () => {
  beforeEach(() => setOperatorSource(() => "Ľubica Šťastná"));
  afterEach(() => setOperatorSource(() => ""));

  it("sends the operator percent-encoded (fetch rejects non-Latin-1 headers)", async () => {
    const { calls, transport } = recorder(200, []);
    setTransport(transport);
    await api.projects();
    const value = headers(calls[0]!)["X-Operator"]!;
    expect(value).toBe(operatorHeader("Ľubica Šťastná"));
    expect(/^[\x20-\x7e]+$/.test(value)).toBe(true);
    expect(decodeURIComponent(value)).toBe("Ľubica Šťastná");
  });

  it("omits X-Operator when no name is set", async () => {
    setOperatorSource(() => "");
    const { calls, transport } = recorder(200, []);
    setTransport(transport);
    await api.projects();
    expect(headers(calls[0]!)["X-Operator"]).toBeUndefined();
  });

  it("serialises bodies as JSON with the right method and path", async () => {
    const { calls, transport } = recorder(202, { id: 7 });
    setTransport(transport);
    const run = await api.createRun(3, { kind: "rerun", pages: [6], interactive: true });
    expect(run).toEqual({ id: 7 });
    expect(calls[0]!.url).toBe("/api/v1/books/3/runs");
    expect(calls[0]!.init.method).toBe("POST");
    expect(JSON.parse(calls[0]!.init.body as string)).toEqual({ kind: "rerun", pages: [6], interactive: true });
    expect(headers(calls[0]!)["Content-Type"]).toBe("application/json");
  });

  it("passes book filters as query parameters", async () => {
    const { calls, transport } = recorder(200, { items: [], total: 0 });
    setTransport(transport);
    await api.books({ project: "stu-local", status: "review", sort: "quality_score", flagged: true });
    const url = new URL(calls[0]!.url, "http://x");
    expect(url.pathname).toBe("/api/v1/books");
    expect(Object.fromEntries(url.searchParams)).toEqual({ project: "stu-local", status: "review", sort: "quality_score", flagged: "true" });
  });

  it("maps {detail} errors to ApiError (409 = state transition)", async () => {
    setTransport(recorder(409, { detail: "book cannot go from running to queued" }).transport);
    const err = await api.createRun(1, { kind: "full" }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(409);
    expect((err as ApiError).isConflict).toBe(true);
    expect((err as ApiError).detail).toBe("book cannot go from running to queued");
  });

  it("stringifies structured 422 details", async () => {
    setTransport(recorder(422, { detail: [{ loc: ["body", "bbox"], msg: "empty bbox" }] }).transport);
    const err = (await api.patchRegion(1, { bbox: [5, 5, 5, 5] }).catch((e: unknown) => e)) as ApiError;
    expect(err.status).toBe(422);
    expect(err.detail).toContain("empty bbox");
  });

  it("returns undefined for 204 (region reject never hard-deletes)", async () => {
    const { calls, transport } = recorder(204);
    setTransport(transport);
    await expect(api.rejectRegion(9)).resolves.toBeUndefined();
    expect(calls[0]!.init.method).toBe("DELETE");
    expect(calls[0]!.url).toBe("/api/v1/regions/9");
  });
});
