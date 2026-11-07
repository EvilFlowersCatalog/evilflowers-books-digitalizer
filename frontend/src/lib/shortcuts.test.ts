import { describe, expect, it, vi } from "vitest";
import { annotationBBox, regionToAnnotation } from "../features/studio/annotations";
import type { RegionOut } from "../api/types";
import { approvalWarning, confirmApproval } from "./approval";
import { nextDispute, rectToBBox, relabel, stepRegion } from "./regions";
import { reviewCommand, studioCommand } from "./shortcuts";

describe("studioCommand (page studio keys)", () => {
  it.each([
    ["t", "text"],
    ["f", "formula"],
    ["b", "table"],
    ["p", "picture"],
    ["s", "stamp"],
    ["F", "formula"],
  ])("%s relabels the selected region as %s", (key, kind) => {
    expect(studioCommand({ key }, true)).toEqual({ type: "relabel", kind });
  });

  it("x rejects and Enter confirms the selection", () => {
    expect(studioCommand({ key: "x" }, true)).toEqual({ type: "reject" });
    expect(studioCommand({ key: "Enter" }, true)).toEqual({ type: "confirm" });
  });

  it("region keys do nothing without a selection", () => {
    for (const key of ["t", "f", "x", "Enter"]) expect(studioCommand({ key }, false)).toBeNull();
  });

  it("arrows page through leaves with or without a selection", () => {
    expect(studioCommand({ key: "ArrowLeft" }, false)).toEqual({ type: "page", delta: -1 });
    expect(studioCommand({ key: "ArrowRight" }, true)).toEqual({ type: "page", delta: 1 });
  });

  it("is silent while typing in a form field or with modifiers", () => {
    expect(studioCommand({ key: "t", targetTag: "TEXTAREA" }, true)).toBeNull();
    expect(studioCommand({ key: "ArrowLeft", targetTag: "input" }, true)).toBeNull();
    expect(studioCommand({ key: "s", targetEditable: true }, true)).toBeNull();
    expect(studioCommand({ key: "s", metaKey: true }, true)).toBeNull();
    expect(studioCommand({ key: "x", ctrlKey: true }, true)).toBeNull();
  });

  it("unknown keys are ignored", () => {
    expect(studioCommand({ key: "q" }, true)).toBeNull();
  });

  it("leaves Enter and Space to a focused button, link or tab", () => {
    expect(studioCommand({ key: "Enter", targetTag: "BUTTON" }, true)).toBeNull();
    expect(studioCommand({ key: " ", targetTag: "button" }, true)).toBeNull();
    expect(studioCommand({ key: "Enter", targetTag: "A" }, true)).toBeNull();
    expect(studioCommand({ key: "Enter", targetTag: "DIV", targetRole: "tab" }, true)).toBeNull();
    // a focused region row (role option) is not activatable: Enter confirms it
    expect(studioCommand({ key: "Enter", targetTag: "LI", targetRole: "option" }, true)).toEqual({ type: "confirm" });
    // letters and arrows still work from a focused button
    expect(studioCommand({ key: "x", targetTag: "BUTTON" }, true)).toEqual({ type: "reject" });
    expect(studioCommand({ key: "ArrowRight", targetTag: "BUTTON" }, false)).toEqual({ type: "page", delta: 1 });
  });

  it("Escape stops drawing before it deselects", () => {
    expect(studioCommand({ key: "Escape" }, { hasSelection: true, drawing: true })).toEqual({ type: "cancelDraw" });
    expect(studioCommand({ key: "Escape" }, { hasSelection: true })).toEqual({ type: "deselect" });
    expect(studioCommand({ key: "Escape" }, { hasSelection: false })).toBeNull();
  });

  it("n/N step through regions and d jumps to the next dispute, selection or not", () => {
    expect(studioCommand({ key: "n" }, false)).toEqual({ type: "region", delta: 1 });
    expect(studioCommand({ key: "N" }, true)).toEqual({ type: "region", delta: -1 });
    expect(studioCommand({ key: "d" }, false)).toEqual({ type: "nextDisputed" });
  });
});

describe("region navigation", () => {
  const r = (id: number, status: RegionOut["status"]) => ({ id, status }) as RegionOut;
  const regions = [r(1, "proposed"), r(2, "rejected"), r(3, "disputed"), r(4, "confirmed"), r(5, "disputed")];

  it("steps over rejected regions and wraps", () => {
    expect(stepRegion(regions, null, 1)).toBe(1);
    expect(stepRegion(regions, null, -1)).toBe(5);
    expect(stepRegion(regions, 1, 1)).toBe(3);
    expect(stepRegion(regions, 5, 1)).toBe(1);
    expect(stepRegion([], null, 1)).toBeNull();
  });

  it("finds the next dispute on the page, then on the next page that has one", () => {
    const pages = [
      { id: 10, index: 0, disputed: 2 },
      { id: 11, index: 1, disputed: 0 },
      { id: 12, index: 2, disputed: 1 },
    ];
    expect(nextDispute(regions, null, pages, 10)).toEqual({ region: 3 });
    expect(nextDispute(regions, 3, pages, 10)).toEqual({ region: 5 });
    expect(nextDispute(regions, 5, pages, 10)).toEqual({ page: 12 });
    expect(nextDispute([], null, pages, 12)).toEqual({ page: 10 });          // wraps around
    expect(nextDispute(regions, 5, [{ id: 10, index: 0, disputed: 2 }], 10)).toEqual({ region: 3 });
    expect(nextDispute([], null, [{ id: 10, index: 0 }], 10)).toBeNull();
  });
});

describe("approval guard", () => {
  it("asks only when a book still has disputed regions", () => {
    expect(approvalWarning([{ slug: "a", disputed: 0 }])).toBeNull();
    expect(approvalWarning([{ slug: "a", title: "Vektory", disputed: 1 }])).toBe(
      '"Vektory" has 1 disputed region nobody has decided. Approve and publish anyway?',
    );
    expect(approvalWarning([{ slug: "a", disputed: 2 }, { slug: "b", disputed: 3 }])).toMatch(/^2 books have 5 disputed regions/);
    const ask = vi.fn(() => false);
    expect(confirmApproval([{ slug: "a", disputed: 1 }], ask)).toBe(false);
    expect(confirmApproval([{ slug: "a" }], ask)).toBe(true);
    expect(ask).toHaveBeenCalledTimes(1);
  });
});

describe("reviewCommand (review queue keys)", () => {
  it("maps j/k/a/o", () => {
    expect(reviewCommand({ key: "j" })).toEqual({ type: "move", delta: 1 });
    expect(reviewCommand({ key: "k" })).toEqual({ type: "move", delta: -1 });
    expect(reviewCommand({ key: "a" })).toEqual({ type: "approve" });
    expect(reviewCommand({ key: "o" })).toEqual({ type: "open" });
    expect(reviewCommand({ key: "a", targetTag: "INPUT" })).toBeNull();
  });
});

describe("relabel (one-key decision)", () => {
  it("sets the kind, its default action, and confirms", () => {
    expect(relabel("stamp")).toEqual({ kind: "stamp", action: "remove", status: "confirmed" });
    expect(relabel("formula")).toEqual({ kind: "formula", action: "reocr", status: "confirmed" });
    expect(relabel("picture")).toEqual({ kind: "picture", action: "as_image", status: "confirmed" });
    expect(relabel("text")).toEqual({ kind: "text", action: "keep", status: "confirmed" });
  });
});

describe("region geometry", () => {
  const region = { id: 12, bbox: [100, 200, 400, 260] } as RegionOut;

  it("round-trips a region through an Annotorious rectangle", () => {
    const a = regionToAnnotation(region);
    expect(a.id).toBe("12");
    expect(annotationBBox(a)).toEqual([100, 200, 400, 260]);
  });

  it("clamps drawn boxes to the page and never produces an empty box", () => {
    expect(rectToBBox({ x: -5.4, y: 10.6, w: 3000, h: 0.2 }, 2480, 3508)).toEqual([0, 11, 2480, 12]);
  });
});
