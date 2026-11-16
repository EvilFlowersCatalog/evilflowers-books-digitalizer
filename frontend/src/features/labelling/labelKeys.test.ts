import { describe, expect, it } from "vitest";
import { keyFor, labelCommand, udcTop } from "./labelKeys";

describe("labelCommand", () => {
  it("maps digits to labels, 0 to the tenth", () => {
    expect(labelCommand({ key: "1" }, 10)).toEqual({ type: "pick", index: 0 });
    expect(labelCommand({ key: "0" }, 10)).toEqual({ type: "pick", index: 9 });
    expect(labelCommand({ key: "6" }, 5)).toBeNull();
  });

  it("skips with s, saves with Enter", () => {
    expect(labelCommand({ key: "s" }, 2)).toEqual({ type: "skip" });
    expect(labelCommand({ key: "Enter" }, 2)).toEqual({ type: "submit" });
  });

  it("ignores typing and modified keys", () => {
    expect(labelCommand({ key: "1", targetTag: "INPUT" }, 3)).toBeNull();
    expect(labelCommand({ key: "s", targetEditable: true }, 3)).toBeNull();
    expect(labelCommand({ key: "1", metaKey: true }, 3)).toBeNull();
  });
});

describe("helpers", () => {
  it("shows keys for the first ten labels", () => {
    expect([0, 8, 9, 10].map(keyFor)).toEqual(["1", "9", "0", ""]);
  });

  it("takes the UDC top level from the first digit", () => {
    expect(udcTop("004.8")).toBe("0");
    expect(udcTop("621.3")).toBe("6");
    expect(udcTop("")).toBe("?");
  });
});
