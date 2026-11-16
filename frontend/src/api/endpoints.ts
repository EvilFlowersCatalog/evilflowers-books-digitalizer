/**
 * One typed function per row of the spec's REST table (plus the two requested
 * endpoints, marked). Screens call these through TanStack Query only.
 */

import { request } from "./client";
import type {
  BookDetail,
  BookOut,
  BookQuery,
  ItemMetadata,
  LabelAnswer,
  LabelItemOut,
  LabelSummaryOut,
  LabelTaskName,
  LabelTaskOut,
  MarkBand,
  MarkGroupOut,
  NoteCreate,
  NoteOut,
  Page,
  PageDetail,
  PageOut,
  PagePatch,
  ProjectConfigOut,
  ProjectOut,
  RegionCreate,
  RegionOut,
  RegionPatch,
  ReviewCreate,
  ReviewOut,
  RunCreate,
  RunOut,
  TaskAccepted,
  TitleConfirmIn,
  TitlePageOut,
} from "./types";

export const api = {
  projects: () => request<ProjectOut[]>("GET", "/projects"),
  importProject: (project: string, collection?: string) =>
    request<TaskAccepted>("POST", `/projects/${encodeURIComponent(project)}/imports`, {
      body: collection ? { collection } : {},
    }),
  /** Requested contract: read-only project preset for the Settings screen. */
  projectConfig: (project: string) =>
    request<ProjectConfigOut>("GET", `/projects/${encodeURIComponent(project)}/config`),

  books: (query: BookQuery) => request<Page<BookOut>>("GET", "/books", { query: { ...query } }),
  book: (id: number) => request<BookDetail>("GET", `/books/${id}`),
  patchMetadata: (id: number, patch: Partial<ItemMetadata>) =>
    request<ItemMetadata>("PATCH", `/books/${id}/metadata`, { body: patch }),
  /** Requested contract: OPAC lookup returns a partial ItemMetadata (nothing is saved). */
  lookupMetadata: (id: number, provider = "opac") =>
    request<Partial<ItemMetadata>>("GET", `/books/${id}/metadata/lookup`, { query: { provider } }),

  /** Title-page suggestions (#15): nothing reaches the metadata until `confirmTitlePage`. */
  titlePage: (bookId: number) => request<TitlePageOut>("GET", `/books/${bookId}/title-page`),
  confirmTitlePage: (bookId: number, body: TitleConfirmIn) =>
    request<TitlePageOut>("POST", `/books/${bookId}/title-page/confirm`, { body }),

  /** Book-wide scan-mark groups (#14), most leaves first. */
  markGroups: (bookId: number) => request<MarkGroupOut[]>("GET", `/books/${bookId}/mark-groups`),
  patchMarkGroup: (id: number, band: MarkBand) =>
    request<MarkGroupOut>("PATCH", `/mark-groups/${id}`, { body: { band } }),
  /** 201: the new group; 400 when no leaf matches or the split would empty the group. */
  splitMarkGroup: (id: number, leaves: number[]) =>
    request<MarkGroupOut>("POST", `/mark-groups/${id}/split`, { body: { leaves } }),

  createRun: (bookId: number, body: RunCreate) => request<RunOut>("POST", `/books/${bookId}/runs`, { body }),
  run: (id: number) => request<RunOut>("GET", `/runs/${id}`),
  cancelRun: (id: number) => request<RunOut>("POST", `/runs/${id}/cancel`),

  pages: (bookId: number) => request<PageOut[]>("GET", `/books/${bookId}/pages`),
  page: (id: number) => request<PageDetail>("GET", `/pages/${id}`),
  patchPage: (id: number, patch: PagePatch) => request<PageDetail>("PATCH", `/pages/${id}`, { body: patch }),

  createRegion: (pageId: number, body: RegionCreate) =>
    request<RegionOut>("POST", `/pages/${pageId}/regions`, { body }),
  patchRegion: (id: number, patch: RegionPatch) => request<RegionOut>("PATCH", `/regions/${id}`, { body: patch }),
  rejectRegion: (id: number) => request<void>("DELETE", `/regions/${id}`),

  createNote: (body: NoteCreate) => request<NoteOut>("POST", "/notes", { body }),
  review: (bookId: number, body: ReviewCreate) => request<ReviewOut>("POST", `/books/${bookId}/reviews`, { body }),

  /** Labelling for decider calibration (#7). `next` is `null` when nothing is left. */
  labelTasks: () => request<LabelTaskOut[]>("GET", "/labelling/tasks"),
  labelNext: (task: LabelTaskName) => request<LabelItemOut | null>("GET", `/labelling/tasks/${task}/next`),
  labelSummary: (task: LabelTaskName) => request<LabelSummaryOut>("GET", `/labelling/tasks/${task}/summary`),
  /** 202: the catalog fan-out runs in the background; `items` in the task list grows when done. */
  labelSample: (task: LabelTaskName, n: number) =>
    request<{ task: string; n: number; status: string }>("POST", `/labelling/tasks/${task}/samples`, { query: { n } }),
  label: (itemId: number, labels: LabelAnswer[]) =>
    request<LabelItemOut>("POST", `/labelling/items/${itemId}/labels`, { body: { labels } }),
};

/** Plain link (a download, not JSON): the browser saves the JSONL. */
export const labelExportUrl = (task: LabelTaskName) => `/api/v1/labelling/tasks/${task}/export`;

export type Api = typeof api;
