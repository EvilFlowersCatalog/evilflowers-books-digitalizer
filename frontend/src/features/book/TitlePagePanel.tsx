/**
 * Title-page suggestions (#15): the decider's picks from the title page's OCR lines, prefilled
 * for the operator. Nothing reaches the item metadata until Confirm; an empty input means "the
 * page does not say it". Once confirmed, the comparison is shown read-only (it is also a
 * labelled `title_page` case for decider calibration).
 */

import { useEffect, useState } from "react";
import { ApiError } from "../../api/client";
import { useConfirmTitlePage, useTitlePage } from "../../api/hooks";
import type { PageOut, TitleOutcome, TitleSuggestion } from "../../api/types";
import { Badge, Button, ErrorNote, Panel } from "../../components/ui";
import { relativeTime } from "../../lib/format";
import { confirmBody, pct, titleDraft, valueText, type TitleDraft } from "../../lib/titlePage";
import s from "./book.module.css";
import sc from "../screen.module.css";
import { LeafLink } from "./BookPanels";

const OUTCOME_COLOR: Record<TitleOutcome, string> = {
  accepted: "var(--ok)",
  changed: "var(--warn)",
  none: "var(--text-faint)",
};

function Source({ s: sug, pages }: { s: Pick<TitleSuggestion, "p" | "line" | "leaf">; pages: PageOut[] }) {
  return (
    <div className={sc.hint}>
      p {pct(sug.p)}
      {sug.line && <> · “{sug.line}”</>}
      {sug.leaf !== null && (
        <>
          {" "}
          · <LeafLink leaf={sug.leaf} pages={pages}>leaf {sug.leaf}</LeafLink>
        </>
      )}
    </div>
  );
}

export function TitlePagePanel({ bookId, pages }: { bookId: number; pages: PageOut[] }) {
  const { data, error } = useTitlePage(bookId);
  const confirm = useConfirmTitlePage(bookId);
  const [draft, setDraft] = useState<TitleDraft>({});
  // seed once per suggestion set: a refetch (or the confirm answer) must not wipe typing
  const seed = data ? `${bookId}|${data.suggested_at ?? ""}|${data.suggestions.map((x) => x.field).join(",")}` : null;
  useEffect(() => {
    if (data) setDraft(titleDraft(data.suggestions));
  }, [seed]); // `data` is read only when `seed` changes, on purpose

  if (error) return error instanceof ApiError && error.status === 404 ? null : <ErrorNote error={error} />;
  if (!data || data.suggestions.length === 0) return null;
  const confirmed = data.confirmed;

  return (
    <Panel
      title="Title page"
      actions={
        confirmed ? (
          <Badge color="var(--ok)">confirmed</Badge>
        ) : (
          <Button size="small" variant="primary" disabled={confirm.isPending} onClick={() => confirm.mutate(confirmBody(draft))}>
            Confirm
          </Button>
        )
      }
    >
      {confirmed ? (
        <>
          <dl className={sc.dl}>
            {data.suggestions.map((sug) => {
              const f = confirmed.fields[sug.field];
              return (
                <div key={sug.field} style={{ display: "contents" }}>
                  <dt>{sug.field}</dt>
                  <dd>
                    {f ? (
                      <>
                        <Badge color={OUTCOME_COLOR[f.outcome]}>{f.outcome}</Badge>{" "}
                        {valueText(f.final).replace(/\n/g, "; ") || <span className="faint">(not on the page)</span>}
                        {f.outcome === "changed" && valueText(f.suggested) && (
                          <div className={sc.hint}>suggested: {valueText(f.suggested).replace(/\n/g, "; ")}</div>
                        )}
                      </>
                    ) : (
                      <span className="faint">—</span>
                    )}
                  </dd>
                </div>
              );
            })}
          </dl>
          <div className={sc.hint} style={{ marginTop: 8 }}>
            Confirmed by {confirmed.labeller ?? "unknown"} · {relativeTime(confirmed.at)}
            {confirmed.decider_model ? ` · suggested by ${confirmed.decider_model}` : ""}
          </div>
        </>
      ) : (
        <>
          <div className={sc.hint} style={{ marginBottom: 8 }}>
            Nothing is written to the item metadata before Confirm. An empty field means the page does not say it.
          </div>
          <ErrorNote error={confirm.error} />
          <div className={s.titleFields}>
            {data.suggestions.map((sug) => {
              const id = `tp-${sug.field}`;
              const value = draft[sug.field] ?? "";
              const onChange = (e: { target: { value: string } }) => setDraft({ ...draft, [sug.field]: e.target.value });
              return (
                <div key={sug.field}>
                  <label className="label" htmlFor={id}>
                    {sug.field} <span className="faint">→ {sug.metadata_field}</span>
                  </label>
                  {sug.field === "authors" ? (
                    <textarea id={id} className="textarea" rows={Math.max(2, value.split("\n").length)} value={value} onChange={onChange} placeholder="one per line" />
                  ) : (
                    <input id={id} className="input" value={value} onChange={onChange} />
                  )}
                  <Source s={sug} pages={pages} />
                </div>
              );
            })}
          </div>
          {data.decider_model && (
            <div className={sc.hint} style={{ marginTop: 8 }}>
              Suggested by {data.decider_model}
              {data.suggested_at ? ` · ${relativeTime(data.suggested_at)}` : ""}
            </div>
          )}
        </>
      )}
    </Panel>
  );
}
