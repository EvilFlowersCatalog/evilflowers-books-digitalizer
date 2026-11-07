/**
 * Regions tab: disputed regions first (reason + one-click accept / relabel), then
 * the editor for the selected region and the full list.
 */

import { useState, type CSSProperties, type KeyboardEvent } from "react";
import { useCreateNote } from "../../api/hooks";
import { REGION_ACTIONS, REGION_KINDS, type RegionAction, type RegionKind, type RegionOut, type RegionPatch } from "../../api/types";
import { Badge, Button, Kbd } from "../../components/ui";
import { ACTION_LABEL, disputedRegions, KIND_COLOR, KIND_LABEL, relabel } from "../../lib/regions";
import s from "./studio.module.css";

interface Props {
  bookId: number;
  pageId: number;
  regions: RegionOut[];
  selectedId: number | null;
  onSelect: (id: number | null) => void;
  onPatch: (id: number, patch: RegionPatch) => void;
  onReject: (id: number) => void;
}

const kindStyle = (kind: RegionKind) => ({ "--kind": KIND_COLOR[kind], background: KIND_COLOR[kind] }) as CSSProperties;

function Disputes({ regions, selectedId, onSelect, onPatch }: Omit<Props, "bookId" | "pageId" | "onReject">) {
  const disputed = disputedRegions(regions);
  if (disputed.length === 0) return null;
  return (
    <div className={s.section}>
      <div className={s.sectionTitle}>
        Disputed <Badge color="var(--st-review)">{disputed.length}</Badge>
      </div>
      {disputed.map((r) => {
        const options = [...new Set<RegionKind>([r.kind, ...(r.alternatives ?? [])])];
        return (
          <div
            key={r.id}
            className={s.dispute}
            aria-current={r.id === selectedId}
            role="group"
            aria-label={`Disputed ${KIND_LABEL[r.kind].toLowerCase()}: ${r.reason ?? "needs a decision"}`}
            tabIndex={0}
            onFocus={(e) => e.target === e.currentTarget && onSelect(r.id)}
            onClick={() => onSelect(r.id)}
          >
            <b>{KIND_LABEL[r.kind]}</b> <span className="faint">· {r.source} · {(r.confidence * 100).toFixed(0)}%</span>
            <p>{r.reason ?? "needs a decision"}</p>
            <div className={s.chips}>
              <Button size="small" variant="primary" onClick={(e) => { e.stopPropagation(); onPatch(r.id, { status: "confirmed" }); }}>
                Accept {KIND_LABEL[r.kind].toLowerCase()} · {ACTION_LABEL[r.action].toLowerCase()}
              </Button>
              {options
                .filter((k) => k !== r.kind)
                .map((k) => (
                  <Button key={k} size="small" onClick={(e) => { e.stopPropagation(); onPatch(r.id, relabel(k)); }}>
                    It is {KIND_LABEL[k].toLowerCase()}
                  </Button>
                ))}
              {r.kind === "stamp" || r.kind === "handwriting" || r.kind === "sticker" ? (
                <Button size="small" onClick={(e) => { e.stopPropagation(); onPatch(r.id, { status: "confirmed", action: "keep" }); }}>
                  Keep on page
                </Button>
              ) : null}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function RegionEditor({ region, bookId, pageId, onPatch, onReject }: { region: RegionOut; bookId: number; pageId: number; onPatch: Props["onPatch"]; onReject: Props["onReject"] }) {
  const [note, setNote] = useState("");
  const createNote = useCreateNote();
  return (
    <div className={`${s.section} ${s.editor}`}>
      <div className={s.sectionTitle}>
        Region #{region.id} <Badge color={region.status === "disputed" ? "var(--st-review)" : undefined}>{region.status}</Badge>
        <span style={{ flex: 1 }} />
        <span className="faint" style={{ textTransform: "none", letterSpacing: 0 }}>
          {region.source}
          {region.author ? ` · ${region.author}` : ""}
        </span>
      </div>
      <div className={s.editorGrid}>
        <div>
          <label className="label" htmlFor="region-kind">Kind</label>
          <select id="region-kind" className="select" value={region.kind} onChange={(e) => onPatch(region.id, relabel(e.target.value as RegionKind))}>
            {REGION_KINDS.map((k) => (
              <option key={k} value={k}>{KIND_LABEL[k]}</option>
            ))}
          </select>
        </div>
        <div>
          <label className="label" htmlFor="region-action">Action</label>
          <select id="region-action" className="select" value={region.action} onChange={(e) => onPatch(region.id, { action: e.target.value as RegionAction })}>
            {REGION_ACTIONS.map((a) => (
              <option key={a} value={a}>{ACTION_LABEL[a]}</option>
            ))}
          </select>
        </div>
      </div>
      <div className={s.inline} style={{ marginBottom: 10 }}>
        <Button size="small" variant="primary" disabled={region.status === "confirmed"} onClick={() => onPatch(region.id, { status: "confirmed" })}>
          Confirm <Kbd>↵</Kbd>
        </Button>
        <Button size="small" variant="danger" disabled={region.status === "rejected"} onClick={() => onReject(region.id)}>
          Reject <Kbd>x</Kbd>
        </Button>
        <span className="faint mono" style={{ fontSize: 11 }}>[{region.bbox.join(", ")}]</span>
      </div>
      <form
        className={s.inline}
        onSubmit={(e) => {
          e.preventDefault();
          if (note.trim()) createNote.mutate({ book_id: bookId, page_id: pageId, region_id: region.id, text: note }, { onSuccess: () => setNote("") });
        }}
      >
        <input className="input" placeholder="Note on this region…" value={note} onChange={(e) => setNote(e.target.value)} />
        <Button size="small" type="submit" disabled={!note.trim()}>Add</Button>
      </form>
    </div>
  );
}

export function RegionsPanel(props: Props) {
  const { regions, selectedId, onSelect, bookId, pageId, onPatch, onReject } = props;
  const selected = regions.find((r) => r.id === selectedId);
  const live = regions.filter((r) => r.status !== "rejected");
  // roving tab stop: one Tab reaches the list, arrows move inside it
  const focusable = selected && selected.status !== "rejected" ? selected.id : live[0]?.id;
  const moveFocus = (e: KeyboardEvent<HTMLUListElement>) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const items = [...e.currentTarget.querySelectorAll<HTMLLIElement>("li[tabindex]")];
    const at = items.indexOf(document.activeElement as HTMLLIElement);
    items[Math.max(0, Math.min(items.length - 1, at + (e.key === "ArrowDown" ? 1 : -1)))]?.focus();
  };
  return (
    <>
      <Disputes regions={regions} selectedId={selectedId} onSelect={onSelect} onPatch={onPatch} />
      {selected && <RegionEditor key={selected.id} region={selected} bookId={bookId} pageId={pageId} onPatch={onPatch} onReject={onReject} />}
      <div className={s.section}>
        <div className={s.sectionTitle}>All regions ({regions.filter((r) => r.status !== "rejected").length})</div>
        <ul className={s.regionList} role="listbox" aria-label="Regions on this page" onKeyDown={moveFocus}>
          {regions.map((r) => (
            <li
              key={r.id}
              role="option"
              className={`${s.regionItem} ${r.status === "rejected" ? s.rejected : ""}`}
              aria-selected={r.id === selectedId}
              aria-disabled={r.status === "rejected"}
              tabIndex={r.status === "rejected" ? undefined : r.id === focusable ? 0 : -1}
              onFocus={() => r.status !== "rejected" && r.id !== selectedId && onSelect(r.id)}
              onClick={() => onSelect(r.status === "rejected" ? null : r.id)}
            >
              <span className={`${s.swatch} ${r.status === "disputed" ? s.dashed : ""}`} style={kindStyle(r.kind)} />
              <span>
                {KIND_LABEL[r.kind]} <span className="faint">· {ACTION_LABEL[r.action]}</span>
              </span>
              <span className="faint" style={{ fontSize: 11 }}>{r.status}</span>
            </li>
          ))}
        </ul>
      </div>
      <div className={s.section}>
        <div className={s.sectionTitle}>Keyboard</div>
        <div className={s.keys}>
          <Kbd>t</Kbd> text <Kbd>f</Kbd> formula
          <Kbd>b</Kbd> table <Kbd>p</Kbd> picture
          <Kbd>s</Kbd> stamp <Kbd>x</Kbd> reject
          <Kbd>↵</Kbd> confirm <Kbd>←/→</Kbd> page
          <Kbd>n/N</Kbd> next/prev region <Kbd>d</Kbd> next disputed
          <Kbd>Esc</Kbd> deselect / stop drawing
        </div>
      </div>
    </>
  );
}
