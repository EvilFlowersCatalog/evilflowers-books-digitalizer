/**
 * The handful of primitives every screen uses. Deliberately tiny: a UI kit would
 * cost more than these ~200 lines and fight the dense, tool-like layout.
 */

import type { ButtonHTMLAttributes, CSSProperties, ReactNode } from "react";
import { ApiError } from "../api/client";
import { PROGRESS_STATUSES, type BookStatus, type RunStatus } from "../api/types";
import { percent } from "../lib/format";
import s from "./ui.module.css";

type Variant = "default" | "primary" | "ghost" | "danger";

export function Button({
  variant = "default",
  size,
  className,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; size?: "small" }) {
  const cls = [buttonClass(variant, size), className].filter(Boolean).join(" ");
  return <button type="button" className={cls} {...rest} />;
}

const tone = (color: string) => ({ "--tone": color }) as CSSProperties;

export const STATUS_COLOR: Record<BookStatus, string> = {
  discovered: "var(--st-discovered)",
  queued: "var(--st-queued)",
  running: "var(--st-running)",
  review: "var(--st-review)",
  approved: "var(--st-approved)",
  published: "var(--st-published)",
  error: "var(--st-error)",
  ok: "var(--st-approved)",
};

const RUN_COLOR: Record<RunStatus, string> = {
  queued: "var(--st-queued)",
  running: "var(--st-running)",
  succeeded: "var(--st-approved)",
  failed: "var(--st-error)",
  cancelled: "var(--st-discovered)",
};

export function Badge({ color, children, title }: { color?: string; children: ReactNode; title?: string }) {
  return (
    <span className={s.badge} style={color ? tone(color) : undefined} title={title}>
      {children}
    </span>
  );
}

export function StatusBadge({ status }: { status: BookStatus }) {
  return (
    <span className={s.badge} style={tone(STATUS_COLOR[status])}>
      <span className={`${s.dot} ${status === "running" ? s.pulse : ""}`} />
      {status}
    </span>
  );
}

export function RunStatusBadge({ status }: { status: RunStatus }) {
  return (
    <span className={s.badge} style={tone(RUN_COLOR[status])}>
      <span className={`${s.dot} ${status === "running" ? s.pulse : ""}`} />
      {status}
    </span>
  );
}

/** Book-status distribution: one bar, one segment per status in pipeline order. */
export function SegmentedBar({
  counts,
  legend = true,
}: {
  counts: Partial<Record<BookStatus, number>>;
  legend?: boolean;
}) {
  const merged = { ...counts, approved: (counts.approved ?? 0) + (counts.ok ?? 0) };
  const total = PROGRESS_STATUSES.reduce((n, st) => n + (merged[st] ?? 0), 0);
  return (
    <div>
      <div className={s.segbar} role="img" aria-label={`${total} books by status`}>
        {PROGRESS_STATUSES.map((st) => {
          const n = merged[st] ?? 0;
          return n ? (
            <div
              key={st}
              className={s.seg}
              title={`${st}: ${n}`}
              style={{ width: `${(n / total) * 100}%`, background: STATUS_COLOR[st] }}
            />
          ) : null;
        })}
      </div>
      {legend && (
        <div className={s.legend}>
          {PROGRESS_STATUSES.map((st) => (
            <span key={st}>
              <span className={s.dot} style={tone(STATUS_COLOR[st])} />
              {st} <b>{merged[st] ?? 0}</b>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

export function ProgressBar({ done, total, color }: { done: number; total: number; color?: string }) {
  return (
    <div className={s.progress} role="progressbar" aria-valuenow={percent(done, total)} aria-valuemin={0} aria-valuemax={100}>
      <div className={s.progressFill} style={{ width: `${percent(done, total)}%`, background: color }} />
    </div>
  );
}

export const Kbd = ({ children }: { children: ReactNode }) => <kbd className={s.kbd}>{children}</kbd>;

export function Panel({
  title,
  actions,
  children,
  bodyClass,
  className,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  bodyClass?: string;
  className?: string;
}) {
  return (
    <section className={`${s.panel} ${className ?? ""}`}>
      {(title || actions) && (
        <header className={s.panelHead}>
          <h2>{title}</h2>
          {actions}
        </header>
      )}
      <div className={bodyClass ?? s.panelBody}>{children}</div>
    </section>
  );
}

export const Empty = ({ children }: { children: ReactNode }) => <div className={s.empty}>{children}</div>;
export const Spinner = () => <span className={s.spinner} role="status" aria-label="loading" />;

/** `role="alert"`: a failed edit or re-run is announced, not only painted red. */
export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null;
  const text = error instanceof ApiError ? error.detail : error instanceof Error ? error.message : String(error);
  return (
    <div className={s.error} role="alert">
      {text}
    </div>
  );
}

/** Class names of a `Button`, for links that must look like one (no button inside an `<a>`). */
export function buttonClass(variant: Variant = "default", size?: "small"): string {
  return [s.button, variant !== "default" && s[variant], size === "small" && s.small].filter(Boolean).join(" ");
}

export function PageHeader({ title, sub, actions }: { title: ReactNode; sub?: ReactNode; actions?: ReactNode }) {
  return (
    <div className={s.pageHeader}>
      <div className={s.titles}>
        <h1>{title}</h1>
        {sub && <div className={s.sub}>{sub}</div>}
      </div>
      {actions}
    </div>
  );
}

export function Segmented<T extends string>({
  value,
  options,
  onChange,
  label,
}: {
  value: T;
  options: { value: T; label: ReactNode; title?: string }[];
  onChange: (v: T) => void;
  label: string;
}) {
  return (
    <div className={s.segmented} role="group" aria-label={label}>
      {options.map((o) => (
        <button key={o.value} type="button" aria-pressed={o.value === value} title={o.title} onClick={() => onChange(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

export const Toast = ({ children }: { children: ReactNode }) => (
  <div className={s.toast} role="status">
    {children}
  </div>
);
