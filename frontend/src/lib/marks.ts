/**
 * Scan-mark split rule (#14), pure and unit-tested: the API answers 400 when no picked leaf
 * carries the mark, or when the split would take every leaf (the group must keep one).
 */

/** The picked leaves the group really occurs on, sorted. */
export function splitLeaves(onLeaves: number[], picked: Iterable<number>): number[] {
  const on = new Set(onLeaves);
  return [...new Set(picked)].filter((l) => on.has(l)).sort((a, b) => a - b);
}

export function canSplit(onLeaves: number[], picked: Iterable<number>): boolean {
  const n = splitLeaves(onLeaves, picked).length;
  return n > 0 && n < new Set(onLeaves).size;
}
