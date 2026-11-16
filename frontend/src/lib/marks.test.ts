import { describe, expect, it } from "vitest";
import { canSplit, splitLeaves } from "./marks";

describe("mark-group split", () => {
  const on = [2, 3, 5, 7];

  it("keeps only leaves the group occurs on, sorted", () => {
    expect(splitLeaves(on, [7, 4, 2, 2])).toEqual([2, 7]);
  });

  it("refuses an empty split and one that would empty the group", () => {
    expect(canSplit(on, [])).toBe(false);
    expect(canSplit(on, [4])).toBe(false);
    expect(canSplit(on, [2, 3, 5, 7])).toBe(false);
    expect(canSplit(on, [3, 5])).toBe(true);
    expect(canSplit([9], [9])).toBe(false);
  });
});
