/**
 * Item metadata editor with IA `_meta.xml` field names. Repeatable fields are one
 * value per line; the save sends only changed fields (a partial ItemMetadata).
 * OPAC lookup fills *empty* fields only, the same rule as `ItemMetadata.merge`.
 */

import { useEffect, useMemo, useState } from "react";
import { ApiError } from "../../api/client";
import { api } from "../../api/endpoints";
import { usePatchMetadata } from "../../api/hooks";
import type { ItemMetadata } from "../../api/types";
import { Button, ErrorNote, Panel } from "../../components/ui";
import s from "../screen.module.css";

type Key = keyof ItemMetadata;
interface Field {
  key: Key;
  ia: string;
  hint?: string;
  wide?: boolean;
  readOnly?: boolean;
}

const LIST_FIELDS = new Set<Key>(["creator", "subject", "isbn", "language", "description", "notes", "oclc_id", "lccn", "external_identifier", "collection"]);
const NUMBER_FIELDS = new Set<Key>(["ppi", "imagecount", "foldoutcount"]);

const GROUPS: [string, Field[]][] = [
  [
    "Bibliographic",
    [
      { key: "identifier", ia: "identifier", readOnly: true, hint: "permanent" },
      { key: "mediatype", ia: "mediatype", readOnly: true },
      { key: "title", ia: "title", wide: true },
      { key: "creator", ia: "creator", hint: "one per line · Surname, Given, dates" },
      { key: "publisher", ia: "publisher", hint: "Place : Publisher" },
      { key: "date", ia: "date", hint: "YYYY" },
      { key: "language", ia: "language", hint: "MARC codes: slo, cze, ger" },
      { key: "edition", ia: "edition" },
      { key: "volume", ia: "volume" },
      { key: "subject", ia: "subject", hint: "one per line" },
      { key: "description", ia: "description", wide: true },
    ],
  ],
  [
    "Identifiers",
    [
      { key: "isbn", ia: "isbn" },
      { key: "oclc_id", ia: "oclc-id" },
      { key: "lccn", ia: "lccn" },
      { key: "call_number", ia: "call_number" },
      { key: "external_identifier", ia: "external-identifier", wide: true },
      { key: "collection", ia: "collection", hint: "first = primary" },
    ],
  ],
  [
    "Rights",
    [
      { key: "rights", ia: "rights", wide: true },
      { key: "licenseurl", ia: "licenseurl" },
      { key: "possible_copyright_status", ia: "possible-copyright-status" },
      { key: "copyright_region", ia: "copyright-region" },
      { key: "contributor", ia: "contributor" },
    ],
  ],
  [
    "Capture",
    [
      { key: "scanner", ia: "scanner" },
      { key: "scanningcenter", ia: "scanningcenter" },
      { key: "scandate", ia: "scandate", hint: "YYYYMMDDHHMMSS UTC" },
      { key: "ppi", ia: "ppi" },
      { key: "imagecount", ia: "imagecount", readOnly: true },
      { key: "ocr", ia: "ocr", readOnly: true },
      { key: "page_progression", ia: "page-progression", hint: "lr | rl" },
      { key: "notes", ia: "notes", wide: true },
    ],
  ],
];

type Draft = Record<string, string>;

function toDraft(m: ItemMetadata): Draft {
  const out: Draft = {};
  for (const [, fields] of GROUPS)
    for (const f of fields) {
      const v = m[f.key];
      out[f.key] = Array.isArray(v) ? v.join("\n") : v === null || v === undefined ? "" : String(v);
    }
  return out;
}

function parse(key: Key, raw: string): ItemMetadata[Key] {
  if (LIST_FIELDS.has(key)) return raw.split("\n").map((x) => x.trim()).filter(Boolean) as never;
  if (NUMBER_FIELDS.has(key)) return (raw.trim() ? Number(raw) : null) as never;
  return raw as never;
}

/** Only the fields whose parsed value differs from the server's. */
export function metadataPatch(original: ItemMetadata, draft: Draft): Partial<ItemMetadata> {
  const patch: Partial<Record<Key, unknown>> = {};
  for (const [key, raw] of Object.entries(draft) as [Key, string][]) {
    const value = parse(key, raw);
    if (JSON.stringify(value) !== JSON.stringify(original[key] ?? (LIST_FIELDS.has(key) ? [] : ""))) patch[key] = value;
  }
  return patch as Partial<ItemMetadata>;
}

export function MetadataForm({ bookId, metadata }: { bookId: number; metadata: ItemMetadata }) {
  const [draft, setDraft] = useState<Draft>(() => toDraft(metadata));
  const [lookupMsg, setLookupMsg] = useState("");
  const [lookupBusy, setLookupBusy] = useState(false);
  const save = usePatchMetadata(bookId);
  useEffect(() => setDraft(toDraft(metadata)), [metadata]);
  const patch = useMemo(() => metadataPatch(metadata, draft), [metadata, draft]);
  const dirty = Object.keys(patch).length > 0;

  const lookup = async () => {
    setLookupBusy(true);
    setLookupMsg("");
    try {
      const found = await api.lookupMetadata(bookId);
      const filled = toDraft({ ...metadata, ...found } as ItemMetadata);
      const next = { ...draft };
      let n = 0;
      for (const [k, v] of Object.entries(filled)) {
        if (!next[k] && v) {
          next[k] = v;
          n += 1;
        }
      }
      setDraft(next);
      setLookupMsg(n ? `OPAC filled ${n} empty field(s); review and save.` : "OPAC had nothing new.");
    } catch (e) {
      setLookupMsg(e instanceof ApiError && e.status === 404 ? "OPAC lookup is not available on this server." : String(e));
    } finally {
      setLookupBusy(false);
    }
  };

  return (
    <Panel
      title="Item metadata"
      actions={
        <>
          <Button size="small" onClick={lookup} disabled={lookupBusy}>
            OPAC lookup
          </Button>
          <Button size="small" disabled={!dirty} variant="ghost" onClick={() => setDraft(toDraft(metadata))}>
            Revert
          </Button>
          <Button size="small" variant="primary" disabled={!dirty || save.isPending} onClick={() => save.mutate(patch)}>
            Save{dirty ? ` (${Object.keys(patch).length})` : ""}
          </Button>
        </>
      }
    >
      {lookupMsg && <div className={s.hint} style={{ marginBottom: 8 }}>{lookupMsg}</div>}
      <ErrorNote error={save.error} />
      {GROUPS.map(([group, fields]) => (
        <fieldset key={group} style={{ border: 0, padding: 0, margin: "0 0 16px" }}>
          <legend className="label" style={{ color: "var(--text)", marginBottom: 8 }}>
            {group}
          </legend>
          <div className={s.formGrid}>
            {fields.map((f) => {
              const id = `md-${f.key}`;
              const multiline = LIST_FIELDS.has(f.key) || f.key === "description";
              const props = {
                id,
                value: draft[f.key] ?? "",
                readOnly: f.readOnly,
                onChange: (e: { target: { value: string } }) => setDraft({ ...draft, [f.key]: e.target.value }),
              };
              return (
                <div key={f.key} className={f.wide ? s.wide : undefined}>
                  <label className="label" htmlFor={id}>
                    {f.ia}
                  </label>
                  {multiline ? (
                    <textarea className="textarea" rows={Math.max(2, (draft[f.key] ?? "").split("\n").length)} {...props} />
                  ) : (
                    <input className="input" {...props} />
                  )}
                  {f.hint && <div className={s.hint}>{f.hint}</div>}
                </div>
              );
            })}
          </div>
        </fieldset>
      ))}
    </Panel>
  );
}
