/**
 * Title-page form (#15), pure and unit-tested: the draft the operator edits, the confirm body
 * it becomes, and the outcome rule of `studio/application/title_page.py` (the mock reuses it).
 */

import type { TitleConfirmIn, TitleField, TitleOutcome, TitleSuggestion } from "../api/types";

export type TitleDraft = Partial<Record<TitleField, string>>;

/** A suggested value as the input shows it: authors one per line, nothing = "". */
export function valueText(value: string | string[] | null | undefined): string {
  if (Array.isArray(value)) return value.join("\n");
  return value ?? "";
}

/** One input per suggested field, prefilled with the decider's value. */
export function titleDraft(suggestions: TitleSuggestion[]): TitleDraft {
  return Object.fromEntries(suggestions.map((s) => [s.field, valueText(s.value)]));
}

/**
 * The operator's final values for the suggested fields. An empty input is `null` ("the page
 * does not say it"); authors are split by lines, blank lines dropped.
 */
export function confirmBody(draft: TitleDraft): TitleConfirmIn {
  const body: TitleConfirmIn = {};
  for (const [field, raw] of Object.entries(draft) as [TitleField, string | undefined][]) {
    if (field === "authors") {
      const authors = (raw ?? "").split("\n").map((x) => x.trim()).filter(Boolean);
      body.authors = authors.length ? authors : null;
    } else {
      const text = (raw ?? "").trim();
      body[field] = text || null;
    }
  }
  return body;
}

/** `title_page._norm`: NFKC, casefold, commas as spaces, whitespace collapsed; lists joined by "; ". */
function norm(value: unknown): string {
  const text = Array.isArray(value) ? value.map(String).join("; ") : String(value ?? "");
  return text.normalize("NFKC").toLowerCase().replace(/,/g, " ").split(/\s+/).filter(Boolean).join(" ");
}

/** `title_page._outcome`: empty final = none; same as the suggestion = accepted; else changed. */
export function titleOutcome(suggested: unknown, final: unknown): TitleOutcome {
  if (!norm(final)) return "none";
  return norm(suggested) === norm(final) ? "accepted" : "changed";
}

/** The decider's probability as a percentage, or "—". */
export const pct = (p: number | null | undefined): string => (p === null || p === undefined ? "—" : `${Math.round(p * 100)} %`);
