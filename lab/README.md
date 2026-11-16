# lab/

Notebooks and experiments: not part of the image (`.dockerignore`), not tested by CI.

* `notebooks/` drove the **batch pipeline** (ScanTailor, Tesseract, recode_pdf, DocRes, the
  covers, the reflow EPUB) that ran in this repository until evilflowers-books-digitalizer#6.
  That code is gone; the work it did now runs on evilflowers-scan-service,
  evilflowers-ocr-service, evilflowers-analyzer-service, evilflowers-equation-service and
  evilflowers-render-service. To run a notebook as it was written, check out the last commit
  with the pipeline:

  ```bash
  git worktree add ../digitalizer-batch c94a858
  cd ../digitalizer-batch && poetry install --with notebooks
  ```

* `experiments/reflow/` is the reflow-edition experiment (Tesseract + PaddleOCR-VL to EPUB);
  its findings went into render-service's EPUB.

The training export of labelled regions stays a studio command:
`evilflowers-digitalizer studio export-training <project>`.
