/**
 * Labelling mode (#7): mirrors `studio/application/labelling.py`. An item never carries the
 * model's answer (band, p, decision, relation): labellers must not be anchored.
 */

export type LabelItemKind = "figure" | "table" | "section_pair" | "entry";
export type LabelTaskName = "figure_kind" | "is_data_table" | "table_quality" | "section_relation" | "subject";

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
  double: number;
  agreement: number | null;
  kappa: number | null;
  sampling: string;
}
