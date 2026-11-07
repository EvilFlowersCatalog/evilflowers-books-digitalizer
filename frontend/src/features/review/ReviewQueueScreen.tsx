/**
 * Screen 5 — Review queue: flagged books, worst score first, driven by keyboard
 * (`j`/`k` move, `a` approve, `o` open) so an operator can clear a queue fast.
 */

import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useBooks, useReview } from "../../api/hooks";
import type { BookOut } from "../../api/types";
import { Badge, Button, Empty, ErrorNote, Kbd, PageHeader, Panel, Spinner, StatusBadge } from "../../components/ui";
import { confirmApproval } from "../../lib/approval";
import { relativeTime, score } from "../../lib/format";
import { useSession } from "../../lib/session";
import { keyLike, reviewCommand } from "../../lib/shortcuts";
import s from "../screen.module.css";

function ApproveButton({ book }: { book: BookOut }) {
  const review = useReview(book.id);
  return (
    <>
      <Button
        size="small"
        variant="primary"
        disabled={review.isPending}
        onClick={() => confirmApproval([book]) && review.mutate({ decision: "approved" })}
      >
        Approve
      </Button>
      {review.error ? <ErrorNote error={review.error} /> : null}
    </>
  );
}

const PAGE_SIZE = 200;

export function ReviewQueueScreen() {
  const { project } = useSession();
  const navigate = useNavigate();
  const [limit, setLimit] = useState(PAGE_SIZE);
  const { data, isLoading, error } = useBooks(project ? { project: project.slug, status: "review", sort: "quality_score", limit } : null);
  const items = data?.items ?? [];
  const total = data?.total ?? items.length;
  const [cursor, setCursor] = useState(0);
  const [approving, setApproving] = useState<number | null>(null);
  // one index for the highlight *and* the key target: after an approval the list
  // shrinks, and a cursor past its end must not approve a row nobody sees selected
  const idx = Math.max(0, Math.min(cursor, items.length - 1));
  const current = items[idx];
  const review = useReview(current?.id ?? 0);
  const busy = approving !== null || review.isPending;
  const rows = useRef<HTMLTableSectionElement>(null);

  useEffect(() => {
    if (cursor !== idx) setCursor(idx);
  }, [cursor, idx]);

  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      const cmd = reviewCommand(keyLike(ev));
      if (!cmd || !current) return;
      ev.preventDefault();
      if (cmd.type === "move") setCursor(Math.max(0, Math.min(items.length - 1, idx + cmd.delta)));
      if (cmd.type === "open") navigate(`/books/${current.id}`);
      if (cmd.type === "approve" && !busy && confirmApproval([current])) {
        setApproving(current.id);
        review.mutate({ decision: "approved" }, { onSettled: () => setApproving(null) });
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [current, idx, items.length, navigate, review, busy]);

  useEffect(() => {
    rows.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" });
  }, [idx]);

  return (
    <div className={s.screen}>
      <PageHeader
        title="Review queue"
        sub={`${total} flagged ${total === 1 ? "book" : "books"}, lowest quality score first${
          items.length < total ? ` (showing ${items.length})` : ""
        }`}
        actions={
          <span className="muted" style={{ fontSize: 12 }}>
            <Kbd>j</Kbd>/<Kbd>k</Kbd> move · <Kbd>a</Kbd> approve · <Kbd>o</Kbd> open
          </span>
        }
      />
      <ErrorNote error={error ?? review.error} />
      <Panel bodyClass="">
        {isLoading ? (
          <Empty>
            <Spinner />
          </Empty>
        ) : items.length === 0 ? (
          <Empty>Nothing to review. The quality gate is happy.</Empty>
        ) : (
          <table className={s.table}>
            <thead>
              <tr>
                <th className={s.num}>Score</th>
                <th>Book</th>
                <th>Why it was flagged</th>
                <th className={s.num}>Disputed</th>
                <th>Status</th>
                <th>Updated</th>
                <th />
              </tr>
            </thead>
            <tbody ref={rows}>
              {items.map((b, i) => (
                <tr key={b.id} aria-selected={i === idx} onClick={() => setCursor(i)}>
                  <td className={`${s.num} mono`} style={{ color: (b.quality_score ?? 0) < 0.6 ? "var(--danger)" : "var(--warn)" }}>
                    {score(b.quality_score)}
                  </td>
                  <td className={s.titleCell}>
                    <Link to={`/books/${b.id}`}>{b.title || b.slug}</Link>
                    <small>{b.item_id}</small>
                  </td>
                  <td className={s.flags}>{(b.flags ?? []).join(" · ") || "—"}</td>
                  <td className={s.num}>
                    {b.disputed ? <Badge color="var(--st-review)" title="open disputed regions now">{b.disputed}</Badge> : <span className="faint">0</span>}
                  </td>
                  <td>{approving === b.id ? <Spinner /> : <StatusBadge status={b.status} />}</td>
                  <td className="muted">{relativeTime(b.updated_at)}</td>
                  <td>
                    <ApproveButton book={b} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {items.length < total && (
          <Empty>
            <Button size="small" onClick={() => setLimit((n) => n + PAGE_SIZE)}>
              Load {Math.min(PAGE_SIZE, total - items.length)} more
            </Button>
          </Empty>
        )}
      </Panel>
    </div>
  );
}
