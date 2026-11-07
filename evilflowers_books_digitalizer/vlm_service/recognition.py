"""PaddleOCR-VL recognition core, shared by every way the model is run.

* ``reflow/vlm_worker.py`` — the subprocess sidecar in the tool venv;
* :mod:`.app` — the HTTP service (``python -m vlm_service`` in the ``vlm`` image);
* ``studio.adapters.formula.http`` — the client, which crops with :func:`crop` so the
  service receives exactly what the sidecar would have fed the model.

Only stdlib + Pillow at import time: torch / transformers are imported inside
:func:`load_model`, so the main package (and its tests) never need them. This
module must not import anything from ``evilflowers_books_digitalizer``: the
worker script and the ``vlm`` image load it as the top-level package
``vlm_service``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

PROMPTS = {"ocr": "OCR:", "formula": "Formula Recognition:", "table": "Table Recognition:"}
MAX_PIXELS = 1280 * 28 * 28
MAX_ASPECT = 10.0
MAX_NEW_TOKENS = 512
PAD = 10
Image.MAX_IMAGE_PIXELS = None


def normalize(im: Image.Image) -> Image.Image:
    """RGB, white-padded to a sane aspect ratio and size, downscaled to :data:`MAX_PIXELS`.

    A 3x140 px sliver explodes under the processor's min-pixel upscaling (and OOMs a
    GPU); an oversized crop costs quadratic attention time on CPU.
    """
    im = im.convert("RGB")
    w, h = im.size
    tw, th = max(w, 64, int(h / MAX_ASPECT)), max(h, 64, int(w / MAX_ASPECT))
    if (tw, th) != (w, h):
        canvas = Image.new("RGB", (tw, th), "white")
        canvas.paste(im, ((tw - w) // 2, (th - h) // 2))
        im = canvas
    if im.width * im.height > MAX_PIXELS:
        s = (MAX_PIXELS / (im.width * im.height)) ** 0.5
        im = im.resize((int(im.width * s), int(im.height * s)), Image.Resampling.LANCZOS)
    return im


def crop_from(src: Image.Image, bbox: tuple[int, int, int, int] | list[int]) -> Image.Image:
    """The region ``bbox`` of an open page, with :data:`PAD` px of context, normalized."""
    x0, y0, x1, y1 = bbox
    return normalize(src.crop((max(0, x0 - PAD), max(0, y0 - PAD),
                               min(src.width, x1 + PAD), min(src.height, y1 + PAD))))


def crop(path: str | Path, bbox: tuple[int, int, int, int] | list[int]) -> Image.Image:
    with Image.open(path) as src:
        return crop_from(src, bbox)


@dataclass
class LoadedModel:
    model: Any
    processor: Any
    device: str


def pick_device(device: str) -> str:
    import torch

    if device != "auto":
        return device
    if torch.cuda.is_available():
        return "cuda"
    return "mps" if torch.backends.mps.is_available() else "cpu"


def load_model(model_dir: str | Path, device: str = "auto", dtype: str = "auto",
               attn: str = "sdpa") -> LoadedModel:
    """Load weights (safetensors only) and processor; ``dtype`` auto = fp32 on CPU, bf16 else."""
    import torch
    from transformers import AutoModelForImageTextToText, AutoProcessor

    if not any(Path(model_dir).glob("*.safetensors")):
        raise FileNotFoundError(f"no .safetensors weights in {model_dir}")
    device = pick_device(device)
    if dtype == "auto":
        dtype = "float32" if device == "cpu" else "bfloat16"
    model = AutoModelForImageTextToText.from_pretrained(
        str(model_dir), dtype=getattr(torch, dtype), attn_implementation=attn,
        use_safetensors=True).to(device).eval()
    return LoadedModel(model, AutoProcessor.from_pretrained(str(model_dir)), device)


def generate(lm: LoadedModel, image: Image.Image, task: str, *, use_cache: bool | None = None) -> str:
    """One crop -> text. Greedy, 512 tokens max (runaway repetition is what OOMs)."""
    import torch

    msgs = [{"role": "user", "content": [
        {"type": "image", "image": image}, {"type": "text", "text": PROMPTS[task]}]}]
    inputs = lm.processor.apply_chat_template(msgs, add_generation_prompt=True, tokenize=True,
                                              return_dict=True, return_tensors="pt").to(lm.device)
    extra = {} if use_cache is None else {"use_cache": use_cache}
    with torch.inference_mode():
        out = lm.model.generate(**inputs, max_new_tokens=MAX_NEW_TOKENS, do_sample=False,
                                repetition_penalty=1.05, **extra)
    return str(lm.processor.decode(out[0][inputs["input_ids"].shape[-1]:-1]))


def free_memory(device: str) -> None:
    import gc

    import torch

    gc.collect()
    if device == "mps":
        torch.mps.empty_cache()
    elif device == "cuda":
        torch.cuda.empty_cache()
