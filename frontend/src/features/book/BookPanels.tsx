/**
 * Side panels of the book screen: quality, review, runs with live log,
 * publications, downloads, notes, paths. Each is small and data-driven.
 */

import { useState } from "react";
import { Link } from "react-router-dom";
import { useCancelRun, useCreateNote, useReview } from "../../api/hooks";
import { media } from "../../api/media";
import type { BookDetail, PageOut, ReviewDecision, RunOut } from "../../api/types";
import { Badge, Button, buttonClass, Empty, ErrorNote, Panel, ProgressBar, RunStatusBadge } from "../../components/ui";
import { confirmApproval } from "../../lib/approval";
import { bytes, clock, relativeTime, score } from "../../lib/format";
import { NOT_IN_ACCESS } from "../../api/types";
import s from "./book.module.css";
import sc from "../screen.module.css";

export function QualityPanel({ detail }: { detail: BookDetail }) {
  const q = detail.quality;
  const sig = q.signals;
  const openNow = detail.book.disputed;
  const color = q.passed ? "var(--ok)" : (q.score ?? 1) < 0.6 ? "var(--danger)" : "var(--warn)";
  return (
    <Panel title="Quality gate" actions={q.passed !== undefined && <Badge color={color}>{q.passed ? "passed" : "flagged"}</Badge>}>
      {q.score === undefined ? (
        <span className="faint">No gate yet: run the book first.</span>
      ) : (
        <>
          <div className={sc.row} style={{ alignItems: "flex-start", gap: 16 }}>
            <div className={s.score} style={{ color }}>
              {score(q.score)}
            </div>
            {q.flags && q.flags.length > 0 ? (
              <ul className={s.flagList}>
                {q.flags.map((f) => (
                  <li key={f}>{f}</li>
                ))}
              </ul>
            ) : (
              <span className="muted">Every signal passed.</span>
            )}
          </div>
          {sig && (
            <div className={s.signals}>
              <span>
                <b>{sig.ocr_mean_conf.toFixed(1)}</b>OCR conf
              </span>
              <span title="counted when the quality gate last ran">
                <b>{sig.disputed_regions}</b>disputed at gate
              </span>
              {openNow !== undefined && openNow !== sig.disputed_regions && (
                <span title="open disputed regions right now" style={{ color: "var(--warn)" }}>
                  <b>{openNow}</b>open now
                </span>
              )}
              <span>
                <b>{sig.formula_fallbacks}</b>formula fallbacks
              </span>
              <span>
                <b>{sig.vlm_rejected}</b>VLM rejected
              </span>
            </div>
          )}
        </>
      )}
    </Panel>
  );
}

export function ReviewPanel({ detail }: { detail: BookDetail }) {
  const review = useReview(detail.book.id);
  const [note, setNote] = useState("");
  const decide = (decision: ReviewDecision) => {
    if (decision === "approved" && !confirmApproval([detail.book])) return;
    review.mutate({ decision, ...(note ? { note } : {}) }, { onSuccess: () => setNote("") });
  };
  const history = detail.reviews ?? [];
  return (
    <Panel title="Review">
      <textarea className="textarea" rows={2} placeholder="Note for this decision (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
      <div className={sc.row} style={{ marginTop: 8 }}>
        <Button variant="primary" disabled={review.isPending} onClick={() => decide("approved")}>
          Approve
        </Button>
        <Button disabled={review.isPending} onClick={() => decide("needs_work")}>
          Needs work
        </Button>
        <Button variant="danger" disabled={review.isPending} onClick={() => decide("rejected")}>
          Reject
        </Button>
      </div>
      <ErrorNote error={review.error} />
      {history.length > 0 && (
        <div style={{ marginTop: 10 }}>
          {history
            .slice()
            .reverse()
            .map((r) => (
              <div key={r.id} className={s.note}>
                <b>{r.decision}</b> by {r.author ?? "unknown"} <small>{relativeTime(r.created_at)}</small>
                {r.note && <div className="muted">{r.note}</div>}
              </div>
            ))}
        </div>
      )}
    </Panel>
  );
}

function RunItem({ run, open, onToggle }: { run: RunOut; open: boolean; onToggle: () => void }) {
  const cancel = useCancelRun();
  const active = run.status === "running" || run.status === "queued";
  return (
    <div className={s.run}>
      <button type="button" className={s.runHead} onClick={onToggle} aria-expanded={open}>
        <span className="mono">#{run.id}</span>
        <span>
          <b>{run.kind}</b>
          {run.pages && ` · leaves ${run.pages.join(", ")}`} {run.interactive && <Badge>interactive</Badge>}{" "}
          <span className="faint">
            {run.stage ?? ""} {run.created_by ? `· ${run.created_by}` : ""} · {relativeTime(run.created_at)}
          </span>
        </span>
        <span style={{ width: 90 }}>{active && <ProgressBar done={run.done} total={run.total} />}</span>
        <RunStatusBadge status={run.status} />
      </button>
      {open && (
        <>
          {run.error && <ErrorNote error={run.error} />}
          <div className={s.log} role="log" aria-live="polite">
            {(run.log ?? []).length === 0 ? (
              <span className="faint">no log lines</span>
            ) : (
              run.log.map((l, i) => (
                <div key={i} className={s[l.level] ?? undefined}>
                  {clock(l.ts)} {l.level.padEnd(7)} {l.message}
                </div>
              ))
            )}
          </div>
          {active && (
            <div style={{ padding: "0 16px 12px" }}>
              <Button size="small" variant="danger" disabled={cancel.isPending} onClick={() => cancel.mutate(run.id)}>
                Cancel run
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  );
}

export function RunsPanel({ runs }: { runs: RunOut[] }) {
  const [openId, setOpenId] = useState<number | null>(null);
  const latest = runs[0]?.id ?? null;
  const current = openId ?? latest;
  return (
    <Panel title="Runs" bodyClass="">
      {runs.length === 0 && <Empty>No runs yet.</Empty>}
      {runs.map((r) => (
        <RunItem key={r.id} run={r} open={current === r.id} onToggle={() => setOpenId(current === r.id ? -1 : r.id)} />
      ))}
    </Panel>
  );
}

export function PublicationsPanel({ detail }: { detail: BookDetail }) {
  return (
    <Panel title="Publication history">
      {detail.publications.length === 0 ? (
        <span className="faint">Not published yet.</span>
      ) : (
        detail.publications
          .slice()
          .reverse()
          .map((p) => (
            <div key={p.id} className={s.note}>
              <b>v{p.version}</b> → {p.target} {p.url ? <a href={p.url} target="_blank" rel="noreferrer">entry {p.entry_id.slice(0, 8)}</a> : <code>{p.entry_id}</code>}{" "}
              <small>
                {p.author ? `by ${p.author} · ` : ""}
                {relativeTime(p.published_at)}
              </small>
            </div>
          ))
      )}
    </Panel>
  );
}

export function DownloadsPanel({ detail }: { detail: BookDetail }) {
  return (
    <Panel
      title="Downloads"
      actions={
        <a className={buttonClass("default", "small")} href={media.iaExport(detail.book.id)} download>
          IA export (.zip)
        </a>
      }
    >
      <ul className={s.downloads}>
        {detail.artifacts.map((a) => (
          <li key={a.kind} className={a.exists ? undefined : s.missing}>
            <code style={{ width: 96 }}>{a.kind}</code>
            {a.exists ? (
              <a href={media.bookFile(detail.book.id, a.kind)} download title={a.path}>
                {a.path.split("/").pop()}
              </a>
            ) : (
              <span title={a.path}>not built</span>
            )}
            <span className={sc.spacer} />
            <span className="faint">{bytes(a.size)}</span>
          </li>
        ))}
      </ul>
    </Panel>
  );
}

export function NotesPanel({ detail }: { detail: BookDetail }) {
  const create = useCreateNote();
  const [text, setText] = useState("");
  const notes = detail.notes ?? [];
  return (
    <Panel title="Notes">
      {notes.map((n) => (
        <div key={n.id} className={s.note}>
          {n.text}
          <div>
            <small>
              {n.author ?? "unknown"} · {relativeTime(n.created_at)} {n.page_id ? `· page ${n.page_id}` : ""}
            </small>
          </div>
        </div>
      ))}
      <form
        style={{ marginTop: 8 }}
        onSubmit={(e) => {
          e.preventDefault();
          if (text.trim()) create.mutate({ book_id: detail.book.id, text }, { onSuccess: () => setText("") });
        }}
      >
        <textarea className="textarea" rows={2} value={text} onChange={(e) => setText(e.target.value)} placeholder="Add a note on this book" />
        <Button type="submit" size="small" disabled={!text.trim() || create.isPending}>
          Add note
        </Button>
      </form>
    </Panel>
  );
}

export function PathsPanel({ detail }: { detail: BookDetail }) {
  return (
    <Panel title="Paths">
      <dl className={sc.dl}>
        {Object.entries(detail.paths).map(([k, v]) => (
          <div key={k} style={{ display: "contents" }}>
            <dt>{k}</dt>
            <dd className="mono">{v ?? "—"}</dd>
          </div>
        ))}
      </dl>
    </Panel>
  );
}

export function PagesGrid({ pages }: { pages: PageOut[] }) {
  if (pages.length === 0) return <Empty>No leaves yet: the book has not been cleaned.</Empty>;
  return (
    <div className={s.pages}>
      {pages.map((p) => (
        <Link
          key={p.id}
          to={`/pages/${p.id}`}
          className={`${s.leaf} ${p.add_to_access ? "" : s.excluded}`}
          title={`leaf ${p.index} · ${p.page_type}${p.add_to_access ? "" : " · not in access formats"}${
            p.disputed ? ` · ${p.disputed} disputed` : ""
          }`}
        >
          <div className={s.thumb}>
            <img src={p.thumb ?? media.pageImage(p.id, "clean", 240, p.version)} alt={`leaf ${p.index}`} loading="lazy" />
            {(p.disputed ?? 0) > 0 && (
              <span className={s.disputed} aria-label={`${p.disputed} disputed`}>
                {p.disputed} ?
              </span>
            )}
            {p.page_type !== "Normal" && <span className={s.typeBadge}>{p.page_type}</span>}
          </div>
          <div className={s.leafMeta}>
            <span className={s.num}>#{p.index}</span>
            <span>{p.page_number ? `p. ${p.page_number}` : ""}</span>
            <span className={sc.spacer} />
            {!p.add_to_access && <Badge color="var(--text-faint)">{NOT_IN_ACCESS.has(p.page_type) ? "excl." : "hidden"}</Badge>}
          </div>
        </Link>
      ))}
    </div>
  );
}
