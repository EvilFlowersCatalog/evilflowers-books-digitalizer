"""ClearScan-style probe: trace the 600 dpi JBIG2 text mask with potrace -> vector PDF page.

Compares a zoomed crop: raster MRC (current) vs vector outline text, and file size.
"""
import subprocess
from pathlib import Path

import cv2
import numpy as np

D = Path(__file__).parent
mask = cv2.imread(str(D / "fad20-002.png"), cv2.IMREAD_GRAYSCALE)
ink_is_white = (mask > 127).mean() < 0.5
ink = (mask > 127) if ink_is_white else (mask <= 127)
print("mask", mask.shape, "ink fraction", round(float(ink.mean()), 4))
pbm = D / "mask.pbm"
cv2.imwrite(str(pbm), np.where(ink, 0, 255).astype(np.uint8))

W_PT, H_PT = 558.48, 764.88  # same MediaBox as the distribution PDF
variants = {
    "potrace_default": [],
    "potrace_smooth": ["-a", "1.2", "-O", "0.5", "-t", "3"],
}
for name, extra in variants.items():
    out = D / f"{name}.pdf"
    subprocess.run(["potrace", "-b", "pdf", "--flat", "-W", f"{W_PT}pt", "-H", f"{H_PT}pt",
                    *extra, str(pbm), "-o", str(out)], check=True)
    print(name, f"{out.stat().st_size / 1024:.0f} KiB")
