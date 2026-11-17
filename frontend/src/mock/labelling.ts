/**
 * Mock labelling store (#7) for `pnpm dev:mock`: a few items per task, answers per operator,
 * the same rules as the server (skip stored, never exported; one answer per item/task/person).
 */

import type {
  LabelAnswer,
  LabelItemOut,
  LabelSummaryOut,
  LabelTaskName,
  LabelTaskOut,
} from "../api/types";

/** A placeholder crop as a data URI: an `<img src>` never goes through the mock transport. */
export const CROP = (text: string) =>
  `data:image/svg+xml;utf8,${encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" width="360" height="240"><rect width="100%" height="100%" fill="#f8fafc" stroke="#94a3b8"/><text x="50%" y="50%" text-anchor="middle" font-family="sans-serif" font-size="20" fill="#334155">${text}</text></svg>`,
  )}`;

const TASKS: Omit<LabelTaskOut, "items" | "labelled" | "skipped" | "double" | "mine" | "left_for_me">[] = [
  { name: "figure_kind", title: "What is this image?", item_kind: "figure", companion_of: null, target: 200,
    labels: ["chart", "diagram", "photo", "map", "table", "formula", "stamp", "decoration", "scan", "blank"].map((key) => ({ key, hint: key === "stamp" ? "stamp, signature or logo" : null })) },
  { name: "is_data_table", title: "Is this a data table (not a ToC, a form or page layout)?", item_kind: "table", companion_of: null, target: 200,
    labels: [{ key: "yes", hint: null }, { key: "no", hint: "ToC, form or page layout" }] },
  { name: "table_quality", title: "Is the markdown a faithful copy of the table?", item_kind: "table", companion_of: "is_data_table", target: 100,
    labels: [{ key: "correct", hint: null }, { key: "minor_errors", hint: "a few wrong cells" }, { key: "broken", hint: "unusable" }] },
  { name: "section_relation", title: "How is section B related to section A?", item_kind: "section_pair", companion_of: null, target: 200,
    labels: [["same", "explains the same idea"], ["pre", "A is a prerequisite of B"], ["example", "B is an example of A"], ["loose", "loosely related"], ["none", "unrelated"]].map(([key, hint]) => ({ key: key!, hint: hint! })) },
  { name: "subject", title: "Which subject (UDC category) is this book?", item_kind: "entry", companion_of: null, target: 200, labels: [] },
  { name: "scan_mark", title: "Is this mark part of the printed page, or something added to or around it?", item_kind: "mark_group", companion_of: null, target: 200,
    labels: [["stamp", "stamp, signature or logo"], ["sticker", "sticker or label: call number, barcode"], ["bleed_through", "shows through from the other side"],
             ["edge", "scan edge, finger, shadow"], ["content", "part of the printed page"]].map(([key, hint]) => ({ key: key!, hint: hint! })) },
  { name: "title_page", title: "Did the title-page suggestion hold (per field, from confirmed metadata forms)?", item_kind: "book", companion_of: null, target: 200,
    labels: ["accepted", "changed", "none"].map((key) => ({ key, hint: null })) },
];

/** Tasks collected outside this screen (title_page): `next` and `samples` answer 400. */
export const FORM_TASKS: ReadonlySet<LabelTaskName> = new Set(["title_page"]);

const NO_PAIRS = { pairs: 0, agreement: null, kappa: null, by_language: {}, confusion: {} };

const OPTIONS = [
  { id: "cat-004", term: "004.8", label: "Umelá inteligencia" },
  { id: "cat-004-1", term: "004.4", label: "Softvér" },
  { id: "cat-51", term: "51", label: "Matematika" },
  { id: "cat-53", term: "53", label: "Fyzika" },
  { id: "cat-62", term: "621.3", label: "Elektrotechnika" },
];

const ITEMS: LabelItemOut[] = [
  ...[1, 2, 3].map((i) => ({
    id: i, kind: "figure" as const, key: `figure:e1:r${i}`, language: i === 3 ? "en" : "sk",
    ref: { entry_id: "e1", run_id: "run-1", region: `p01${i}-fig01`, region_id: `r${i}` },
    payload: { entry_title: "Vektorová analýza", page: 10 + i, region: `p01${i}-fig01`, printed_caption: `Obr. ${i}. Vektorové pole`, page_text: "Na obrázku vidíme pole…" },
    image_url: CROP(`figure ${i}`), mine: {},
  })),
  { id: 10, kind: "table", key: "table:e1:t1", language: "sk", ref: { entry_id: "e1", run_id: "run-1", region: "p020-tab01", region_id: "t1" },
    payload: { entry_title: "Vektorová analýza", page: 20, region: "p020-tab01", markdown: "| x | f(x) |\n|---|---|\n| 1 | 2 |\n| 2 | 4 |", page_text: "Tabuľka hodnôt…" },
    image_url: CROP("table"), mine: {} },
  { id: 20, kind: "section_pair", key: "section_pair:s1:s2", language: "sk", ref: { entry_id: "e1", section_a: "s1", section_b: "s2" },
    payload: { a: { key: "s1", entry_id: "e1", entry_title: "Vektorová analýza", title: "Úvod", first_page: 1, text: "V tejto kapitole zavedieme vektory…" },
               b: { key: "s2", entry_id: "e1", entry_title: "Vektorová analýza", title: "Gradient", first_page: 12, text: "Gradient skalárneho poľa je vektor…" } },
    image_url: null, mine: {} },
  { id: 30, kind: "entry", key: "entry:e1", language: "sk", ref: { entry_id: "e1", catalog_id: "c1" },
    payload: { title: "Vektorová analýza", summary: "Učebnica pre študentov FEI.", table_of_contents: ["1 Úvod", "2 Gradient", "3 Divergencia"], options: OPTIONS },
    image_url: null, mine: {} },
  ...[["stamp", 2, 5, 6], ["sticker", 1, 2, 2], ["bleed", 6, 2, 3]].map(([text, leaf, leaves, occurrences], i) => ({
    id: 40 + i, kind: "mark_group" as const, key: `mark_group:${700 + i}`, language: "sk",
    ref: { book_id: "1", group_id: String(700 + i), leaf: String(leaf) },
    payload: { title: "Fyzika I — vektory a sily", leaf: Number(leaf), leaves: Number(leaves), occurrences: Number(occurrences) },
    image_url: CROP(`${text} (leaf ${leaf})`), mine: {},
  })),
];

interface MockLabel extends LabelAnswer {
  item: number;
  labeller: string;
  labelled_at: string;
}

export class MockLabelling {
  private labels: MockLabel[] = [];

  /** `formCases`: title_page progress, read from the mock books' confirmed title-page forms. */
  constructor(private readonly formCases: () => { books: number; cases: number } = () => ({ books: 0, cases: 0 })) {}

  private kindOf(task: LabelTaskName) {
    return TASKS.find((t) => t.name === task)!.item_kind;
  }

  tasks(op: string): LabelTaskOut[] {
    return TASKS.map((t) => {
      if (FORM_TASKS.has(t.name)) {
        const { books, cases } = this.formCases();
        return { ...t, items: books, labelled: cases, skipped: 0, double: 0, mine: 0, left_for_me: 0 };
      }
      const rows = this.labels.filter((l) => l.task === t.name);
      const answered = rows.filter((l) => l.value !== "skip");
      const per = new Map<number, number>();
      for (const l of answered) per.set(l.item, (per.get(l.item) ?? 0) + 1);
      const items = ITEMS.filter((i) => i.kind === t.item_kind).length;
      const mine = rows.filter((l) => l.labeller === op).length;
      return { ...t, items, labelled: answered.length, skipped: rows.length - answered.length,
               double: [...per.values()].filter((c) => c >= 2).length, mine, left_for_me: Math.max(0, items - mine) };
    });
  }

  next(task: LabelTaskName, op: string): LabelItemOut | null {
    const done = new Set(this.labels.filter((l) => l.task === task && l.labeller === op).map((l) => l.item));
    const item = ITEMS.find((i) => i.kind === this.kindOf(task) && !done.has(i.id));
    return item ? this.out(item, op) : null;
  }

  label(id: number, labels: LabelAnswer[], op: string): LabelItemOut {
    const item = ITEMS.find((i) => i.id === id)!;
    for (const a of labels) {
      this.labels = this.labels.filter((l) => !(l.item === id && l.task === a.task && l.labeller === op));
      this.labels.push({ ...a, item: id, labeller: op, labelled_at: new Date().toISOString() });
    }
    return this.out(item, op);
  }

  summary(task: LabelTaskName): LabelSummaryOut {
    const t = TASKS.find((x) => x.name === task)!;
    const rows = this.labels.filter((l) => l.task === task);
    const answered = rows.filter((l) => l.value !== "skip");
    const count = (f: (l: MockLabel) => string) =>
      answered.reduce<Record<string, number>>((acc, l) => ({ ...acc, [f(l)]: (acc[f(l)] ?? 0) + 1 }), {});
    return { task, target: t.target, labels: answered.length, skipped: rows.length - answered.length,
             done: answered.length >= t.target, by_label: count((l) => l.value),
             by_language: count((l) => ITEMS.find((i) => i.id === l.item)?.language ?? "??"),
             labellers: count((l) => l.labeller), by_via: count(() => "spa"), by_labeller_kind: count(() => "human"),
             by_model: {}, double: 0, agreement: null, kappa: null, human_human: NO_PAIRS, agent_human: NO_PAIRS,
             agent_items: 0, spot_checked: 0, spot_check_target: 0, agent_kappa_min: 0.7, agent_trusted: null,
             sampling: "mock data" };
  }

  private out(item: LabelItemOut, op: string): LabelItemOut {
    const mine = Object.fromEntries(this.labels.filter((l) => l.item === item.id && l.labeller === op).map((l) => [l.task, l.value]));
    return { ...item, mine };
  }
}
