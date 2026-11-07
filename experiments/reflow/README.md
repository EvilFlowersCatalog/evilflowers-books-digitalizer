# Reflow edition experiment (2026-11-06)

Rebuild a scanned book as a born-digital edition: reflowable EPUB 3 (MathML) and a
re-typeset LaTeX PDF. Findings and numbers: [`docs/reflow_edition.md`](../../docs/reflow_edition.md).

Experiment code, not production. Nothing here is imported by the package.

## Files

| File | Role |
|---|---|
| `prep_book.py` | Extract every page scan from a cached output PDF (native stream, no re-encode) and run Tesseract `slk+eng` to per-page hOCR |
| `hocr_blocks.py` | hOCR model: page → paragraph → line → word, with confidence; `parse_pages()` handles the pipeline's multi-page `book.hocr` |
| `analyze.py` | Page image + hOCR → semantic blocks (heading, para, list/olist, table, formula, figure, caption). Routing, figure, stamp and facing-page guards |
| `paddle_vl.py` | PaddleOCR-VL-1.6 element recognizer (transformers, MPS/CPU). Runs in the tool venv as a sidecar, with an incremental JSON cache |
| `build.py` | Driver: analysis → VLM jobs → anchoring + dictionary arbitration → LaTeX validation → Markdown → pandoc EPUB + xelatex PDF |
| `evaluate.py` | Diacritic and foreign-script counts: raw VLM vs anchored vs Tesseract on inline segments |
| `formula_sheet.py` | Visual QA: scan crop next to the re-rendered VLM LaTeX |
| `vector_probe.py` | ClearScan-style probe: potrace the 600 dpi JBIG2 text mask into vector outlines |
| `yolo_layout.py` | DocLayout-YOLO (DocLayNet) region probe, borrowed from the YoGaDoc idea. **Not run**: blocked pending a decision on loading pickled `.pt` weights |
| `fei_9788022726634.meta.json` | EPUB metadata for the test book (Fyzika po kapitolách 1: Vektory) |

## Tools (host-local)

- PaddleOCR-VL-1.6 weights (Apache-2.0, 1.9 GB safetensors):
  `~/.local/share/evilflowers-tools/models/PaddleOCR-VL-1.6`. Install with
  `hf download PaddlePaddle/PaddleOCR-VL-1.6 --local-dir …`.
- Its Python is `~/.local/share/evilflowers-tools/venv-docres/bin/python`, which already has
  torch 2.12. Add `transformers>=5`.
- Slovak hunspell: `~/Library/Spelling/sk_SK.{aff,dic}` plus `hunspell` from brew.
- `pandoc`, `xelatex` (TeX Live basic plus polyglossia), `potrace`, Java plus epubcheck 5.4.0.

## Reproduce

Run from this directory with the project venv.

```sh
python prep_book.py ../../output/fei/fei_CVI_OPACID_FEI_9788022726634.pdf /tmp/book_fei
python build.py /tmp/book_fei fei_9788022726634.meta.json --no-vlm --out out_novlm   # variant B
python build.py /tmp/book_fei fei_9788022726634.meta.json --out out_hybrid           # variant C
python evaluate.py /tmp/book_fei/out_hybrid
```
