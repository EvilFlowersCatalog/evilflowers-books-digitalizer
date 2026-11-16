/**
 * Keyboard of the labelling screen (pure, unit-tested): digits pick a label, `s` skips.
 * Digit `1` is the first label, … `9` the ninth, `0` the tenth.
 */

import type { KeyLike } from "../../lib/shortcuts";

export type LabelCommand = { type: "pick"; index: number } | { type: "skip" } | { type: "submit" };

export function labelCommand(ev: KeyLike, count: number): LabelCommand | null {
  if (ev.ctrlKey || ev.metaKey || ev.altKey || ev.targetEditable) return null;
  if (ev.targetTag === "INPUT" || ev.targetTag === "TEXTAREA" || ev.targetTag === "SELECT") return null;
  if (ev.key === "s") return { type: "skip" };
  if (ev.key === "Enter") return { type: "submit" };
  if (/^[0-9]$/.test(ev.key)) {
    const index = ev.key === "0" ? 9 : Number(ev.key) - 1;
    return index < count ? { type: "pick", index } : null;
  }
  return null;
}

/** The key shown next to the n-th (0-based) label, or "" past the tenth. */
export const keyFor = (index: number): string => (index < 9 ? String(index + 1) : index === 9 ? "0" : "");

/** Top level of a UDC class: its first digit (as `studio.labelling.udc_top`). */
export function udcTop(term: string): string {
  const m = /\d/.exec(term);
  return m ? m[0] : "?";
}
