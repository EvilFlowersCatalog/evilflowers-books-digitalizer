/**
 * Text & math tab: edit the OCR text of the selected region; formulas are LaTeX
 * with a live KaTeX preview (HTML + MathML, the same MathML the EPUB carries).
 */

import katex from "katex";
import "katex/dist/katex.min.css";
import { useEffect, useMemo, useState } from "react";
import type { PageDetail, RegionOut } from "../../api/types";
import { Button, Empty } from "../../components/ui";
import { KIND_LABEL } from "../../lib/regions";
import s from "./studio.module.css";

export function renderLatex(src: string): { html: string; error: string | null } {
  try {
    return { html: katex.renderToString(src, { displayMode: true, throwOnError: true, output: "htmlAndMathml", strict: "ignore" }), error: null };
  } catch (e) {
    const html = katex.renderToString(src, { displayMode: true, throwOnError: false, output: "htmlAndMathml", strict: "ignore" });
    return { html, error: e instanceof Error ? e.message : String(e) };
  }
}

function MathPreview({ latex }: { latex: string }) {
  const { html, error } = useMemo(() => renderLatex(latex), [latex]);
  return (
    <>
      <div className={s.math} dangerouslySetInnerHTML={{ __html: html }} />
      {error && <div className={s.mathError}>{error}</div>}
    </>
  );
}

function RegionText({ region, onSave }: { region: RegionOut; onSave: (text: string) => void }) {
  const [text, setText] = useState(region.text ?? "");
  useEffect(() => setText(region.text ?? ""), [region.id, region.text]);
  const isFormula = region.kind === "formula";
  const dirty = text !== (region.text ?? "");
  return (
    <div className={s.section}>
      <div className={s.sectionTitle}>
        {KIND_LABEL[region.kind]} #{region.id} {isFormula ? "· LaTeX" : "· OCR text"}
      </div>
      <textarea
        className="textarea mono"
        rows={isFormula ? 4 : 8}
        value={text}
        spellCheck={!isFormula}
        lang={isFormula ? undefined : "sk"}
        onChange={(e) => setText(e.target.value)}
        aria-label={isFormula ? "LaTeX" : "OCR text"}
      />
      {isFormula && (
        <div style={{ marginTop: 8 }}>
          <MathPreview latex={text} />
        </div>
      )}
      <div className={s.inline} style={{ marginTop: 8 }}>
        <Button size="small" variant="primary" disabled={!dirty} onClick={() => onSave(text)}>
          Save text
        </Button>
        <Button size="small" variant="ghost" disabled={!dirty} onClick={() => setText(region.text ?? "")}>
          Revert
        </Button>
      </div>
    </div>
  );
}

export function TextPanel({ detail, selected, onSave }: { detail: PageDetail; selected: RegionOut | undefined; onSave: (id: number, text: string) => void }) {
  return (
    <>
      {selected ? (
        <RegionText region={selected} onSave={(t) => onSave(selected.id, t)} />
      ) : (
        <Empty>Select a region to edit its text or LaTeX.</Empty>
      )}
      <div className={s.section}>
        <div className={s.sectionTitle}>
          Page OCR {detail.page.ocr_conf != null && <span className="faint">· conf {detail.page.ocr_conf.toFixed(1)}</span>}
        </div>
        <div className={s.ocr} lang="sk">{detail.ocr_text || <span className="faint">no OCR text yet</span>}</div>
      </div>
    </>
  );
}
