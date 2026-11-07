/**
 * Approving enqueues render + publish, so one key press can ship a book. A book
 * with open disputed regions gets a confirmation first, wherever it is approved
 * (review queue key and button, books table bulk action, book screen).
 */

export interface Approvable {
  title?: string | null;
  slug: string;
  disputed?: number;
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** The question to ask before approving, or null when nothing is left open. */
export function approvalWarning(books: Approvable[]): string | null {
  const open = books.filter((b) => (b.disputed ?? 0) > 0);
  if (open.length === 0) return null;
  const regions = open.reduce((n, b) => n + (b.disputed ?? 0), 0);
  const which =
    open.length === 1 ? `"${open[0]!.title || open[0]!.slug}" has` : `${plural(open.length, "book")} have`;
  return `${which} ${plural(regions, "disputed region")} nobody has decided. Approve and publish anyway?`;
}

/** `window.confirm` behind {@link approvalWarning}; true when approval may go ahead. */
export function confirmApproval(books: Approvable[], ask: (q: string) => boolean = (q) => window.confirm(q)): boolean {
  const warning = approvalWarning(books);
  return warning === null || ask(warning);
}
