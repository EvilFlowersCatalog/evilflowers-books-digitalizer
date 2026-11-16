"""Extract every page scan of a cached PDF (native stream, no re-encode) + Tesseract hOCR.

usage: prep_book.py <pdf> <outdir>
"""
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import fitz

pdf, out = Path(sys.argv[1]), Path(sys.argv[2])
(out / "pages").mkdir(parents=True, exist_ok=True)
(out / "hocr").mkdir(exist_ok=True)
doc = fitz.open(pdf)
pages = []
for i, page in enumerate(doc, start=1):
    xref = max(page.get_images(full=True), key=lambda im: im[2] * im[3])[0]
    info = doc.extract_image(xref)
    path = out / "pages" / f"p{i:03d}.{info['ext']}"
    path.write_bytes(info["image"])
    pages.append(path)
print(f"extracted {len(pages)} pages")


def ocr(path: Path) -> None:
    base = out / "hocr" / path.stem
    if base.with_suffix(".hocr").exists():
        return
    subprocess.run(["tesseract", str(path), str(base), "-l", "slk+eng", "--dpi", "300", "hocr"],
                   check=True, capture_output=True, env={"OMP_THREAD_LIMIT": "1", "PATH": "/opt/homebrew/bin:/usr/bin"})


with ThreadPoolExecutor(8) as ex:
    list(ex.map(ocr, pages))
print("ocr done")
