/**
 * React hooks over the endpoints. Queries never poll (SSE invalidates them, see
 * live.ts); mutations invalidate what they changed so the UI is right even before
 * the matching event arrives.
 */

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./endpoints";
import { qk } from "./queryKeys";
import type {
  BookQuery,
  ItemMetadata,
  NoteCreate,
  PageDetail,
  PagePatch,
  RegionCreate,
  RegionOut,
  RegionPatch,
  ReviewCreate,
  RunCreate,
} from "./types";

export const useProjects = () => useQuery({ queryKey: qk.projects(), queryFn: api.projects });

export const useProjectConfig = (slug: string | undefined) =>
  useQuery({
    queryKey: qk.projectConfig(slug ?? ""),
    queryFn: () => api.projectConfig(slug ?? ""),
    enabled: Boolean(slug),
    retry: false,
  });

export const useBooks = (query: BookQuery | null) =>
  useQuery({
    queryKey: qk.books(query ?? { project: "" }),
    queryFn: () => api.books(query ?? { project: "" }),
    enabled: Boolean(query?.project),
    placeholderData: keepPreviousData,
  });

/** Database ids start at 1: 0 is "not known yet" (e.g. before the page detail loads). */
export const useBook = (id: number) =>
  useQuery({ queryKey: qk.book(id), queryFn: () => api.book(id), enabled: id > 0 });
export const usePages = (bookId: number | undefined) =>
  useQuery({
    queryKey: qk.pages(bookId ?? 0),
    queryFn: () => api.pages(bookId ?? 0),
    enabled: bookId !== undefined,
  });
export const usePage = (id: number) => useQuery({ queryKey: qk.page(id), queryFn: () => api.page(id) });

export function useImport() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ project, collection }: { project: string; collection?: string }) =>
      api.importProject(project, collection),
    onSuccess: () => client.invalidateQueries({ queryKey: qk.projects() }),
  });
}

export function useCreateRun() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ bookId, body }: { bookId: number; body: RunCreate }) => api.createRun(bookId, body),
    onSuccess: (run) => {
      client.setQueryData(qk.run(run.id), run);
      void client.invalidateQueries({ queryKey: qk.book(run.book_id) });
      void client.invalidateQueries({ queryKey: qk.booksAll() });
    },
  });
}

export function useCancelRun() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (runId: number) => api.cancelRun(runId),
    onSuccess: (run) => {
      client.setQueryData(qk.run(run.id), run);
      void client.invalidateQueries({ queryKey: qk.book(run.book_id) });
    },
  });
}

export function usePatchMetadata(bookId: number) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (patch: Partial<ItemMetadata>) => api.patchMetadata(bookId, patch),
    onSuccess: () => client.invalidateQueries({ queryKey: qk.book(bookId) }),
  });
}

export function useReview(bookId: number) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: ReviewCreate) => api.review(bookId, body),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: qk.book(bookId) });
      void client.invalidateQueries({ queryKey: qk.booksAll() });
      void client.invalidateQueries({ queryKey: qk.projects() });
    },
  });
}

/** Approve several books from a list (bulk action / review queue). */
export function useBulkReview() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({ ids, body }: { ids: number[]; body: ReviewCreate }) =>
      Promise.allSettled(ids.map((id) => api.review(id, body))),
    onSettled: () => {
      void client.invalidateQueries({ queryKey: qk.booksAll() });
      void client.invalidateQueries({ queryKey: qk.projects() });
    },
  });
}

export function useBulkRun() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({ ids, body }: { ids: number[]; body: RunCreate }) =>
      Promise.allSettled(ids.map((id) => api.createRun(id, body))),
    onSettled: () => {
      void client.invalidateQueries({ queryKey: qk.booksAll() });
      void client.invalidateQueries({ queryKey: qk.projects() });
    },
  });
}

export function usePatchPage(pageId: number) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (patch: PagePatch) => api.patchPage(pageId, patch),
    onSuccess: (detail) => {
      client.setQueryData(qk.page(pageId), detail);
      void client.invalidateQueries({ queryKey: qk.pages(detail.page.book_id) });
    },
  });
}

/** Region edits update the cached PageDetail optimistically: the canvas must not flicker. */
export function useRegionMutations(pageId: number) {
  const client = useQueryClient();
  const replace = (fn: (regions: RegionOut[]) => RegionOut[]) =>
    client.setQueryData<PageDetail>(qk.page(pageId), (d) => (d ? { ...d, regions: fn(d.regions) } : d));
  const settle = () => client.invalidateQueries({ queryKey: qk.page(pageId) });

  const create = useMutation({
    mutationFn: (body: RegionCreate) => api.createRegion(pageId, body),
    onSuccess: (region) => replace((rs) => [...rs, region]),
    onSettled: settle,
  });
  const patch = useMutation({
    mutationFn: ({ id, patch: body }: { id: number; patch: RegionPatch }) => api.patchRegion(id, body),
    onMutate: ({ id, patch: body }) => replace((rs) => rs.map((r) => (r.id === id ? { ...r, ...body } : r))),
    onSuccess: (region) => replace((rs) => rs.map((r) => (r.id === region.id ? region : r))),
    onSettled: settle,
  });
  const reject = useMutation({
    mutationFn: (id: number) => api.rejectRegion(id),
    onMutate: (id) => replace((rs) => rs.map((r) => (r.id === id ? { ...r, status: "rejected" } : r))),
    onSettled: settle,
  });
  return { create, patch, reject };
}

export function useCreateNote() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: NoteCreate) => api.createNote(body),
    onSuccess: (note) => {
      if (note.page_id) void client.invalidateQueries({ queryKey: qk.page(note.page_id) });
      void client.invalidateQueries({ queryKey: qk.book(note.book_id) });
    },
  });
}
