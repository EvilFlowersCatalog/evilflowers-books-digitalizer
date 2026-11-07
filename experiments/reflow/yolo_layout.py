"""DocLayout-YOLO (DocLayNet classes) layout regions for page images -> JSON + overlays.

usage: yolo_layout.py <pages_dir> <out_json> [page stems...]
"""
import json
import sys
import time
from pathlib import Path

from doclayout_yolo import YOLOv10
from PIL import Image, ImageDraw

WEIGHTS = Path.home() / ".local/share/evilflowers-tools/models/doclayout-yolo/doclayout_yolo_doclaynet_imgsz1120_docsynth_pretrain.pt"
COL = {"Picture": "blue", "Formula": "red", "Table": "magenta", "Caption": "orange", "Text": "green",
       "Section-header": "purple", "List-item": "teal", "Page-header": "gray", "Page-footer": "gray",
       "Footnote": "brown", "Title": "black"}

pages_dir, out_json = Path(sys.argv[1]), Path(sys.argv[2])
stems = sys.argv[3:]
paths = [p for p in sorted(pages_dir.iterdir()) if not stems or p.stem in stems]
model = YOLOv10(str(WEIGHTS))
result = {}
t0 = time.time()
for p in paths:
    r = model.predict(str(p), imgsz=1120, conf=0.25, device="cpu", verbose=False)[0]
    regs = []
    for box in r.boxes:
        x0, y0, x1, y1 = (int(v) for v in box.xyxy[0].tolist())
        regs.append({"cls": r.names[int(box.cls[0])], "conf": round(float(box.conf[0]), 3),
                     "bbox": [x0, y0, x1, y1]})
    result[p.stem] = regs
    if stems:
        im = Image.open(p).convert("RGB")
        d = ImageDraw.Draw(im)
        for g in regs:
            d.rectangle(g["bbox"], outline=COL.get(g["cls"], "black"), width=6)
            d.text((g["bbox"][0] + 5, g["bbox"][1] + 3), f"{g['cls']} {g['conf']:.2f}", fill=COL.get(g["cls"], "black"))
        im.thumbnail((800, 1100))
        im.save(out_json.parent / f"yolo_{p.stem}.png")
print(f"{len(paths)} pages in {time.time() - t0:.1f}s")
out_json.write_text(json.dumps(result, indent=0))
