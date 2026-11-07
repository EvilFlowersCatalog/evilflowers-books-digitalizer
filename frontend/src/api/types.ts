/**
 * Wire types of the studio REST API (`/api/v1`), written by hand from
 * docs/design/studio-spec.md (REST table), studio/domain.py, studio/metadata.py,
 * studio/events.py and db/models.py.
 *
 * Why by hand: the pydantic DTOs are written in parallel; these types are the SPA's
 * half of the contract and the report lists them so the integrator can align both.
 * Fields the spec does not pin down are optional, so a slightly richer or poorer
 * payload degrades the UI instead of crashing it.
 */

// ------------------------------------------------------------------ vocabulary (domain.py)
export const BOOK_STATUSES = [
  "discovered",
  "queued",
  "running",
  "review",
  "approved",
  "published",
  "error",
  "ok",
] as const;
export type BookStatus = (typeof BOOK_STATUSES)[number];

/** Dashboard segments, in pipeline order (legacy `ok` is folded into `approved`). */
export const PROGRESS_STATUSES = [
  "discovered",
  "queued",
  "running",
  "review",
  "approved",
  "published",
  "error",
] as const satisfies readonly BookStatus[];

export type PageStatus = "pending" | "cleaned" | "analyzed" | "disputed" | "done";

export const REGION_KINDS = [
  "text",
  "title",
  "section_header",
  "list_item",
  "caption",
  "footnote",
  "formula",
  "table",
  "picture",
  "page_header",
  "page_footer",
  "stamp",
  "sticker",
  "handwriting",
  "other",
] as const;
export type RegionKind = (typeof REGION_KINDS)[number];

export type RegionSource = "heuristic" | "yolo" | "vlm" | "operator";
export type RegionStatus = "proposed" | "disputed" | "confirmed" | "rejected";
export const REGION_ACTIONS = ["keep", "remove", "as_image", "reocr"] as const;
export type RegionAction = (typeof REGION_ACTIONS)[number];

export type RunKind = "full" | "rerun" | "render" | "publish";
export type RunStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";
export type ReviewDecision = "approved" | "needs_work" | "rejected";

// ------------------------------------------------------------------ geometry
/** Region box in cleaned-page pixels: `[x0, y0, x1, y1]` (domain `BBox.as_list()`). */
export type BBoxList = [number, number, number, number];
/** IA scandata crop: `[x, y, w, h]` in the original frame after rotation (`pages.crop_box`). */
export type XYWH = [number, number, number, number];

// ------------------------------------------------------------------ IA leaf semantics (metadata.py)
export const PAGE_TYPES = [
  "Normal",
  "Cover",
  "Title",
  "Copyright",
  "Contents",
  "Tissue",
  "Color Card",
  "Chapter",
  "White Card",
  "Foldout",
  "Abstract",
  "Appendix",
  "Contributions",
  "Delete",
  "Foreword",
  "Glossary",
  "Index",
  "Inscriptions",
  "Introduction",
  "List of Figures",
  "List of Tables",
  "Notes",
  "Preface",
  "Quotations",
  "Reference",
  "Subchapter",
  "SubSubchapter",
] as const;
export type PageType = (typeof PAGE_TYPES)[number];

/** Page types whose `add_to_access` defaults to false (metadata.NOT_IN_ACCESS). */
export const NOT_IN_ACCESS: ReadonlySet<PageType> = new Set(["Color Card", "White Card", "Delete"]);

export type HandSide = "LEFT" | "RIGHT";

/** `ItemMetadata.as_dict()`: python field names, IA semantics, repeatables as lists. */
export interface ItemMetadata {
  identifier: string;
  title: string;
  mediatype: string;
  creator: string[];
  contributor: string;
  publisher: string;
  date: string;
  language: string[];
  subject: string[];
  description: string[];
  isbn: string[];
  oclc_id: string[];
  lccn: string[];
  call_number: string;
  identifier_ark: string;
  external_identifier: string[];
  openlibrary_edition: string;
  openlibrary_work: string;
  edition: string;
  volume: string;
  collection: string[];
  rights: string;
  licenseurl: string;
  possible_copyright_status: string;
  copyright_region: string;
  page_progression: string;
  notes: string[];
  sponsor: string;
  sponsordate: string;
  scanner: string;
  scanningcenter: string;
  operator: string;
  camera: string;
  scandate: string;
  ppi: number | null;
  imagecount: number | null;
  foldoutcount: number | null;
  ocr: string;
  ocr_parameters: string;
  ocr_detected_lang: string;
  extra: Record<string, string[]>;
}

// ------------------------------------------------------------------ projects
export interface ProjectOut {
  slug: string;
  name: string;
  /** collection key -> human name */
  collections: Record<string, string>;
  /** book status -> count (whole project) */
  counts: Partial<Record<BookStatus, number>>;
  /** collection key -> (status -> count); optional, see contract request */
  collection_counts?: Record<string, Partial<Record<BookStatus, number>>>;
  auto_publish?: "never" | "gate" | "review";
}

/** Read-only project preset (Settings screen); `GET /projects/{p}/config` (requested). */
export interface ProjectConfigOut {
  slug: string;
  name: string;
  book_pattern?: string;
  frames_glob?: string;
  item_id?: string;
  language?: string;
  ocr_language?: string;
  layout_detectors?: string[];
  formula_recognizer?: string;
  outputs?: string[];
  auto_publish?: string;
  quality?: Record<string, number>;
  publish?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
}

// ------------------------------------------------------------------ quality
export interface QualitySignals {
  pages: number;
  ocr_mean_conf: number;
  disputed_regions: number;
  formula_fallbacks: number;
  vlm_rejected: number;
  foreign_letters: number;
  unreadable_frames: number;
  missing_pages: number;
}

/** `QualityReport.as_dict()`; empty object before the first gate. */
export interface QualityReport {
  passed?: boolean;
  flags?: string[];
  score?: number;
  signals?: QualitySignals;
}

// ------------------------------------------------------------------ books
export interface BookOut {
  id: number;
  slug: string;
  item_id: string | null;
  project: string | null;
  collection?: string | null;
  title: string | null;
  status: BookStatus;
  quality_score: number | null;
  /** quality gate failed (status review or flags present) */
  flagged?: boolean;
  flags?: string[];
  n_pages: number | null;
  n_frames?: number | null;
  updated_at: string;
  /** stage of the active run, if any */
  step?: string | null;
  progress?: number;
  /** open (disputed) regions right now, not as counted at the last gate */
  disputed?: number;
}

export interface Page<T> {
  items: T[];
  total: number;
}

export type BookSort = "quality_score" | "-quality_score" | "updated_at" | "-updated_at" | "title" | "-title";

export interface BookQuery {
  project: string;
  collection?: string;
  status?: BookStatus;
  q?: string;
  flagged?: boolean;
  limit?: number;
  offset?: number;
  sort?: BookSort;
}

export interface ArtifactOut {
  kind: string;
  path: string;
  exists: boolean;
  size?: number | null;
}

export interface LogLine {
  ts?: string;
  level: string;
  message: string;
}

export interface RunOut {
  id: number;
  book_id: number;
  kind: RunKind;
  status: RunStatus;
  interactive: boolean;
  pages: number[] | null;
  stage: string | null;
  done: number;
  total: number;
  error: string | null;
  log: LogLine[];
  created_by: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  /** convenience for the dashboard (book title / slug) */
  book_title?: string | null;
  book_slug?: string | null;
}

export interface PublicationOut {
  id: number;
  target: string;
  entry_id: string;
  version: number;
  url: string | null;
  author: string | null;
  published_at: string;
}

export interface ReviewOut {
  id: number;
  book_id: number;
  decision: ReviewDecision;
  author: string | null;
  note: string | null;
  created_at: string;
}

export interface NoteOut {
  id: number;
  book_id: number;
  page_id: number | null;
  region_id: number | null;
  author: string | null;
  text: string;
  created_at: string;
}

export interface BookDetail {
  book: BookOut;
  item_metadata: ItemMetadata;
  quality: QualityReport;
  runs: RunOut[];
  publications: PublicationOut[];
  reviews?: ReviewOut[];
  notes?: NoteOut[];
  artifacts: ArtifactOut[];
  /** every path worth showing: source_dir, work_dir, output_dir, … */
  paths: Record<string, string | null>;
}

// ------------------------------------------------------------------ pages
export interface PageOut {
  id: number;
  index: number;
  page_type: PageType;
  hand_side: HandSide | null;
  page_number: string | null;
  add_to_access: boolean;
  status: PageStatus;
  version: number;
  width: number | null;
  height: number | null;
  /** thumbnail URL (API-relative); the SPA builds one from the id when absent */
  thumb?: string | null;
  disputed?: number;
}

/** `PageOverride.as_dict()` (crop is `[x0, y0, x1, y1]` in the original frame). */
export interface PageOverride {
  crop?: BBoxList;
  deskew_deg?: number;
  split_x?: number;
  despeckle?: "off" | "cautious" | "normal" | "aggressive";
  color_mode?: "mixed" | "color_grayscale" | "black_and_white";
  ocr_language?: string;
  extra?: Record<string, unknown>;
}

export interface RegionOut {
  id: number;
  page_id: number;
  kind: RegionKind;
  bbox: BBoxList;
  source: RegionSource;
  confidence: number;
  status: RegionStatus;
  action: RegionAction;
  text: string | null;
  alternatives: RegionKind[] | null;
  reason: string | null;
  author: string | null;
  updated_at: string;
}

export interface FrameOut {
  path: string | null;
  width?: number | null;
  height?: number | null;
  /** half of a spread: 0 left, 1 right */
  side?: number | null;
  n_sides?: number | null;
  x0?: number | null;
  x1?: number | null;
  crop_box?: XYWH | null;
}

export interface PageDetail {
  page: PageOut & {
    book_id: number;
    rotate_degree?: number;
    skew_angle?: number | null;
    dpi?: number | null;
    ocr_conf?: number | null;
  };
  regions: RegionOut[];
  notes: NoteOut[];
  override: PageOverride;
  ocr_text: string | null;
  frame: FrameOut;
  /** neighbours for ←/→ (optional; the SPA falls back to the pages list) */
  prev_id?: number | null;
  next_id?: number | null;
}

// ------------------------------------------------------------------ request bodies
export interface RunCreate {
  kind: RunKind;
  pages?: number[];
  interactive?: boolean;
}

export interface PagePatch {
  page_type?: PageType;
  page_number?: string | null;
  include_in_access?: boolean;
  override?: PageOverride;
}

export interface RegionCreate {
  kind: RegionKind;
  bbox: BBoxList;
  action?: RegionAction;
  text?: string;
}

export interface RegionPatch {
  kind?: RegionKind;
  bbox?: BBoxList;
  status?: RegionStatus;
  action?: RegionAction;
  text?: string | null;
}

export interface NoteCreate {
  book_id?: number;
  page_id?: number;
  region_id?: number;
  text: string;
}

export interface ReviewCreate {
  decision: ReviewDecision;
  note?: string;
}

export interface TaskAccepted {
  task_id: string;
}

export type ImageLayer = "original" | "spread" | "clean" | "pdf";

// ------------------------------------------------------------------ events (events.py)
export const EVENT_TYPES = [
  "import.started",
  "import.finished",
  "book.discovered",
  "book.queued",
  "run.started",
  "run.progress",
  "run.log",
  "run.finished",
  "page.updated",
  "regions.updated",
  "book.status",
  "book.review_needed",
  "book.ready",
  "book.published",
] as const;
export type EventType = (typeof EVENT_TYPES)[number];

export interface StudioEvent {
  type: EventType;
  project: string;
  book_id: number | null;
  data: Record<string, unknown>;
  ts: string;
  /** SSE `id:` (monotonic), when known */
  id?: string;
}
