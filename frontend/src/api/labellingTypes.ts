/**
 * Labelling mode (#7): mirrors `studio/application/labelling.py`. An item never carries the
 * model's answer (band, p, decision, relation): labellers must not be anchored.
 */

export type LabelItemKind = "figure" | "table" | "section_pair" | "entry" | "mark_group" | "book";
export type LabelTaskName =
  | "figure_kind"
  | "is_data_table"
  | "table_quality"
  | "section_relation"
  | "subject"
  | "scan_mark"
  | "title_page";

/**
 * Tasks of item kind `book` (`title_page`) are not answered on the labelling screen: their
 * cases come from confirmed title-page forms, and `next` / `samples` answer 400 for them.
 */
export const labelledHere = (task: Pick<LabelTaskOut, "item_kind">): boolean => task.item_kind !== "book";

export const SKIP = "skip";

export interface LabelChoice {
  key: string;
  hint: string | null;
}

export interface LabelTaskOut {
  name: LabelTaskName;
  title: string;
  item_kind: LabelItemKind;
  labels: LabelChoice[];
  companion_of: LabelTaskName | null;
  target: number;
  items: number;
  labelled: number;
  skipped: number;
  double: number;
  mine: number;
  left_for_me: number;
}

export interface SectionSide {
  key: string;
  entry_id: string;
  entry_title: string | null;
  title: string | null;
  first_page: number | null;
  text: string;
}

export interface CategoryOption {
  id: string;
  term: string;
  label: string;
}

/** What the screen shows; which keys exist depends on the item kind. */
export interface LabelPayload {
  entry_title?: string | null;
  page?: number;
  region?: string;
  printed_caption?: string | null;
  page_text?: string;
  markdown?: string;
  a?: SectionSide;
  b?: SectionSide;
  title?: string | null;
  summary?: string;
  table_of_contents?: string[];
  options?: CategoryOption[];
  /** mark_group (`scan_mark`): the leaf of the crop, and how far the mark spreads */
  leaf?: number | null;
  leaves?: number;
  occurrences?: number;
}

export interface LabelItemOut {
  id: number;
  kind: LabelItemKind;
  key: string;
  language: string | null;
  ref: Record<string, string>;
  payload: LabelPayload;
  image_url: string | null;
  mine: Partial<Record<LabelTaskName, string>>;
}

export interface LabelAnswer {
  task: LabelTaskName;
  value: string;
}

export interface LabelSummaryOut {
  task: LabelTaskName;
  target: number;
  labels: number;
  skipped: number;
  done: boolean;
  by_label: Record<string, number>;
  by_language: Record<string, number>;
  labellers: Record<string, number>;
  /** interface the labels came through: spa | cli | mcp */
  by_via: Record<string, number>;
  /** who produced the labels (#16): human | agent, and the agents' models */
  by_labeller_kind: Record<string, number>;
  by_model: Record<string, number>;
  /** human↔human: the first two people of an item (same as `human_human`) */
  double: number;
  agreement: number | null;
  kappa: number | null;
  human_human: PairStats;
  /** the spot check: an item's first human answer against its first agent answer */
  agent_human: PairStats;
  agent_items: number;
  spot_checked: number;
  spot_check_target: number;
  agent_kappa_min: number;
  agent_trusted: boolean | null;
  sampling: string;
}

export interface Agreement {
  pairs: number;
  agreement: number | null;
  kappa: number | null;
}

export interface PairStats extends Agreement {
  by_language: Record<string, Agreement>;
  /** first rater's answer → second rater's answer → count */
  confusion: Record<string, Record<string, number>>;
}
