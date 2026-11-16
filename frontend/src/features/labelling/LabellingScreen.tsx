/**
 * Labelling for decider calibration (#7): one catalog item at a time, one keypress per answer.
 *
 * Never shows the model's answer (the API does not send it). Digits pick a label, `s` skips
 * (stored, never exported). Tables answer `is_data_table` (1–2) and, optionally,
 * `table_quality` (3–5) on one screen; Enter saves. Subjects: pick the UDC top level, then
 * the category.
 */

import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { labelExportUrl } from "../../api/endpoints";
import { useLabel, useLabelNext, useLabelSample, useLabelSummary, useLabelTasks } from "../../api/hooks";
import {
  SKIP,
  type CategoryOption,
  type LabelAnswer,
  type LabelItemOut,
  type LabelTaskName,
  type LabelTaskOut,
} from "../../api/types";
import { Badge, Button, Empty, ErrorNote, Kbd, PageHeader, Panel, ProgressBar, Spinner } from "../../components/ui";
import { useSession } from "../../lib/session";
import { keyLike } from "../../lib/shortcuts";
import s from "../screen.module.css";
import { keyFor, labelCommand, udcTop } from "./labelKeys";
import l from "./labelling.module.css";

function TaskList({ tasks, current, onPick }: { tasks: LabelTaskOut[]; current: LabelTaskName | null; onPick: (t: LabelTaskName) => void }) {
  return (
    <ul className={l.tasks}>
      {tasks.map((t) => (
        <li key={t.name}>
          <button type="button" className={`${l.task} ${t.name === current ? l.taskActive : ""}`} onClick={() => onPick(t.name)}>
            <b className="mono">{t.name}</b>
            {t.companion_of && <small> (on the {t.companion_of} screen)</small>}
            <ProgressBar done={Math.min(t.labelled, t.target)} total={t.target} />
            <small className="muted">
              {t.labelled}/{t.target} labels · {t.items} items · mine {t.mine} · double {t.double}
            </small>
          </button>
        </li>
      ))}
    </ul>
  );
}

interface Choice {
  key: string;
  hint: string | null;
  /** shown instead of the key (subjects: the key is a category id) */
  text?: string;
}

function Choices({ choices, picked, offset = 0, onPick }: { choices: Choice[]; picked?: string; offset?: number; onPick: (key: string) => void }) {
  return (
    <div className={l.choices}>
      {choices.map((c, i) => (
        <Button key={c.key} variant={picked === c.key ? "primary" : "default"} onClick={() => onPick(c.key)} title={c.hint ?? undefined}>
          {keyFor(i + offset) && <Kbd>{keyFor(i + offset)}</Kbd>} {c.text ?? c.key}
          {c.hint && <small className={l.hint}>{c.hint}</small>}
        </Button>
      ))}
    </div>
  );
}

function Crop({ item }: { item: LabelItemOut }) {
  if (!item.image_url) return null;
  return <img className={l.crop} src={item.image_url} alt={`region ${item.payload.region ?? ""}`} />;
}

function Context({ item }: { item: LabelItemOut }) {
  const p = item.payload;
  return (
    <div className={l.context}>
      <div className="muted">
        {p.entry_title} · page {p.page} · <span className="mono">{p.region}</span> · {item.language ?? "?"}
      </div>
      {p.printed_caption && <p><b>Caption:</b> {p.printed_caption}</p>}
      {p.page_text && <p className={l.pageText}>{p.page_text}</p>}
    </div>
  );
}

function SectionPair({ item }: { item: LabelItemOut }) {
  const side = (name: "A" | "B", sec: LabelItemOut["payload"]["a"]) =>
    sec && (
      <section className={l.side}>
        <h3>
          Section {name}: {sec.title || <i>untitled</i>}
        </h3>
        <div className="muted">{sec.entry_title ?? sec.entry_id} · p. {sec.first_page ?? "?"}</div>
        <p className={l.pageText}>{sec.text || <i>No text found for this section.</i>}</p>
      </section>
    );
  return (
    <div className={l.pair}>
      {side("A", item.payload.a)}
      {side("B", item.payload.b)}
    </div>
  );
}

function EntryCard({ item }: { item: LabelItemOut }) {
  const p = item.payload;
  return (
    <div className={l.context}>
      <h3>{p.title}</h3>
      <div className="muted">{item.language ?? "?"}</div>
      {p.summary && <p>{p.summary}</p>}
      {(p.table_of_contents ?? []).length > 0 && <pre className={l.toc}>{(p.table_of_contents ?? []).join("\n")}</pre>}
    </div>
  );
}

function groupsOf(options: CategoryOption[]): [string, CategoryOption[]][] {
  const groups = new Map<string, CategoryOption[]>();
  for (const o of options) groups.set(udcTop(o.term), [...(groups.get(udcTop(o.term)) ?? []), o]);
  return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b));
}

export function LabellingScreen() {
  const { operator } = useSession();
  const tasks = useLabelTasks();
  const visible = useMemo(() => (tasks.data ?? []).filter((t) => !t.companion_of), [tasks.data]);
  const [current, setCurrent] = useState<LabelTaskName | null>(null);
  const task = current ?? visible[0]?.name ?? null;
  const meta = visible.find((t) => t.name === task);
  const companion = (tasks.data ?? []).find((t) => t.companion_of === task);
  const next = useLabelNext(operator ? task : null);
  const summary = useLabelSummary(task);
  const label = useLabel();
  const sample = useLabelSample();
  const item = next.data ?? null;
  const resetSample = sample.reset;
  useEffect(() => {
    resetSample(); // another task: its own "Sample more" state
  }, [task, resetSample]);
  const [quality, setQuality] = useState<string | undefined>();
  const [dataTable, setDataTable] = useState<string | undefined>();
  const [group, setGroup] = useState<string | null>(null);
  const groups = useMemo(() => groupsOf(item?.payload.options ?? []), [item]);

  useEffect(() => {
    setQuality(undefined);
    setDataTable(undefined);
    setGroup(null);
  }, [item?.id]);

  const send = (labels: LabelAnswer[]) => {
    if (!item || label.isPending) return;
    label.mutate({ itemId: item.id, labels });
  };
  const answer = (value: string) => {
    if (!task) return;
    if (companion && value !== SKIP) {
      setDataTable(value);
      return;
    }
    send(companion && value === SKIP ? [{ task, value }, { task: companion.name, value }] : [{ task, value }]);
  };
  const saveTable = () => {
    if (!task || !companion || !dataTable) return;
    send([{ task, value: dataTable }, ...(quality ? [{ task: companion.name, value: quality }] : [])]);
  };

  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      if (!item || !meta) return;
      const own = meta.labels.length || (group === null ? groups.length : (groups.find(([g]) => g === group)?.[1].length ?? 0));
      const cmd = labelCommand(keyLike(ev), own + (companion?.labels.length ?? 0));
      if (!cmd) return;
      ev.preventDefault();
      if (cmd.type === "skip") answer(SKIP);
      else if (cmd.type === "submit") saveTable();
      else if (cmd.index >= own && companion) setQuality(companion.labels[cmd.index - own]?.key);
      else if (meta.labels.length) answer(meta.labels[cmd.index]!.key);
      else if (group === null) setGroup(groups[cmd.index]?.[0] ?? null);
      else {
        const opt = groups.find(([g]) => g === group)?.[1][cmd.index];
        if (opt) answer(opt.id);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  if (!operator)
    return (
      <div className={s.screen}>
        <PageHeader title="Labelling" />
        <Empty>
          Labels are recorded under your name. <Link to="/settings">Set your name</Link> first.
        </Empty>
      </div>
    );

  return (
    <div className={s.screen}>
      <PageHeader
        title="Labelling"
        sub="Hand labels for decision-model calibration. The model's answer is never shown."
        actions={
          <span className="muted" style={{ fontSize: 12 }}>
            <Kbd>1</Kbd>…<Kbd>0</Kbd> label · <Kbd>s</Kbd> skip (unsure){companion && <> · <Kbd>Enter</Kbd> save</>}
          </span>
        }
      />
      <ErrorNote error={tasks.error ?? next.error ?? label.error} />
      <div className={l.layout}>
        <Panel title="Tasks" bodyClass="">
          {tasks.isLoading ? <Spinner /> : <TaskList tasks={tasks.data ?? []} current={task} onPick={setCurrent} />}
          {task && (
            <div className={l.export}>
              <Button size="small" disabled={sample.isPending || sample.isSuccess} onClick={() => sample.mutate({ task, n: 100 })}>
                {sample.isSuccess ? "Sampling… (a few minutes)" : "Sample 100 more"}
              </Button>
              <ErrorNote error={sample.error} />
              <a href={labelExportUrl(task)} download>
                Export {task}.jsonl
              </a>
              {summary.data && (
                <small className="muted">
                  κ {summary.data.kappa?.toFixed(2) ?? "—"} on {summary.data.double} double · agreement{" "}
                  {summary.data.agreement !== null ? `${Math.round(summary.data.agreement * 100)} %` : "—"}
                </small>
              )}
            </div>
          )}
        </Panel>
        <Panel title={meta?.title ?? "—"} bodyClass="">
          {next.isLoading || label.isPending ? (
            <Empty>
              <Spinner />
            </Empty>
          ) : !item || !meta ? (
            <Empty>Nothing left for you in this task. Sample more (left), or `studio sample-labels --task {task}`.</Empty>
          ) : (
            <div className={l.item}>
              {item.kind === "section_pair" ? (
                <SectionPair item={item} />
              ) : item.kind === "entry" ? (
                <EntryCard item={item} />
              ) : (
                <div className={l.figure}>
                  <Crop item={item} />
                  <div>
                    <Context item={item} />
                    {item.payload.markdown !== undefined && <pre className={l.markdown}>{item.payload.markdown || "(no markdown)"}</pre>}
                  </div>
                </div>
              )}
              {meta.labels.length > 0 && <Choices choices={meta.labels} picked={dataTable} onPick={answer} />}
              {companion && (
                <>
                  <div className="muted">{companion.title} (optional)</div>
                  <Choices choices={companion.labels} picked={quality} offset={meta.labels.length} onPick={setQuality} />
                  <Button variant="primary" disabled={!dataTable} onClick={saveTable}>
                    Save <Kbd>Enter</Kbd>
                  </Button>
                </>
              )}
              {item.kind === "entry" &&
                (group === null ? (
                  <Choices
                    choices={groups.map(([g, opts]) => ({ key: g, text: `${g}…`, hint: opts.map((o) => o.label).slice(0, 3).join(", ") }))}
                    onPick={setGroup}
                  />
                ) : (
                  <>
                    <Button size="small" variant="ghost" onClick={() => setGroup(null)}>
                      ← all top levels
                    </Button>
                    <Choices
                      choices={(groups.find(([g]) => g === group)?.[1] ?? []).map((o) => ({ key: o.id, text: o.term, hint: o.label }))}
                      onPick={answer}
                    />
                  </>
                ))}
              <div className={l.footer}>
                <Button size="small" variant="ghost" onClick={() => answer(SKIP)}>
                  Skip (unsure) <Kbd>s</Kbd>
                </Button>
                {item.language && <Badge>{item.language}</Badge>}
                <span className="faint mono">{item.key}</span>
              </div>
            </div>
          )}
        </Panel>
      </div>
    </div>
  );
}
