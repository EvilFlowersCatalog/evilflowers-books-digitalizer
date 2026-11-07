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
};

export type Api = typeof api;
