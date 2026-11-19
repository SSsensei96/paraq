"""Engine tests. Run with:  python tests/test_engine.py   (or pytest tests/)

The OCR tests need Tesseract with rus, kaz, eng and tur language data.
"""

from __future__ import annotations

import math
import random
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pymupdf

import make_samples as ms
from paraq import ocr
from paraq.layout import Word, analyze, join_lines
from paraq.pdfutil import open_as_pdf, page_layout
from paraq.textquality import assess
from paraq.translate import detect_language, modernize_russian, split_sentences

_samples: Path | None = None


def samples() -> Path:
    global _samples
    if _samples is None:
        _samples = Path(tempfile.mkdtemp(prefix="paraq_test_"))
        ms.build(_samples)
    return _samples


def norm_words(text: str) -> list[str]:
    text = text.lower().replace("ё", "е")
    return re.findall(r"[^\W\d_]+", text)


def recall(truth: str, got: str) -> float:
    """Share of ground-truth words that also occur in the OCR output."""
    t, g = Counter(norm_words(truth)), Counter(norm_words(got))
    hit = sum(min(n, g[w]) for w, n in t.items())
    return hit / max(1, sum(t.values()))


# --------------------------------------------------------------- text quality

def test_quality_good_text():
    for paras in (ms.RU, ms.KK, ms.TR, ms.EN):
        assert assess(" ".join(paras)).status == "good"


def test_quality_damaged_text():
    ru = " ".join(ms.RU)
    assert assess(ms.mojibake(ru)).status == "damaged"  # wrong encoding
    assert assess(" ".join(ru.replace(" ", ""))).status == "damaged"  # letters split apart
    assert assess("".join(chr(0xE000 + (ord(c) % 200)) if c.isalpha() else c for c in ru)).status == "damaged"  # private-use glyphs
    assert assess(ru.replace("о", "�").replace("е", "�")).status == "damaged"  # replacement chars
    mixed = " ".join(w.replace("а", "a").replace("о", "o").replace("с", "c") for w in ru.split())
    assert assess(mixed).status == "damaged"  # Latin look-alikes inside Cyrillic words
    assert assess("бвгдж клмнп рстфх цчшщ бвгд жклм нпрс тфхц чшщб вгдж клмн прст " * 3).status == "damaged"
    assert assess("").status == "none"
    assert assess("12").status == "none"


# --------------------------------------------------------------------- layout

def make_column(x0, x1, y0, n_lines, rng, size=10.0, pitch=14.0, indent_every=6, last_short=True):
    """Justified lines of random words filling [x0, x1], with word gaps of 0.25 to about 1.5 em."""
    words = []
    for i in range(n_lines):
        lx0 = x0 + (size * 1.5 if i % indent_every == 0 else 0)
        avail = x1 - lx0
        while True:
            widths = []
            while sum(widths) + 0.3 * size * len(widths) < avail:
                widths.append(rng.uniform(1.0, 7.0) * size * 0.55)
            widths = widths[:-1]
            if len(widths) >= 2 and (avail - sum(widths)) / (len(widths) - 1) <= 1.5 * size:
                break  # a typesetter would hyphenate rather than leave wider holes
        short = last_short and i % indent_every == indent_every - 1
        gap = 0.3 * size if short else (avail - sum(widths)) / (len(widths) - 1)
        x = lx0
        y = y0 + i * pitch
        for w in widths:
            words.append(Word("w" * max(1, int(w / (size * 0.55))), x, y, x + w, y + size))
            x += w + gap
    return words


def test_layout_single_column_never_splits():
    rng = random.Random(1)
    for trial in range(300):
        width = rng.choice([150, 220, 300, 450])  # narrow measures give wide, river-prone gaps
        words = make_column(60, 60 + width, 80, rng.randint(12, 45), rng)
        lay = analyze(words, 595, 842)
        assert len(lay.blocks) == 1, f"trial {trial}: single column split into {len(lay.blocks)} blocks"


def test_layout_two_columns_heading_footnote():
    rng = random.Random(2)
    for gutter in (12, 18, 30):
        for aligned in (True, False):
            head = [Word("Heading", 180, 50, 294, 66), Word("across", 300, 50, 420, 66)]
            mid = 297.5
            left = make_column(60, mid - gutter / 2, 90, 40, rng)
            right = make_column(mid + gutter / 2, 535, 90 if aligned else 95, 33, rng)
            foot = make_column(60, 535, 700, 2, rng, size=8, pitch=11, indent_every=99)
            words = head + left + right + foot
            rng.shuffle(words)
            lay = analyze(words, 595, 842)
            kinds = [b.kind for b in lay.blocks]
            assert kinds == ["full", "column", "column", "full"], (gutter, aligned, kinds)
            assert len(lay.blocks[1].lines) == 40 and len(lay.blocks[2].lines) == 33
            assert lay.blocks[1].x1 < mid < lay.blocks[2].x0
            assert lay.n_columns == 2


def test_layout_three_columns():
    rng = random.Random(3)
    cols = [make_column(40 + i * 175, 40 + i * 175 + 160, 80, 30 + i, rng, size=8, pitch=11) for i in range(3)]
    words = [w for c in cols for w in c]
    rng.shuffle(words)
    lay = analyze(words, 595, 842)
    assert [b.kind for b in lay.blocks] == ["column"] * 3
    assert [round(b.x0) for b in lay.blocks] == sorted(round(b.x0) for b in lay.blocks)
    assert lay.n_columns == 3


def test_layout_columns_then_full_width_then_columns():
    rng = random.Random(4)
    a = make_column(60, 285, 80, 12, rng) + make_column(310, 535, 80, 12, rng)
    mid = make_column(60, 535, 270, 3, rng, indent_every=99)
    b = make_column(60, 285, 340, 12, rng) + make_column(310, 535, 340, 12, rng)
    lay = analyze(a + mid + b, 595, 842)
    assert [b_.kind for b_ in lay.blocks] == ["column", "column", "full", "column", "column"]
    ys = [(round(b_.y0), round(b_.x0)) for b_ in lay.blocks]
    assert ys[0][1] < ys[1][1] and ys[3][1] < ys[4][1] and ys[1][0] < ys[2][0] < ys[3][0]


def test_margin_heading():
    """A heading in the margin beside the first lines of a paragraph (older Soviet books)."""
    rng = random.Random(9)
    body_top = make_column(60, 535, 80, 6, rng)  # text above
    heading = [Word("Феодально-", 60, 180, 120, 189), Word("монархические", 60, 192, 128, 201), Word("движения", 60, 204, 108, 213)]
    beside = make_column(150, 535, 180, 4, rng, indent_every=99, last_short=False)  # lines beside the heading
    below = make_column(60, 535, 236, 6, rng, indent_every=99)  # the paragraph continues full width
    for w in below[:3]:
        pass
    lay = analyze(body_top + heading + beside + below, 595, 842)
    texts = [" ".join(l.text for l in b.lines) for b in lay.blocks]
    assert any(t.startswith("Феодально-") and "движения" in t and len(t) < 40 for t in texts), texts
    # the lines beside the heading and the lines below form one paragraph
    paras = lay.paragraphs()
    assert not any("Феодально" in p.text and "w" in p.text for p in paras), [p.text[:40] for p in paras]


def test_selection_stays_inside_column():
    rng = random.Random(5)
    left = make_column(60, 285, 90, 20, rng)
    right = make_column(310, 535, 90, 20, rng)
    lay = analyze(left + right, 595, 842)
    a = lay.position_at(70, 120)
    # The pointer travels into the right column; confined to the anchor block it clamps to the left one.
    b = lay.position_at(500, 300, block=a[0])
    assert a[0] == b[0] == 0
    rects = lay.selection_rects(a, b)
    assert rects and all(r[2] <= 286 for r in rects)
    # Unconfined, the selection continues into the second column in reading order.
    c = lay.position_at(500, 300)
    assert c[0] == 1
    assert len(lay.selection_text(a, c)) > len(lay.selection_text(a, b))
    # Pointer above / below the column clamps to its first / last character.
    assert lay.position_at(70, -50, block=0) == lay.start_of(0)
    assert lay.position_at(70, 5000, block=0) == lay.end_of(0)


def test_join_lines_dehyphenates():
    assert join_lines(["истори-", "ческий очерк"]) == "исторический очерк"
    assert join_lines(["в 1917 -", "1918 годах"]) == "в 1917 - 1918 годах"
    assert join_lines(["Алма-", "Ата"]) == "Алма- Ата"  # capital after hyphen: keep


# ------------------------------------------------------------ translation glue

def test_language_detection():
    assert detect_language(" ".join(ms.RU)) == "rus_Cyrl"
    assert detect_language(" ".join(ms.KK)) == "kaz_Cyrl"
    assert detect_language(" ".join(ms.TR)) == "tur_Latn"
    assert detect_language(" ".join(ms.EN)) is None
    assert detect_language("— 27 —") is None
    for p in ms.RU + ms.KK + ms.TR:
        assert detect_language(p) in ("rus_Cyrl", "kaz_Cyrl", "tur_Latn")


def test_sentence_splitting():
    s = split_sentences("В 1917 г. в Киеве выходило семь газет. А. С. Иванов писал об этом в т. 2, стр. 15. Что дальше?")
    assert s == ["В 1917 г. в Киеве выходило семь газет.", "А. С. Иванов писал об этом в т. 2, стр. 15.", "Что дальше?"], s
    assert len(split_sentences(" ".join(ms.KK[:1]))) == 2
    assert len(split_sentences(ms.EN[0])) == 2


def test_modernize_russian():
    assert modernize_russian("въ Кіевѣ было объявлено") == "в Киеве было объявлено"
    assert modernize_russian("объём") == "объём"


# ------------------------------------------------------------------------ OCR

def _ocr(name: str, mode: str = "auto"):
    src = samples() / name
    out = samples() / (Path(name).stem + f"_{mode}_ocr.pdf")
    res = ocr.process_document([src], out, ["rus", "kaz", "eng", "tur"], mode=mode)
    return res, out


def _blocks_text(layout):
    return [" ".join(l.text for l in b.lines) for b in layout.blocks]


def test_ocr_single_column():
    res, out = _ocr("scan_1col_ru.pdf")
    assert res.ocred == 1 and res.output == out
    doc = pymupdf.open(str(out))
    lay = page_layout(doc[0])
    assert len(lay.blocks) == 1
    r = recall(" ".join(ms.RU), lay.text())
    assert r > 0.95, f"Russian recall {r:.3f}"
    # original image kept, text invisible
    assert len(doc[0].get_images()) == 1
    assert all(s["type"] == 3 for s in doc[0].get_texttrace())


def test_ocr_two_columns_reading_order_and_languages():
    res, out = _ocr("scan_2col_mixed.pdf")
    assert res.ocred == 3
    doc = pymupdf.open(str(out))
    truth = [
        (ms.RU[:3] + ms.RU[3:4], ms.RU[4:] + ms.RU[1:3] + ms.RU[0:1]),
        (ms.KK[:3], ms.KK[3:] + ms.KK[1:2]),
        (ms.TR + ms.TR[:1], ms.EN + ms.EN[:1]),
    ]
    names = ["Russian", "Kazakh", "Turkish/English"]
    for pno, (left, right) in enumerate(truth):
        lay = page_layout(doc[pno])
        kinds = [b.kind for b in lay.blocks]
        assert kinds == ["full", "column", "column", "full"], (pno, kinds)
        texts = _blocks_text(lay)
        rl, rr = recall(" ".join(left), texts[1]), recall(" ".join(right), texts[2])
        print(f"   page {pno + 1} ({names[pno]}): left column recall {rl:.3f}, right column recall {rr:.3f}")
        assert rl > 0.93 and rr > 0.93, (pno, rl, rr)
        # No line of one column may contain words of the other: columns were not merged.
        assert lay.blocks[1].x1 < lay.blocks[2].x0
        # paragraphs survive the trip
        n_left = len([p for p in lay.paragraphs() if p.block == 1])
        assert n_left == len(left), (pno, n_left, len(left))


def test_ocr_skewed_scan_keeps_columns():
    res, out = _ocr("scan_2col_skewed.pdf")
    doc = pymupdf.open(str(out))
    truth = [(ms.RU[:4], ms.RU[4:] + ms.RU[1:3] + ms.RU[0:1]), (ms.KK[:3], ms.KK[3:] + ms.KK[1:2])]
    for pno, (left, right) in enumerate(truth):
        lay = page_layout(doc[pno])
        assert abs(lay.skew) > 0.02, lay.skew
        kinds = [b.kind for b in lay.blocks]
        assert kinds == ["full", "column", "column", "full"], (pno, kinds)
        texts = _blocks_text(lay)
        rl, rr = recall(" ".join(left), texts[1]), recall(" ".join(right), texts[2])
        print(f"   skewed page {pno + 1}: skew {math.degrees(math.atan(lay.skew)):+.2f} deg, recall {rl:.3f} / {rr:.3f}")
        assert rl > 0.93 and rr > 0.93
        assert len(lay.blocks[1].lines) == len(lay.blocks[2].lines) or pno == 1
        # a drag from the left column far into the right one stays in the left column
        a = lay.position_at(lay.blocks[1].x0 + 20, lay.blocks[1].y0 + 40)
        b = lay.position_at(lay.blocks[2].x1 - 20, lay.blocks[2].y0 + 200, block=a[0])
        assert a[0] == b[0] == 1
        sel = lay.selection_text(a, b)
        whole_left = lay.selection_text(lay.start_of(1), lay.end_of(1))
        assert len(sel) > 300 and sel in whole_left


def test_damaged_layer_is_replaced():
    src = samples() / "damaged_layer.pdf"
    doc = open_as_pdf(src)
    info = ocr.analyze_document(doc)[0]
    doc.close()
    assert info.status == "damaged" and info.scan
    res, out = _ocr("damaged_layer.pdf")
    assert res.ocred == 1 and res.damaged == 1
    doc = pymupdf.open(str(out))
    text = doc[0].get_text()
    assert "Â" not in text and "íà÷àëå" not in text, "old mojibake layer still present"
    assert recall(" ".join(ms.RU), text) > 0.95
    assert ocr.analyze_page(doc[0]).status == "good"
    assert len(doc[0].get_images()) == 1


def test_good_digital_pdf_is_left_alone():
    res, out = _ocr("digital_2col.pdf")
    assert res.output is None and res.ocred == 0 and not out.exists()
    doc = pymupdf.open(str(samples() / "digital_2col.pdf"))
    lay = page_layout(doc[0])
    assert [b.kind for b in lay.blocks] == ["full", "column", "column", "full"]
    texts = _blocks_text(lay)
    assert recall(" ".join(ms.RU[:4]), texts[1]) == 1.0
    assert recall(" ".join(ms.RU[4:] + ms.KK[:3]), texts[2]) == 1.0


def test_force_mode_and_garbled_visible_text():
    # A page whose visible text itself extracts as garbage is turned into an image and re-read.
    res, out = _ocr("digital_garbled.pdf")
    assert res.ocred == 1 and res.damaged == 1
    doc = pymupdf.open(str(out))
    assert len(doc[0].get_images()) == 1
    assert all(s["type"] == 3 for s in doc[0].get_texttrace())
    # force mode re-reads a page that already had good text
    res, out = _ocr("digital_2col.pdf", mode="force")
    assert res.ocred == 1 and out.exists()
    doc = pymupdf.open(str(out))
    lay = page_layout(doc[0])
    assert [b.kind for b in lay.blocks] == ["full", "column", "column", "full"]
    assert recall(" ".join(ms.RU[:4]), _blocks_text(lay)[1]) > 0.95


def test_image_input_and_combined_images():
    img = samples() / "scan_page.png"
    out = samples() / "from_image_ocr.pdf"
    res = ocr.process_document([img, img], out, ["rus"], mode="auto")
    assert res.pages == 2 and res.ocred == 2
    doc = pymupdf.open(str(out))
    assert recall(" ".join(ms.RU), doc[1].get_text()) > 0.95


def test_rotated_page():
    src = pymupdf.open(str(samples() / "scan_1col_ru.pdf"))
    src[0].set_rotation(0)
    # Store the scan sideways and declare /Rotate so that it displays upright.
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)
    page.show_pdf_page(page.rect, src, 0, rotate=90)
    page.set_rotation(90)
    path = samples() / "rotated.pdf"
    doc.save(str(path))
    out = samples() / "rotated_ocr.pdf"
    res = ocr.process_document([path], out, ["rus"])
    d2 = pymupdf.open(str(out))
    lay = page_layout(d2[0])
    assert res.ocred == 1 and recall(" ".join(ms.RU), lay.text()) > 0.95, recall(" ".join(ms.RU), lay.text())
    # Text boxes must sit on the displayed page.
    assert all(0 <= b.x0 and b.x1 <= d2[0].rect.width and 0 <= b.y0 and b.y1 <= d2[0].rect.height for b in lay.blocks)


def test_language_folder_without_configs():
    """Regression (0.2.1, Windows): the installer's tessdata folder has only .traineddata
    files and no configs/ subfolder; OCR must still return words."""
    import shutil

    system = ocr._system_tessdata_dir()
    folder = Path(tempfile.mkdtemp(prefix="paraq_tessdata_"))
    for lang in ("rus", "eng"):
        shutil.copyfile(Path(system) / f"{lang}.traineddata", folder / f"{lang}.traineddata")
    doc = pymupdf.open(str(samples() / "scan_1col_ru.pdf"))
    image = doc[0].get_pixmap(dpi=200, colorspace=pymupdf.csGRAY).tobytes("pnm")
    words = ocr.run_tesseract(image, ["rus", "eng"], folder)
    assert len(words) > 120, len(words)  # the sample page has about 150 words
    assert recall(" ".join(ms.RU), " ".join(w.text for w in words)) > 0.95


def test_blank_pages_not_ocred_again():
    """Regression (0.4.2): pages with no text after OCR (blank, photos) were flagged again every time."""
    doc = pymupdf.open(str(samples() / "scan_1col_ru.pdf"))
    doc.new_page(width=595, height=842)  # a blank page
    path = samples() / "with_blank.pdf"
    doc.save(str(path))
    out = samples() / "with_blank_ocr.pdf"
    res = ocr.process_document([path], out, ["rus"])
    assert res.ocred == 2
    d = pymupdf.open(str(out))
    infos = ocr.analyze_document(d)
    assert [i.status for i in infos] == ["good", "ocred"], [i.status for i in infos]
    assert ocr.pages_to_ocr(infos, "auto") == [] and ocr.pages_to_ocr(infos, "force") == [0, 1]
    assert ocr._decode_pages(ocr._encode_pages({0, 1, 2, 5, 7, 8})) == {0, 1, 2, 5, 7, 8}
    assert ocr._encode_pages({0, 1, 2, 5, 7, 8}) == "0-2,5,7-8"


if __name__ == "__main__":
    failed = 0
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback

            print(f"FAIL  {name}: {type(e).__name__}: {e}")
            traceback.print_exc(limit=3)
    print(f"\n{len(tests) - failed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
