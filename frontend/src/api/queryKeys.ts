/**
 * TanStack Query keys. Hierarchical arrays so an SSE event can invalidate a whole
 * family with one prefix (`["books"]` hits every filtered books list).
 */

import type { BookQuery } from "./types";

export const qk = {
  projects: () => ["projects"] as const,
  projectConfig: (slug: string) => ["projectConfig", slug] as const,
  booksAll: () => ["books"] as const,
  books: (query: BookQuery) => ["books", query] as const,
  book: (id: number) => ["book", id] as const,
  pages: (bookId: number) => ["pages", bookId] as const,
  page: (id: number) => ["page", id] as const,
  run: (id: number) => ["run", id] as const,
};

export type QueryKey = readonly unknown[];
