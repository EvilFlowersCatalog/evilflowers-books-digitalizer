/**
 * Book-wide scan-mark groups (#14): one row per mark that repeats across leaves (a library
 * stamp, a sticker, bleed-through), with the operator's verdict for every occurrence at once:
 * keep = part of the page, hide = an artefact. Split moves some leaves into a group of their
 * own when one group mixes two marks. Nothing is erased here.
 */

import { useState } from "react";
import { ApiError } from "../../api/client";
import { useMarkGroupMutations, useMarkGroups } from "../../api/hooks";
import type { MarkBand, MarkGroupOut, PageOut } from "../../api/types";
import { Badge, Button, Empty, ErrorNote, Panel, Spinner } from "../../components/ui";
import { canSplit, splitLeaves } from "../../lib/marks";
import { pct } from "../../lib/titlePage";
import s from "./book.module.css";
import sc from "../screen.module.css";
import { LeafLink } from "./BookPanels";

const BAND_COLOR: Record<MarkBand, string> = {
  keep: "var(--ok)",
  review: "var(--warn)",
  hide: "var(--danger)",
};

/** How many leaf links a row shows before "+n more". */
const LEAF_LINKS = 24;

function SplitBox({
  group,
  busy,
  onSplit,
  onCancel,
}: {
  group: MarkGroupOut;
  busy: boolean;
  onSplit: (leaves: number[]) => void;
  onCancel: () => void;
}) {
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const toggle = (leaf: number) => {
    const next = new Set(picked);
    if (next.has(leaf)) next.delete(leaf);
    else next.add(leaf);
    setPicked(next);
  };
  const ok = canSplit(group.on_leaves, picked);
  return (
    <div className={s.splitBox}>
      <div className={sc.hint}>Pick the leaves whose mark is a different one; they move to a new group (verdict: review). At least one leaf stays.</div>
      <div className={s.splitLeaves}>
        {group.on_leaves.map((leaf) => (
          <label key={leaf}>
            <input type="checkbox" checked={picked.has(leaf)} onChange={() => toggle(leaf)} /> #{leaf}
          </label>
        ))}
      </div>
      <div className={sc.row}>
        <Button size="small" variant="primary" disabled={!ok || busy} onClick={() => onSplit(splitLeaves(group.on_leaves, picked))}>
          Split {picked.size || ""} {picked.size === 1 ? "leaf" : "leaves"} off
        </Button>
        <Button size="small" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

function MarkRow({ group, pages, bookId }: { group: MarkGroupOut; pages: PageOut[]; bookId: number }) {
  const { decide, split } = useMarkGroupMutations(bookId);
  const [splitting, setSplitting] = useState(false);
  const busy = decide.isPending || split.isPending;
  const shown = group.on_leaves.slice(0, LEAF_LINKS);
  return (
    <li className={s.mark}>
      <div className={s.markThumb}>
        <img src={group.image_url} alt={`${group.category} mark`} loading="lazy" decoding="async" />
      </div>
      <div className={s.markInfo}>
        <div className={sc.row} style={{ gap: 8, flexWrap: "wrap" }}>
          <b>{group.category}</b>
          <Badge color={BAND_COLOR[group.band]}>{group.band}</Badge>
          {group.decision && <span className="muted">→ {group.decision}</span>}
          <span className="faint" title={group.decider_model ?? undefined}>
            p {pct(group.p)} · by {group.source}
          </span>
          {group.split_from !== null && <span className="faint">· split from #{group.split_from}</span>}
        </div>
        <div className={s.markLeaves}>
          {group.leaves} {group.leaves === 1 ? "leaf" : "leaves"} ·{group.members} occurrence{group.members === 1 ? "" : "s"}:{" "}
          {shown.map((leaf, i) => (
            <span key={leaf}>
              {i > 0 && ", "}
              <LeafLink leaf={leaf} pages={pages} />
            </span>
          ))}
          {group.on_leaves.length > shown.length && <span className="faint"> +{group.on_leaves.length - shown.length} more</span>}
        </div>
        <ErrorNote error={decide.error ?? split.error} />
        {splitting && (
          <SplitBox
            group={group}
            busy={busy}
            onCancel={() => setSplitting(false)}
            onSplit={(leaves) => split.mutate({ id: group.id, leaves }, { onSuccess: () => setSplitting(false) })}
          />
        )}
      </div>
      <div className={s.markActions}>
        <Button size="small" variant={group.band === "keep" ? "primary" : "default"} disabled={busy} title="part of the printed page" onClick={() => decide.mutate({ id: group.id, band: "keep" })}>
          Keep
        </Button>
        <Button size="small" variant={group.band === "hide" ? "primary" : "default"} disabled={busy} title="an artefact (stamp, sticker, bleed-through…)" onClick={() => decide.mutate({ id: group.id, band: "hide" })}>
          Hide
        </Button>
        <Button size="small" variant="ghost" disabled={busy || group.on_leaves.length < 2} onClick={() => setSplitting(!splitting)}>
          Split…
        </Button>
      </div>
    </li>
  );
}

export function MarksPanel({ bookId, pages }: { bookId: number; pages: PageOut[] }) {
  const { data: groups = [], isLoading, error } = useMarkGroups(bookId);
  const missing = error instanceof ApiError && error.status === 404;
  return (
    <Panel title={`Scan marks${groups.length ? ` (${groups.length})` : ""}`}>
      <div className={sc.hint} style={{ marginBottom: 8 }}>
        Nothing is erased automatically: erasing a stamp stays a confirmed “remove” region in the page studio. A verdict reaches
        the pages’ regions at the next re-run of those pages.
      </div>
      {isLoading && <Spinner />}
      {error && !missing ? <ErrorNote error={error} /> : null}
      {!isLoading && !error && groups.length === 0 && <Empty>No repeated scan marks found in this book.</Empty>}
      {missing && <Empty>This server does not expose scan-mark groups.</Empty>}
      {groups.length > 0 && (
        <ul className={s.marks}>
          {groups.map((g) => (
            <MarkRow key={g.id} group={g} pages={pages} bookId={bookId} />
          ))}
        </ul>
      )}
    </Panel>
  );
}
