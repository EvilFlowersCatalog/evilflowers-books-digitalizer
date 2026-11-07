/**
 * Page properties tab: IA leaf fields (page type, printed number, include in
 * access) and the page override (crop, deskew, split, despeckle, colour mode, OCR
 * language). OpenCV.js previews crop / deskew / threshold before the re-run.
 */

import { useEffect, useRef, useState } from "react";
import { media } from "../../api/media";
import { NOT_IN_ACCESS, PAGE_TYPES, type BBoxList, type PageDetail, type PageOverride, type PagePatch, type PageType } from "../../api/types";
import { Button, ErrorNote } from "../../components/ui";
import { loadOpenCv, renderPreview, type CvBox } from "./opencv";
import s from "./studio.module.css";

interface Props {
  detail: PageDetail;
  saving: boolean;
  error: unknown;
  onSave: (patch: PagePatch, rerun: boolean) => void;
}

interface Draft {
  page_type: PageType;
  page_number: string;
  include: boolean;
  crop: [string, string, string, string];
  deskew: number;
  split_x: string;
  despeckle: string;
  color_mode: string;
  ocr_language: string;
  threshold: number | "otsu";
}

function initial(d: PageDetail): Draft {
  const o = d.override;
  const crop = o.crop ?? null;
  const t = o.extra?.threshold;
  return {
    page_type: d.page.page_type,
    page_number: d.page.page_number ?? "",
    include: d.page.add_to_access,
    crop: crop ? (crop.map(String) as Draft["crop"]) : ["", "", "", ""],
    deskew: o.deskew_deg ?? 0,
    split_x: o.split_x !== undefined ? String(o.split_x) : "",
    despeckle: o.despeckle ?? "",
    color_mode: o.color_mode ?? "",
    ocr_language: o.ocr_language ?? "",
    threshold: typeof t === "number" ? t : "otsu",
  };
}

/** Draft -> PageOverride (only what the operator set; empty = automatic). */
export function overrideFromDraft(d: Draft, extra: Record<string, unknown> | undefined): PageOverride {
  const out: PageOverride = {};
  const crop = d.crop.map((v) => (v.trim() === "" ? NaN : Number(v)));
  if (crop.every(Number.isFinite)) out.crop = crop as BBoxList;
  if (d.deskew) out.deskew_deg = Math.round(d.deskew * 100) / 100;
  if (d.split_x.trim()) out.split_x = Number(d.split_x);
  if (d.despeckle) out.despeckle = d.despeckle as NonNullable<PageOverride["despeckle"]>;
  if (d.color_mode) out.color_mode = d.color_mode as NonNullable<PageOverride["color_mode"]>;
  if (d.ocr_language.trim()) out.ocr_language = d.ocr_language.trim();
  const rest = { ...(extra ?? {}) };
  delete rest.threshold;
  if (d.color_mode === "black_and_white" && d.threshold !== "otsu") rest.threshold = d.threshold;
  if (Object.keys(rest).length) out.extra = rest;
  return out;
}

function Preview({ detail, draft }: { detail: PageDetail; draft: Draft }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [img, setImg] = useState<HTMLImageElement | null>(null);
  const [state, setState] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [message, setMessage] = useState("");
  const [cv, setCv] = useState<CvBox | null>(null);

  const start = async () => {
    setState("loading");
    try {
      const image = new Image();
      image.crossOrigin = "anonymous";
      image.src = media.pageImage(detail.page.id, "original", 1400, detail.page.version);
      await image.decode();
      const loaded = await loadOpenCv();
      setImg(image);
      setCv(loaded);
      setState("ready");
    } catch (e) {
      setState("error");
      setMessage(e instanceof Error ? e.message : String(e));
    }
  };

  useEffect(() => {
    if (state !== "ready" || !cv || !img || !canvas.current) return;
    const f = detail.frame;
    const halfX0 = f.x0 ?? 0;
    const halfW = (f.x1 ?? f.width ?? img.naturalWidth) - halfX0 || img.naturalWidth;
    const scale = img.naturalWidth / halfW;
    const c = draft.crop.map(Number);
    const crop = draft.crop.every((v) => v.trim() !== "") && c.every(Number.isFinite)
      ? ([(c[0]! - halfX0) * scale, c[1]! * scale, (c[2]! - halfX0) * scale, c[3]! * scale] as [number, number, number, number])
      : null;
    try {
      const used = renderPreview(cv.cv, img, canvas.current, {
        crop,
        deskewDeg: draft.deskew,
        threshold: draft.color_mode === "black_and_white" ? draft.threshold : null,
      });
      setMessage(used !== null ? `threshold ${Math.round(used)}` : "");
    } catch (e) {
      setMessage(`preview failed: ${String(e)}`);
    }
  }, [state, cv, img, draft, detail.frame]);

  return (
    <div className={s.previewBox}>
      {state === "ready" ? <canvas ref={canvas} /> : null}
      {state === "idle" && (
        <div className={s.previewMsg}>
          <Button size="small" onClick={start}>Live preview (OpenCV)</Button>
          <div style={{ marginTop: 6 }}>Downloads ~10 MB once; applies deskew → crop → threshold instantly.</div>
        </div>
      )}
      {state === "loading" && <div className={s.previewMsg}>Loading OpenCV…</div>}
      {state === "error" && <div className={s.previewMsg}>Preview unavailable: {message}</div>}
      {state === "ready" && message && <span className={s.paneLabel}>{message}</span>}
    </div>
  );
}

export function PropertiesPanel({ detail, saving, error, onSave }: Props) {
  const [draft, setDraft] = useState(() => initial(detail));
  // Reset only when the saved leaf changes, not on every refetch (region edits
  // refetch the same PageDetail and must not wipe a half-typed override).
  const savedKey = JSON.stringify([detail.page.id, detail.page.version, detail.page.page_type, detail.page.page_number, detail.page.add_to_access, detail.override]);
  useEffect(() => setDraft(initial(detail)), [savedKey]); // `detail` is represented by savedKey
  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setDraft((d) => ({ ...d, [k]: v }));
  const patch = (): PagePatch => ({
    page_type: draft.page_type,
    page_number: draft.page_number.trim() || null,
    include_in_access: draft.include,
    override: overrideFromDraft(draft, detail.override.extra),
  });
  const applyDetectedCrop = () => {
    const box = detail.frame.crop_box;
    if (box) set("crop", [box[0], box[1], box[0] + box[2], box[1] + box[3]].map(String) as Draft["crop"]);
  };

  return (
    <form onSubmit={(e) => { e.preventDefault(); onSave(patch(), false); }}>
      <div className={s.section}>
        <div className={s.sectionTitle}>Leaf (IA scandata)</div>
        <div className={s.editorGrid}>
          <div>
            <label className="label" htmlFor="pp-type">pageType</label>
            <select
              id="pp-type"
              className="select"
              value={draft.page_type}
              onChange={(e) => {
                const t = e.target.value as PageType;
                setDraft((d) => ({ ...d, page_type: t, include: !NOT_IN_ACCESS.has(t) }));
              }}
            >
              {PAGE_TYPES.map((t) => <option key={t}>{t}</option>)}
            </select>
          </div>
          <div>
            <label className="label" htmlFor="pp-number">Printed page number</label>
            <input id="pp-number" className="input" value={draft.page_number} placeholder="unnumbered" onChange={(e) => set("page_number", e.target.value)} />
          </div>
        </div>
        <label className={s.inline}>
          <input type="checkbox" checked={draft.include} onChange={(e) => set("include", e.target.checked)} />
          Include in access formats (addToAccessFormats)
        </label>
      </div>

      <div className={s.section}>
        <div className={s.sectionTitle}>Overrides (original frame px)</div>
        <div className={s.field}>
          <label className="label">Crop x0 · y0 · x1 · y1</label>
          <div className={s.inline}>
            {draft.crop.map((v, i) => (
              <input key={i} className="input" inputMode="numeric" value={v} placeholder="auto" aria-label={["x0", "y0", "x1", "y1"][i]} onChange={(e) => { const c = [...draft.crop] as Draft["crop"]; c[i] = e.target.value; set("crop", c); }} />
            ))}
          </div>
          <div className={s.inline} style={{ marginTop: 4 }}>
            <Button size="small" variant="ghost" onClick={applyDetectedCrop} disabled={!detail.frame.crop_box}>Use detected crop</Button>
            <Button size="small" variant="ghost" onClick={() => set("crop", ["", "", "", ""])}>Auto</Button>
          </div>
        </div>
        <div className={s.field}>
          <label className="label" htmlFor="pp-deskew">Deskew {draft.deskew.toFixed(2)}° {detail.page.skew_angle != null && <span className="faint">(detected {detail.page.skew_angle.toFixed(2)}°)</span>}</label>
          <div className={s.inline}>
            <input id="pp-deskew" className={s.slider} type="range" min={-5} max={5} step={0.05} value={draft.deskew} onChange={(e) => set("deskew", Number(e.target.value))} />
            <Button size="small" variant="ghost" onClick={() => set("deskew", 0)}>0</Button>
          </div>
        </div>
        <div className={s.editorGrid}>
          <div>
            <label className="label" htmlFor="pp-split">Split x (gutter)</label>
            <input id="pp-split" className="input" inputMode="numeric" value={draft.split_x} placeholder="auto" onChange={(e) => set("split_x", e.target.value)} />
          </div>
          <div>
            <label className="label" htmlFor="pp-despeckle">Despeckle</label>
            <select id="pp-despeckle" className="select" value={draft.despeckle} onChange={(e) => set("despeckle", e.target.value)}>
              <option value="">auto</option>
              {["off", "cautious", "normal", "aggressive"].map((v) => <option key={v}>{v}</option>)}
            </select>
          </div>
          <div>
            <label className="label" htmlFor="pp-color">Colour mode</label>
            <select id="pp-color" className="select" value={draft.color_mode} onChange={(e) => set("color_mode", e.target.value)}>
              <option value="">auto</option>
              {["mixed", "color_grayscale", "black_and_white"].map((v) => <option key={v}>{v}</option>)}
            </select>
          </div>
          <div>
            <label className="label" htmlFor="pp-lang">OCR language</label>
            <input id="pp-lang" className="input" value={draft.ocr_language} placeholder="slk+eng" onChange={(e) => set("ocr_language", e.target.value)} />
          </div>
        </div>
        {draft.color_mode === "black_and_white" && (
          <div className={s.field}>
            <label className="label" htmlFor="pp-threshold">Threshold {draft.threshold === "otsu" ? "(Otsu, automatic)" : draft.threshold}</label>
            <div className={s.inline}>
              <input id="pp-threshold" className={s.slider} type="range" min={0} max={255} value={draft.threshold === "otsu" ? 128 : draft.threshold} onChange={(e) => set("threshold", Number(e.target.value))} />
              <Button size="small" variant="ghost" onClick={() => set("threshold", "otsu")}>Auto</Button>
            </div>
          </div>
        )}
        <Preview detail={detail} draft={draft} />
      </div>
      <ErrorNote error={error} />
      <div className={s.inline}>
        <Button type="submit" disabled={saving}>Save</Button>
        <Button variant="primary" disabled={saving} onClick={() => onSave(patch(), true)}>Save &amp; re-run page</Button>
        <Button variant="ghost" onClick={() => setDraft(initial(detail))}>Revert</Button>
      </div>
    </form>
  );
}
