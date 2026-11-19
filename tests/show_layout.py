"""Print the detected blocks of every page of a PDF: python tests/show_layout.py file.pdf"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pymupdf
from paraq.pdfutil import page_layout

for path in sys.argv[1:]:
    doc = pymupdf.open(path)
    print("=====", Path(path).name)
    for pg in doc:
        lay = page_layout(pg)
        print(f"-- page {pg.number + 1}: {len(lay.blocks)} blocks, up to {lay.n_columns} columns")
        for b in lay.blocks:
            print(f"   [{b.kind:6}] {len(b.lines):3} lines  x {b.x0:5.0f}-{b.x1:5.0f}  y {b.y0:5.0f}-{b.y1:5.0f}  | {b.lines[0].text[:48]} ... {b.lines[-1].text[-28:]}")
