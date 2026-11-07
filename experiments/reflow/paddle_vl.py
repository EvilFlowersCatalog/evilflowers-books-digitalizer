"""PaddleOCR-VL-1.6 element recognizer (transformers, MPS/CPU).

Usage as a script: paddle_vl.py jobs.json out.json
jobs.json = [{"id": str, "image": path, "bbox": [x0,y0,x1,y1] | null, "task": "ocr"|"formula"|"table", "pad": int}]
"""
from __future__ import annotations

import gc
import json
import sys
import time
from pathlib import Path

import torch
from PIL import Image
from transformers import AutoModelForImageTextToText, AutoProcessor

MODEL = Path.home() / ".local/share/evilflowers-tools/models/PaddleOCR-VL-1.6"
PROMPTS = {"ocr": "OCR:", "table": "Table Recognition:", "formula": "Formula Recognition:"}
DEVICE = "mps" if torch.backends.mps.is_available() else "cpu"


class PaddleVL:
    def __init__(self) -> None:
        self.model = AutoModelForImageTextToText.from_pretrained(
            MODEL, dtype=torch.bfloat16).to(DEVICE).eval()
        self.proc = AutoProcessor.from_pretrained(MODEL)

    def _min_pixels(self) -> int:
        ip = self.proc.image_processor
        size = getattr(ip, "size", None) or {}
        return getattr(ip, "min_pixels", None) or size.get("shortest_edge") or 147384

    @torch.inference_mode()
    def __call__(self, image: Image.Image, task: str = "ocr", max_new_tokens: int = 1024) -> str:
        msgs = [{"role": "user", "content": [
            {"type": "image", "image": image}, {"type": "text", "text": PROMPTS[task]}]}]
        inputs = self.proc.apply_chat_template(
            msgs, add_generation_prompt=True, tokenize=True, return_dict=True,
            return_tensors="pt",
        ).to(DEVICE)
        out = self.model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False,
                                  repetition_penalty=1.05, no_repeat_ngram_size=0)
        return self.proc.decode(out[0][inputs["input_ids"].shape[-1]:-1])


MAX_PIXELS = 1280 * 28 * 28
MAX_ASPECT = 10.0


def crop(path: str, bbox: list[int] | None, pad: int) -> Image.Image:
    im = Image.open(path).convert("RGB")
    if bbox:
        x0, y0, x1, y1 = bbox
        im = im.crop((max(0, x0 - pad), max(0, y0 - pad),
                      min(im.width, x1 + pad), min(im.height, y1 + pad)))
    # white-pad extreme aspect ratios (a 3x140 px sliver would otherwise explode
    # under the processor's min-pixel upscaling) and keep at least 64 px per side
    w, h = im.size
    tw = max(w, 64, int(h / MAX_ASPECT))
    th = max(h, 64, int(w / MAX_ASPECT))
    if (tw, th) != (w, h):
        canvas = Image.new("RGB", (tw, th), "white")
        canvas.paste(im, ((tw - w) // 2, (th - h) // 2))
        im = canvas
    if im.width * im.height > MAX_PIXELS:
        s = (MAX_PIXELS / (im.width * im.height)) ** 0.5
        im = im.resize((int(im.width * s), int(im.height * s)), Image.LANCZOS)
    return im


def main() -> None:
    jobs = json.loads(Path(sys.argv[1]).read_text())
    out_path = Path(sys.argv[2])
    results = json.loads(out_path.read_text()) if out_path.exists() else {}
    t0 = time.time()
    vl = PaddleVL()
    print(f"model loaded on {DEVICE} in {time.time() - t0:.1f}s", flush=True)
    for job in jobs:
        if job["id"] in results:
            continue
        t = time.time()
        try:
            text = vl(crop(job["image"], job.get("bbox"), job.get("pad", 12)), job.get("task", "ocr"))
            err = None
        except RuntimeError as e:          # MPS OOM on a pathological crop: record, continue
            text, err = "", str(e)[:200]
        finally:
            gc.collect()
            if DEVICE == "mps":
                torch.mps.empty_cache()
        results[job["id"]] = {"text": text, "secs": round(time.time() - t, 2), "task": job.get("task", "ocr"),
                              **({"error": err} if err else {})}
        print(f"{job['id']}: {results[job['id']]['secs']}s", flush=True)
        out_path.write_text(json.dumps(results, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
