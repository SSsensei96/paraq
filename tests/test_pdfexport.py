"""Translated-PDF export: prose replaced in place, table cells one by one, rules and numbers kept."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pymupdf

from paraq import pdfexport as X
from paraq.pdfutil import page_layout
from paraq.translate import paragraph_language

SANS = str(Path(__file__).resolve().parents[1] / "paraq" / "resources" / "fonts" / "DejaVuSans.ttf")
PARA = (
    "Резкое сокращение основных продуктов шло по всем наименованиям, в частности, по Казахстану "
    "произошло сокращение поступления сахара в восемь раз. Цены на колхозных рынках превысили "
    "довоенный уровень во много раз, и семьи едва сводили концы с концами."
)
EN = (
    "Supplies of all basic foodstuffs fell sharply; in Kazakhstan, deliveries of sugar fell eightfold. "
    "Prices at collective-farm markets far exceeded the pre-war level, and families barely made ends meet."
)
CELLS = {"Год": "Year", "Мясо": "Meat", "Молоко": "Milk", "Производство продуктов в СССР": "Food production in the USSR"}


def make_page(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_font(fontname="F0", fontfile=SANS)
    page.insert_textbox(pymupdf.Rect(70, 80, 525, 200), PARA, fontname="F0", fontsize=11, align=3)
    page.insert_text((200, 250), "Производство продуктов в СССР", fontname="F0", fontsize=10)
    rows = [("Год", "1940", "1942", "1943"), ("Мясо", "4 695", "1 841", "1 767"), ("Молоко", "33 640", "15 762", "16 391")]
    x = [70, 200, 300, 400, 500]
    for r, row in enumerate(rows):
        y = 265 + 22 * r
        for c, cell in enumerate(row):
            page.insert_text((x[c] + 6, y + 15), cell, fontname="F0", fontsize=10)
        page.draw_line((x[0], y), (x[-1], y))
    page.draw_line((x[0], 265 + 66), (x[-1], 265 + 66))
    for xx in x:
        page.draw_line((xx, 265), (xx, 331))
    doc.save(str(path))


def test_translated_pdf():
    tmp = Path(tempfile.mkdtemp(prefix="paraq_export_"))
    src = tmp / "table.pdf"
    make_page(src)
    page = pymupdf.open(str(src))[0]
    lay = page_layout(page)
    paras = [p for p in lay.paragraphs() if p.text.strip()]
    entries = [{"src": p.text, "en": EN if p.text.startswith("Резкое") else p.text,
                "lang": paragraph_language(p.text) if p.text.startswith("Резкое") else None} for p in paras]
    kinds = {p.text[:12]: X.classify(p, X._paragraph_lines(lay, p)) for p in paras}
    assert kinds[PARA[:12]] == "prose", kinds
    asked = []

    def runs(items):
        asked.extend(t for t, _l in items)
        return [CELLS.get(t, t) for t, _l in items]

    out = tmp / "table_en.pdf"
    res = X.export_translated_pdf(src, {"0": entries}, out, translate_runs=runs)
    assert res.translated_pages == 1 and res.replaced >= 4, res
    assert "1940" not in asked and not any(a.replace(" ", "").isdigit() for a in asked), asked  # numbers never sent
    new = pymupdf.open(str(out))[0]
    text = new.get_text()
    for word in ("Supplies of all basic foodstuffs", "Year", "Meat", "Milk", "Food production in the USSR"):
        assert word in " ".join(text.split()), (word, text)
    for word in ("Резкое", "Мясо", "Молоко"):
        assert word not in text, word
    for number in ("4 695", "33 640", "1940"):
        assert number in text, number
    assert len(new.get_drawings()) >= len(page.get_drawings()) - 0, "table rules lost"
    side = tmp / "side.pdf"
    X.export_translated_pdf(src, {"0": entries}, side, side_by_side=True, translate_runs=runs)
    sp = pymupdf.open(str(side))[0]
    assert abs(sp.rect.width - (2 * 595 + 18)) < 1 and "Резкое" in sp.get_text() and "Supplies" in sp.get_text()
    print("   translated PDF: prose and", len(asked), "cells replaced; numbers, rules and side-by-side OK")


if __name__ == "__main__":
    test_translated_pdf()
    print("PASS  test_translated_pdf")
