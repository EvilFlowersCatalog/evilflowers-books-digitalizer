/**
 * Realistic fixture data for mock mode: a STU project, ~two dozen books across the
 * whole lifecycle, and one fully detailed flagged book (12 leaves, regions with a
 * disputed stamp and a disputed formula, run history, notes).
 *
 * Geometry is consistent: region boxes are in cleaned-page pixels and the mock
 * images are drawn from the same boxes, so overlays line up in the viewer.
 */

import type {
  BookOut,
  BookStatus,
  ItemMetadata,
  MarkGroupOut,
  NoteOut,
  PageOut,
  PageOverride,
  PageType,
  PublicationOut,
  QualityReport,
  RegionKind,
  RegionOut,
  RegionStatus,
  ReviewOut,
  RunOut,
  TitlePageOut,
} from "../api/types";
import { defaultAction } from "../lib/regions";
import { CROP } from "./labelling";

export const PAGE_W = 2480;
export const PAGE_H = 3508;
export const FRAME_W = 5200;
export const FRAME_H = 3640;

const iso = (minutesAgo: number) => new Date(Date.now() - minutesAgo * 60_000).toISOString();

export interface MockBook extends BookOut {
  metadata: ItemMetadata;
  quality: QualityReport;
}

export interface MockPage extends PageOut {
  book_id: number;
  override: PageOverride;
  ocr_text: string;
  ocr_conf: number;
  skew_angle: number;
  rotate_degree: number;
  dpi: number;
}

export interface MockDb {
  books: MockBook[];
  pages: MockPage[];
  regions: RegionOut[];
  runs: RunOut[];
  notes: NoteOut[];
  reviews: ReviewOut[];
  publications: (PublicationOut & { book_id: number })[];
  /** book id -> title-page suggestions (#15); books without an entry have none */
  titlePages: Record<number, TitlePageOut>;
  markGroups: MockMarkGroup[];
  seq: number;
}

/** A mark group plus what the mock needs to split it: occurrences per leaf. */
export interface MockMarkGroup extends Omit<MarkGroupOut, "on_leaves" | "leaves" | "members"> {
  book_id: number;
  per_leaf: Record<number, number>;
}

function emptyMetadata(identifier: string, title: string): ItemMetadata {
  return {
    identifier,
    title,
    mediatype: "texts",
    creator: [],
    contributor: "Slovenská technická univerzita v Bratislave",
    publisher: "",
    date: "",
    language: ["slo"],
    subject: [],
    description: [],
    isbn: [],
    oclc_id: [],
    lccn: [],
    call_number: "",
    identifier_ark: "",
    external_identifier: [],
    openlibrary_edition: "",
    openlibrary_work: "",
    edition: "",
    volume: "",
    collection: ["fei"],
    rights: "",
    licenseurl: "",
    possible_copyright_status: "",
    copyright_region: "SK",
    page_progression: "lr",
    notes: [],
    sponsor: "",
    sponsordate: "",
    scanner: "Zeutschel OS 12000",
    scanningcenter: "stu-cvt",
    operator: "",
    camera: "",
    scandate: "20261104093000",
    ppi: 300,
    imagecount: null,
    foldoutcount: null,
    ocr: "tesseract 5.5.0",
    ocr_parameters: "-l slk+eng",
    ocr_detected_lang: "",
    extra: {},
  };
}

// ------------------------------------------------------------------ books
const CATALOG: [string, string, string, BookStatus, number | null][] = [
  ["fei", "E2E_FYZIKA_VEKTORY", "Fyzika I — vektory a sily", "review", 0.61],
  ["fei", "MATEMATICKA_ANALYZA_1", "Matematická analýza 1", "running", null],
  ["fei", "ELEKTROTECHNIKA_ZAKLADY", "Základy elektrotechniky", "review", 0.48],
  ["fei", "TEORIA_OBVODOV", "Teória elektrických obvodov", "review", 0.74],
  ["fei", "SIGNALY_A_SUSTAVY", "Signály a sústavy", "published", 0.97],
  ["fei", "NUMERICKE_METODY", "Numerické metódy", "published", 0.95],
  ["fei", "ALGEBRA_LINEARNA", "Lineárna algebra pre inžinierov", "approved", 0.91],
  ["fei", "MIKROPROCESORY", "Mikroprocesory a mikropočítače", "queued", null],
  ["fei", "PROGRAMOVANIE_C", "Programovanie v jazyku C", "queued", null],
  ["fei", "ELEKTRONIKA_1", "Elektronika I", "discovered", null],
  ["fei", "ELEKTRONIKA_2", "Elektronika II", "discovered", null],
  ["fei", "MERANIA", "Elektrické merania", "discovered", null],
  ["fei", "VYSOKE_NAPATIE", "Technika vysokých napätí", "error", null],
  ["fchpt", "ORGANICKA_CHEMIA", "Organická chémia", "published", 0.93],
  ["fchpt", "ANALYTICKA_CHEMIA", "Analytická chémia", "review", 0.57],
  ["fchpt", "FYZIKALNA_CHEMIA", "Fyzikálna chémia I", "approved", 0.88],
  ["fchpt", "BIOCHEMIA", "Biochémia — základy", "discovered", null],
  ["fchpt", "POLYMERY", "Chémia polymérov", "discovered", null],
  ["fchpt", "CHEM_INZINIERSTVO", "Chemické inžinierstvo", "published", 0.96],
  ["svf", "STATIKA", "Statika stavebných konštrukcií", "published", 0.94],
  ["svf", "BETON", "Betónové konštrukcie", "review", 0.69],
  ["svf", "GEODEZIA", "Geodézia", "discovered", null],
  ["svf", "MECHANIKA_ZEMIN", "Mechanika zemín", "running", null],
  ["svf", "HYDRAULIKA", "Hydraulika", "approved", 0.9],
];

const FLAGS: Record<number, string[]> = {
  1: ["low OCR confidence (76 < 80)", "2 disputed region(s)"],
  3: ["4 disputed region(s)", "3/7 formulas left as images", "3 VLM answers rejected as hallucinations"],
  4: ["1 disputed region(s)"],
  15: ["low OCR confidence (71 < 80)", "12 non-Slovak letters in prose"],
  21: ["2 disputed region(s)", "0 unreadable frame(s), 1 missing page(s)"],
};

function makeBook(i: number): MockBook {
  const [collection, raw, title, status, quality] = CATALOG[i]!;
  const id = i + 1;
  const item = `stu-${collection}-${raw.toLowerCase().replace(/_/g, "-")}`;
  const flags = FLAGS[id] ?? [];
  const nPages = id === 1 ? 12 : 120 + ((id * 37) % 240);
  const meta = emptyMetadata(item, title);
  meta.collection = [collection];
  meta.imagecount = nPages;
  if (id === 1) {
    Object.assign(meta, {
      creator: ["Hajko, Vladimír", "Krempaský, Július, 1931-2012"],
      publisher: "Bratislava : Alfa",
      date: "1983",
      subject: ["fyzika", "mechanika", "vektory", "učebnice vysokých škôl"],
      description: ["Vysokoškolská učebnica pre elektrotechnické fakulty. Kapitola 1: vektorový počet."],
      isbn: ["80-05-00123-4"],
      call_number: "53(075.8) HAJ",
      edition: "2. vyd.",
      volume: "1",
      possible_copyright_status: "IN_COPYRIGHT",
    });
  }
  return {
    id,
    slug: `${collection}/${raw}`,
    item_id: item,
    project: "stu-local",
    collection,
    title,
    status,
    quality_score: quality,
    flagged: status === "review" || flags.length > 0,
    flags,
    n_pages: status === "discovered" ? null : nPages,
    n_frames: Math.ceil(nPages / 2) + 1,
    updated_at: iso(i * 17 + 3),
    step: status === "running" ? "clean" : null,
    progress: status === "running" ? 0.35 : undefined,
    metadata: meta,
    quality:
      quality === null
        ? {}
        : {
            passed: flags.length === 0,
            flags,
            score: quality,
            signals: {
              pages: nPages,
              ocr_mean_conf: id === 1 ? 76.4 : 88,
              disputed_regions: id === 1 ? 2 : 0,
              formula_fallbacks: id === 3 ? 3 : 0,
              vlm_rejected: id === 3 ? 3 : 0,
              foreign_letters: id === 15 ? 12 : 0,
              unreadable_frames: 0,
              missing_pages: id === 21 ? 1 : 0,
            },
          },
  };
}

// ------------------------------------------------------------------ leaves of the detailed book
const LEAF_TYPES: [PageType, string | null][] = [
  ["Color Card", null],
  ["Cover", null],
  ["Title", null],
  ["Copyright", null],
  ["Contents", "5"],
  ["Chapter", "7"],
  ["Normal", "8"],
  ["Normal", "9"],
  ["Normal", "10"],
  ["Normal", "11"],
  ["Index", "212"],
  ["White Card", null],
];

type Box = [number, number, number, number];
type Spec = [RegionKind, Box, RegionStatus?, string?, (string | null)?, RegionKind[]?];

const M = 230; // page margin, px
const BODY_W = PAGE_W - 2 * M;

function textColumn(y0: number, y1: number): Spec[] {
  const out: Spec[] = [];
  let y = y0;
  while (y + 300 < y1) {
    const h = 260 + ((y * 7) % 220);
    out.push(["text", [M, y, M + BODY_W, Math.min(y + h, y1)]]);
    y += h + 70;
  }
  return out;
}

/** Region layout per leaf: the story the operator sees in the studio. */
function leafSpecs(leaf: number): Spec[] {
  const header: Spec = ["page_header", [M, 150, M + BODY_W, 215]];
  const footer: Spec = ["page_footer", [PAGE_W / 2 - 80, PAGE_H - 210, PAGE_W / 2 + 80, PAGE_H - 150]];
  switch (leaf) {
    case 0:
      return [["picture", [300, 600, 2180, 2900], "proposed", "", null]];
    case 1:
      return [
        ["title", [M + 200, 900, PAGE_W - M - 200, 1250]],
        ["text", [M + 400, 1400, PAGE_W - M - 400, 1560]],
        ["picture", [M + 500, 1800, PAGE_W - M - 500, 2700]],
      ];
    case 2:
      return [
        ["text", [M + 300, 500, PAGE_W - M - 300, 620]],
        ["title", [M + 150, 1000, PAGE_W - M - 150, 1420]],
        ["text", [M + 400, 1550, PAGE_W - M - 400, 1700]],
        ["stamp", [1500, 2350, 2050, 2900], "disputed", "stamp would be removed: confirm once", null, []],
        ["text", [M + 500, 3000, PAGE_W - M - 500, 3150]],
      ];
    case 3:
      return [["text", [M, 2500, M + BODY_W, 3100]], ["handwriting", [M + 1200, 600, M + 1900, 820], "disputed", "handwriting would be removed: confirm once"]];
    case 4:
      return [header, ["section_header", [M, 400, M + 900, 520]], ...textColumn(620, 3100).map((s) => ["list_item", s[1]] as Spec), footer];
    case 5:
      return [["section_header", [M, 700, M + 1600, 900]], ...textColumn(1050, 3150), footer];
    case 6:
      return [
        header,
        ...textColumn(330, 1350),
        [
          "formula",
          [M + 400, 1420, M + BODY_W - 400, 1720],
          "disputed",
          "detectors disagree: formula vs picture",
          "\\vec{F} = m\\,\\vec{a} = m\\,\\frac{\\mathrm{d}\\vec{v}}{\\mathrm{d}t}",
          ["picture"],
        ],
        ...textColumn(1800, 3150),
        footer,
      ];
    case 7:
      return [
        header,
        ...textColumn(330, 1150),
        ["picture", [M + 300, 1230, M + BODY_W - 300, 2250]],
        ["caption", [M + 300, 2290, M + BODY_W - 300, 2400]],
        ...textColumn(2480, 3150),
        footer,
      ];
    case 8:
      return [
        header,
        ...textColumn(330, 1050),
        ["formula", [M + 500, 1120, M + BODY_W - 500, 1360], "confirmed", "", "\\left|\\vec{a}\\times\\vec{b}\\right| = a\\,b\\,\\sin\\alpha"],
        ...textColumn(1440, 2500),
        ["footnote", [M, 2900, M + BODY_W, 3080]],
        footer,
      ];
    case 9:
      return [
        header,
        ...textColumn(330, 1100),
        ["table", [M + 100, 1180, M + BODY_W - 100, 2300]],
        ["caption", [M + 100, 2340, M + BODY_W - 100, 2440]],
        ...textColumn(2520, 3150),
        footer,
      ];
    case 10:
      return [header, ...textColumn(330, 3150).map((s) => ["list_item", s[1]] as Spec), footer];
    default:
      return [];
  }
}

const OCR_SAMPLE =
  "Vektor je veličina, ktorá je určená veľkosťou a smerom. V tejto kapitole zavedieme " +
  "základné operácie s vektormi: súčet, rozdiel, skalárny a vektorový súčin.";

function makePages(book: MockBook, startId: number, regionStart: number) {
  const pages: MockPage[] = [];
  const regions: RegionOut[] = [];
  let rid = regionStart;
  const count = book.id === 1 ? 12 : Math.min(book.n_pages ?? 0, 16);
  for (let leaf = 0; leaf < count; leaf++) {
    const [type, number] = book.id === 1 ? LEAF_TYPES[leaf]! : (["Normal", String(leaf + 1)] as [PageType, string]);
    const id = startId + leaf;
    const specs = book.id === 1 ? leafSpecs(leaf) : leafSpecs(6 + (leaf % 4)).map((s) => [s[0], s[1]] as Spec);
    const disputed = specs.filter((s) => s[2] === "disputed").length;
    pages.push({
      id,
      book_id: book.id,
      index: leaf,
      page_type: type,
      hand_side: leaf % 2 === 0 ? "RIGHT" : "LEFT",
      page_number: number,
      add_to_access: !["Color Card", "White Card", "Delete"].includes(type),
      status: disputed ? "disputed" : book.status === "discovered" ? "pending" : "analyzed",
      version: 1,
      width: PAGE_W,
      height: PAGE_H,
      disputed,
      override: leaf === 6 ? { deskew_deg: -0.6, color_mode: "black_and_white" } : {},
      ocr_text: specs.some((s) => s[0] === "text") ? OCR_SAMPLE : "",
      ocr_conf: leaf === 6 ? 64.2 : 81.5,
      skew_angle: ((leaf * 13) % 9) / 10 - 0.4,
      rotate_degree: 0,
      dpi: 300,
    });
    for (const [kind, bbox, status = "proposed", reason = "", text = undefined, alternatives = null] of specs) {
      regions.push({
        id: rid++,
        page_id: id,
        kind,
        bbox,
        source: kind === "formula" ? "vlm" : "heuristic",
        confidence: status === "disputed" ? 0.48 : 0.86,
        status,
        action: defaultAction(kind),
        text: text ?? (kind === "text" || kind === "list_item" ? OCR_SAMPLE : null),
        alternatives,
        reason: reason || null,
        author: status === "confirmed" ? "jana" : null,
        updated_at: iso(90),
      });
    }
  }
  return { pages, regions };
}

function makeRuns(): RunOut[] {
  const base = (id: number, book_id: number, extra: Partial<RunOut>): RunOut => ({
    id,
    book_id,
    kind: "full",
    status: "succeeded",
    interactive: false,
    pages: null,
    stage: "gate",
    done: 12,
    total: 12,
    error: null,
    log: [],
    created_by: null,
    created_at: iso(300),
    started_at: iso(298),
    finished_at: iso(260),
    ...extra,
  });
  return [
    base(101, 1, {
      log: [
        { ts: iso(298), level: "info", message: "discovered 7 frames → 12 leaves" },
        { ts: iso(290), level: "info", message: "scantailor: split 6 spreads, deskew mean 0.4°" },
        { ts: iso(281), level: "info", message: "ocr: tesseract slk+eng, mean conf 76.4" },
        { ts: iso(274), level: "warning", message: "leaf 6: formula/picture disagreement (iou 0.82)" },
        { ts: iso(271), level: "warning", message: "leaf 2: stamp proposed (needs consent)" },
        { ts: iso(262), level: "info", message: "render: pdf 4.1 MB, pdfa 38.2 MB, epub 0.9 MB" },
        { ts: iso(260), level: "warning", message: "gate: 2 flags → review" },
      ],
    }),
    base(102, 1, {
      kind: "rerun",
      interactive: true,
      pages: [6],
      stage: "analyze",
      done: 1,
      total: 1,
      created_by: "jana",
      created_at: iso(70),
      started_at: iso(70),
      finished_at: iso(69),
      log: [{ ts: iso(69), level: "info", message: "leaf 6 re-analyzed with override deskew -0.6°" }],
    }),
    base(103, 2, {
      status: "running",
      stage: "clean",
      done: 54,
      total: 152,
      finished_at: null,
      started_at: iso(12),
      created_at: iso(13),
      log: [{ ts: iso(12), level: "info", message: "scantailor: 76 frames queued" }],
    }),
    base(104, 23, {
      status: "running",
      stage: "ocr",
      done: 18,
      total: 210,
      finished_at: null,
      started_at: iso(30),
      created_at: iso(31),
    }),
    base(105, 13, {
      status: "failed",
      stage: "clean",
      done: 3,
      total: 190,
      error: "ScanTailorError: frame 0004.tif is truncated (read 1.2 of 24 MB)",
      finished_at: iso(500),
    }),
  ];
}

// ------------------------------------------------------------------ title page (#15)
const DECIDER = "qwen3-8b-decider@2026-11";

function makeTitlePages(): Record<number, TitlePageOut> {
  return {
    // the detailed book: open suggestions, one field the decider found nothing for
    1: {
      suggestions: [
        { field: "title", metadata_field: "title", value: "Fyzika I", p: 0.91, line: "FYZIKA I", leaf: 2 },
        { field: "authors", metadata_field: "creator", value: ["Vladimír Hajko", "Július Krempaský"], p: 0.84, line: "Vladimír Hajko · Július Krempaský", leaf: 2 },
        { field: "publisher", metadata_field: "publisher", value: "Alfa", p: 0.58, line: "ALFA, BRATISLAVA", leaf: 2 },
        { field: "year", metadata_field: "date", value: null, p: null, line: null, leaf: null },
      ],
      decider_model: DECIDER,
      suggested_at: iso(260),
      confirmed: null,
    },
    // a published book whose form was confirmed already
    5: {
      suggestions: [
        { field: "title", metadata_field: "title", value: "Signály a sústavy", p: 0.95, line: "SIGNÁLY A SÚSTAVY", leaf: 2 },
        { field: "authors", metadata_field: "creator", value: ["J. Novák"], p: 0.66, line: "Ing. J. Novák, CSc.", leaf: 2 },
        { field: "year", metadata_field: "date", value: "1991", p: 0.49, line: "© 1991", leaf: 3 },
      ],
      decider_model: DECIDER,
      suggested_at: iso(1000),
      confirmed: {
        fields: {
          title: { suggested: "Signály a sústavy", line: "SIGNÁLY A SÚSTAVY", leaf: 2, p: 0.95, final: "Signály a sústavy", outcome: "accepted" },
          authors: { suggested: ["J. Novák"], line: "Ing. J. Novák, CSc.", leaf: 2, p: 0.66, final: ["Novák, Ján"], outcome: "changed" },
          year: { suggested: "1991", line: "© 1991", leaf: 3, p: 0.49, final: null, outcome: "none" },
        },
        labeller: "jana",
        at: iso(950),
        decider_model: DECIDER,
      },
    },
  };
}

// ------------------------------------------------------------------ scan-mark groups (#14)
function makeMarkGroups(): MockMarkGroup[] {
  const group = (id: number, book_id: number, category: string, extra: Partial<MockMarkGroup>, per_leaf: Record<number, number>): MockMarkGroup => ({
    id,
    book_id,
    category,
    band: "review",
    decision: null,
    p: null,
    source: "signals",
    decider_model: null,
    crop_leaf: Number(Object.keys(per_leaf)[0]),
    crop_bbox: [1500, 2350, 2050, 2900],
    image_url: CROP(`${category} #${id}`),
    split_from: null,
    per_leaf,
    ...extra,
  });
  return [
    group(701, 1, "stamp", { source: "decider", decision: "stamp", p: 0.62, decider_model: DECIDER }, { 2: 1, 3: 1, 5: 1, 7: 2, 9: 1 }),
    group(702, 1, "sticker", { band: "hide", source: "rule", decision: "sticker" }, { 1: 1, 10: 1 }),
    group(703, 1, "bleed_through", { band: "keep", source: "decider", decision: "content", p: 0.81, decider_model: DECIDER }, { 6: 2, 7: 1 }),
    group(704, 1, "edge", {}, { 11: 1 }),
    group(705, 3, "stamp", { band: "hide", source: "operator", decision: "stamp", decider_model: "operator:jana" }, { 0: 1, 4: 1, 8: 1 }),
  ];
}

export function createDb(): MockDb {
  const books = CATALOG.map((_, i) => makeBook(i));
  const pages: MockPage[] = [];
  const regions: RegionOut[] = [];
  let pageId = 1001;
  let regionId = 5001;
  for (const book of books.filter((b) => b.status !== "discovered")) {
    const made = makePages(book, pageId, regionId);
    pages.push(...made.pages);
    regions.push(...made.regions);
    pageId += 100;
    regionId += made.regions.length + 10;
  }
  const notes: NoteOut[] = [
    { id: 1, book_id: 1, page_id: 1002, region_id: null, author: "jana", text: "Library seal on the title page, not content.", created_at: iso(80) },
    { id: 2, book_id: 1, page_id: null, region_id: null, author: "peter", text: "Cover photo is a reprint; check the 1983 edition date.", created_at: iso(200) },
  ];
  const reviews: ReviewOut[] = [
    { id: 1, book_id: 5, decision: "approved", author: "jana", note: null, created_at: iso(900) },
  ];
  const publications: MockDb["publications"] = [
    { id: 1, book_id: 5, target: "evilflowers", entry_id: "8c1f2e9a-5b7d-4c1e-9f3a-2d4b6e8a0c11", version: 2, url: "https://dev.evilflowers.elvira.stuba.sk/entries/8c1f2e9a", author: "jana", published_at: iso(880) },
  ];
  return { books, pages, regions, runs: makeRuns(), notes, reviews, publications, titlePages: makeTitlePages(), markGroups: makeMarkGroups(), seq: 9000 };
}

export const PROJECT = {
  slug: "stu-local",
  name: "STU — local test scans",
  collections: {
    fei: "Fakulta elektrotechniky a informatiky",
    fchpt: "Fakulta chemickej a potravinárskej technológie",
    svf: "Stavebná fakulta",
  } as Record<string, string>,
  auto_publish: "gate" as const,
};
