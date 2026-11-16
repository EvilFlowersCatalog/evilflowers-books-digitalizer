import { describe, expect, it } from "vitest";
import type { LabelTaskOut, MarkGroupOut, TitlePageOut } from "../api/types";
import { MockServer } from "./server";

/** One request through the mock transport, as the client sends it. */
async function call<T>(server: MockServer, method: string, path: string, body?: unknown): Promise<{ status: number; data: T }> {
  const init: RequestInit = { method, headers: { "X-Operator": "jana" } };
  if (body !== undefined) init.body = JSON.stringify(body);
  const res = await server.transport(`/api/v1${path}`, init);
  const text = await res.text();
  return { status: res.status, data: (text ? JSON.parse(text) : undefined) as T };
}

describe("mock title page (#15)", () => {
  it("confirms: metadata written, outcomes per field, labelling cases counted", async () => {
    const server = new MockServer();
    const before = await call<TitlePageOut>(server, "GET", "/books/1/title-page");
    expect(before.data.suggestions.length).toBeGreaterThan(0);
    expect(before.data.confirmed).toBeNull();

    const res = await call<TitlePageOut>(server, "POST", "/books/1/title-page/confirm", {
      title: "Fyzika I",
      authors: ["Hajko, Vladimír"],
      publisher: null,
      year: "1983",
    });
    expect(res.status).toBe(200);
    const fields = res.data.confirmed?.fields;
    expect(fields?.title?.outcome).toBe("accepted");
    expect(fields?.authors?.outcome).toBe("changed");
    expect(fields?.publisher?.outcome).toBe("none");
    expect(fields?.year?.outcome).toBe("changed");
    expect(res.data.confirmed?.labeller).toBe("jana");

    const book = server.db.books.find((b) => b.id === 1)!;
    expect(book.metadata.title).toBe("Fyzika I");
    expect(book.metadata.creator).toEqual(["Hajko, Vladimír"]);
    expect(book.metadata.publisher).toBe("Bratislava : Alfa"); // null never clears a value
    expect(book.metadata.date).toBe("1983");

    const tasks = await call<LabelTaskOut[]>(server, "GET", "/labelling/tasks");
    expect(tasks.data.find((t) => t.name === "title_page")?.labelled).toBe(3 + 4); // book 5 + book 1
  });

  it("rejects extra keys and books without suggestions", async () => {
    const server = new MockServer();
    expect((await call(server, "POST", "/books/1/title-page/confirm", { title: "x", isbn: "1" })).status).toBe(422);
    expect((await call(server, "POST", "/books/2/title-page/confirm", { title: "x" })).status).toBe(400);
  });
});

describe("mock scan-mark groups (#14)", () => {
  it("lists most leaves first, decides, splits", async () => {
    const server = new MockServer();
    const list = (await call<MarkGroupOut[]>(server, "GET", "/books/1/mark-groups")).data;
    expect(list.map((g) => g.leaves)).toEqual([...list.map((g) => g.leaves)].sort((a, b) => b - a));
    const stamp = list[0]!;
    expect(stamp.on_leaves).toEqual([2, 3, 5, 7, 9]);

    const kept = await call<MarkGroupOut>(server, "PATCH", `/mark-groups/${stamp.id}`, { band: "keep" });
    expect(kept.data).toMatchObject({ band: "keep", source: "operator", decision: "content", p: null });

    expect((await call(server, "POST", `/mark-groups/${stamp.id}/split`, { leaves: [4] })).status).toBe(400);
    expect((await call(server, "POST", `/mark-groups/${stamp.id}/split`, { leaves: stamp.on_leaves })).status).toBe(400);
    const split = await call<MarkGroupOut>(server, "POST", `/mark-groups/${stamp.id}/split`, { leaves: [7, 9] });
    expect(split.status).toBe(201);
    expect(split.data).toMatchObject({ on_leaves: [7, 9], members: 3, band: "review", split_from: stamp.id });

    const after = (await call<MarkGroupOut[]>(server, "GET", "/books/1/mark-groups")).data;
    expect(after.find((g) => g.id === stamp.id)?.on_leaves).toEqual([2, 3, 5]);
  });
});

describe("mock labelling (#7)", () => {
  it("serves scan_mark items with a crop and refuses title_page on this screen", async () => {
    const server = new MockServer();
    const item = await call<{ kind: string; image_url: string | null; payload: { leaves?: number } }>(server, "GET", "/labelling/tasks/scan_mark/next");
    expect(item.data.kind).toBe("mark_group");
    expect(item.data.image_url).toMatch(/^data:image/);
    expect(item.data.payload.leaves).toBeGreaterThan(0);
    expect((await call(server, "GET", "/labelling/tasks/title_page/next")).status).toBe(400);
    expect((await call(server, "POST", "/labelling/tasks/scan_mark/samples?n=100")).status).toBe(200);
  });
});
