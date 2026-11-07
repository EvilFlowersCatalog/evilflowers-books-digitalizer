/**
 * Minimal typed HTTP client for `/api/v1`.
 *
 * The transport is swappable so mock mode (VITE_MOCK=1) and unit tests can answer
 * requests in-process without a server, while every caller keeps using the same
 * typed endpoint functions.
 */

import { getOperator, operatorHeader } from "./operator";

export const API_BASE = "/api/v1";

/** A non-2xx answer; `detail` is the API's `{"detail": str}` (or the status text). */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(`${status}: ${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }

  /** 409 = the state machine refused the transition (domain.TransitionError). */
  get isConflict(): boolean {
    return this.status === 409;
  }
}

export type Transport = (url: string, init: RequestInit) => Promise<Response>;

let transport: Transport = (url, init) => fetch(url, init);
let operatorSource: () => string = getOperator;

export function setTransport(next: Transport): void {
  transport = next;
}

/** Tests inject the operator name without touching localStorage. */
export function setOperatorSource(source: () => string): void {
  operatorSource = source;
}

export type QueryValue = string | number | boolean | null | undefined;

/** `/api/v1` + path + query string; `undefined`/`null`/`""` values are dropped. */
export function buildUrl(path: string, query?: Record<string, QueryValue>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value === undefined || value === null || value === "") continue;
    params.set(key, String(value));
  }
  const qs = params.toString();
  return `${API_BASE}${path}${qs ? `?${qs}` : ""}`;
}

export function buildHeaders(hasBody: boolean): Record<string, string> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (hasBody) headers["Content-Type"] = "application/json";
  const operator = operatorSource();
  if (operator) headers["X-Operator"] = operatorHeader(operator);
  return headers;
}

async function errorDetail(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (body && typeof body === "object" && "detail" in body) {
      const detail = (body as { detail: unknown }).detail;
      return typeof detail === "string" ? detail : JSON.stringify(detail);
    }
  } catch {
    /* not JSON: fall through to the status text */
  }
  return response.statusText || "request failed";
}

export interface RequestOptions {
  query?: Record<string, QueryValue>;
  body?: unknown;
  signal?: AbortSignal;
}

export async function request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
  const hasBody = opts.body !== undefined;
  const init: RequestInit = { method, headers: buildHeaders(hasBody) };
  if (hasBody) init.body = JSON.stringify(opts.body);
  if (opts.signal) init.signal = opts.signal;
  const response = await transport(buildUrl(path, opts.query), init);
  if (!response.ok) throw new ApiError(response.status, await errorDetail(response));
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}
