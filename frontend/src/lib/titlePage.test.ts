import { describe, expect, it } from "vitest";
import type { TitleSuggestion } from "../api/types";
import { confirmBody, pct, titleDraft, titleOutcome } from "./titlePage";

const sug = (field: TitleSuggestion["field"], value: TitleSuggestion["value"]): TitleSuggestion => ({
  field,
  metadata_field: field,
  value,
  p: 0.5,
  line: null,
  leaf: null,
});

describe("titleDraft", () => {
  it("prefills one input per suggestion, authors one per line, nothing as empty", () => {
    expect(titleDraft([sug("title", "Fyzika I"), sug("authors", ["Hajko, V.", "Krempaský, J."]), sug("year", null)])).toEqual({
      title: "Fyzika I",
      authors: "Hajko, V.\nKrempaský, J.",
      year: "",
    });
  });
});

describe("confirmBody", () => {
  it("sends empty inputs as null (the page does not say it)", () => {
    expect(confirmBody({ title: "  ", publisher: "", year: "1983" })).toEqual({ title: null, publisher: null, year: "1983" });
  });

  it("splits authors by lines, trims and drops blank lines", () => {
    expect(confirmBody({ authors: " Hajko, V. \n\n Krempaský, J.\n" })).toEqual({ authors: ["Hajko, V.", "Krempaský, J."] });
    expect(confirmBody({ authors: "\n  \n" })).toEqual({ authors: null });
  });

  it("only carries the fields of the draft (extra keys are rejected by the API)", () => {
    expect(Object.keys(confirmBody({ title: "x" }))).toEqual(["title"]);
  });
});

describe("titleOutcome", () => {
  it("mirrors title_page._outcome", () => {
    expect(titleOutcome("Fyzika I", "  fyzika   i ")).toBe("accepted");
    expect(titleOutcome(["Hajko, V."], ["Hajko V."])).toBe("accepted");
    expect(titleOutcome("Alfa", "Alfa, Bratislava")).toBe("changed");
    expect(titleOutcome(null, "1983")).toBe("changed");
    expect(titleOutcome("1983", null)).toBe("none");
    expect(titleOutcome("1983", [])).toBe("none");
  });
});

describe("pct", () => {
  it("formats p as a percentage", () => {
    expect(pct(0.836)).toBe("84 %");
    expect(pct(null)).toBe("—");
  });
});
