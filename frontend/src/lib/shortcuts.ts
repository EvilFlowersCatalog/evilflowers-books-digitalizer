/**
 * Keyboard maps of the page studio and the review queue (spec, SPA section).
 *
 * Pure functions from a key event to a command, so the mapping is unit-tested
 * without a DOM and the screens only execute commands. Keys typed into a form
 * field never trigger a shortcut — operators edit OCR text on the same screen.
 */

import type { RegionKind } from "../api/types";

export interface KeyLike {
  key: string;
  ctrlKey?: boolean;
  metaKey?: boolean;
  altKey?: boolean;
  /** tag name of the focused element (`INPUT`, `TEXTAREA`, …) */
  targetTag?: string;
  targetEditable?: boolean;
  /** ARIA role of the focused element, if any */
  targetRole?: string;
}

export type StudioCommand =
  | { type: "relabel"; kind: RegionKind }
  | { type: "reject" }
  | { type: "confirm" }
  | { type: "page"; delta: -1 | 1 }
  | { type: "region"; delta: -1 | 1 }
  | { type: "nextDisputed" }
  | { type: "cancelDraw" }
  | { type: "deselect" };

/** What the studio is doing right now, as far as keys care. */
export interface StudioKeyState {
  hasSelection: boolean;
  drawing?: boolean;
}

export type ReviewCommand = { type: "move"; delta: -1 | 1 } | { type: "approve" } | { type: "open" };

/** `t` text, `f` formula, `b` table, `p` picture, `s` stamp. */
export const RELABEL_KEYS: Readonly<Record<string, RegionKind>> = {
  t: "text",
  f: "formula",
  b: "table",
  p: "picture",
  s: "stamp",
};

const FORM_TAGS = new Set(["INPUT", "TEXTAREA", "SELECT"]);
const ACTIVATABLE_TAGS = new Set(["BUTTON", "A", "SUMMARY"]);
const ACTIVATABLE_ROLES = new Set(["button", "tab", "link", "menuitem", "checkbox", "switch"]);
const ACTIVATION_KEYS = new Set(["Enter", " "]);

export function isTyping(ev: KeyLike): boolean {
  return Boolean(ev.targetEditable) || (ev.targetTag !== undefined && FORM_TAGS.has(ev.targetTag.toUpperCase()));
}

/**
 * Enter/Space on a focused button or link must activate *it*: a window-level
 * shortcut calling preventDefault would otherwise confirm a region when the
 * operator tabbed to "Reject", or open the wrong book from the queue.
 */
export function activatesTarget(ev: KeyLike): boolean {
  if (!ACTIVATION_KEYS.has(ev.key)) return false;
  const tag = ev.targetTag?.toUpperCase();
  return (tag !== undefined && ACTIVATABLE_TAGS.has(tag)) || (ev.targetRole !== undefined && ACTIVATABLE_ROLES.has(ev.targetRole));
}

function plain(ev: KeyLike): boolean {
  return !ev.ctrlKey && !ev.metaKey && !ev.altKey && !isTyping(ev) && !activatesTarget(ev);
}

/**
 * Region commands need a selected region; page and region navigation do not.
 * `n`/`N` step through the page's regions, `d` jumps to the next disputed one
 * (crossing to the next page that has disputes). Returns null for anything that
 * is not a studio shortcut.
 */
export function studioCommand(ev: KeyLike, state: StudioKeyState | boolean): StudioCommand | null {
  const { hasSelection, drawing = false } = typeof state === "boolean" ? { hasSelection: state } : state;
  if (!plain(ev)) return null;
  switch (ev.key) {
    case "ArrowLeft":
      return { type: "page", delta: -1 };
    case "ArrowRight":
      return { type: "page", delta: 1 };
    case "Escape":
      if (drawing) return { type: "cancelDraw" };
      return hasSelection ? { type: "deselect" } : null;
    case "n":
      return { type: "region", delta: 1 };
    case "N":
      return { type: "region", delta: -1 };
    case "d":
      return { type: "nextDisputed" };
  }
  if (!hasSelection) return null;
  if (ev.key === "Enter") return { type: "confirm" };
  const key = ev.key.toLowerCase();
  if (key === "x") return { type: "reject" };
  const kind = RELABEL_KEYS[key];
  return kind ? { type: "relabel", kind } : null;
}

/** `j`/`k` move, `a` approve, `o` (or Enter) open. */
export function reviewCommand(ev: KeyLike): ReviewCommand | null {
  if (!plain(ev)) return null;
  switch (ev.key) {
    case "j":
    case "ArrowDown":
      return { type: "move", delta: 1 };
    case "k":
    case "ArrowUp":
      return { type: "move", delta: -1 };
    case "a":
      return { type: "approve" };
    case "o":
    case "Enter":
      return { type: "open" };
    default:
      return null;
  }
}

/** DOM adapter: the fields `studioCommand`/`reviewCommand` read from a KeyboardEvent. */
export function keyLike(ev: KeyboardEvent): KeyLike {
  const target = ev.target as HTMLElement | null;
  return {
    key: ev.key,
    ctrlKey: ev.ctrlKey,
    metaKey: ev.metaKey,
    altKey: ev.altKey,
    targetTag: target?.tagName,
    targetEditable: target?.isContentEditable ?? false,
    targetRole: target?.getAttribute?.("role") ?? undefined,
  };
}
