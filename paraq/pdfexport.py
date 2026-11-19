"""Translated PDF: the original pages with the English set in place of the text.

Everything that is not translated text - photos, maps, graphs, table rules,
numbers, untranslated passages - stays exactly as it was. Two kinds of text are
handled differently:

* prose paragraphs: the paragraph area is painted in the paper's colour and the
  English (from the translation cache) is set into the same box, at a similar
  size, alignment and line spacing, shrinking if it does not fit;
* tables, captions and labels: every cell or label is translated and replaced on
  its own, in its own small box, so table rules and the layout of the cells
  survive.

The English is real text: it can be selected, searched and copied.
"""

from __future__ import annotations

import html
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from statistics import median

import pymupdf

from .config import resource_path
from .layout import Line, PageLayout, Paragraph, Word
from .pdfutil import FITZ_LOCK, page_layout
from .translate import paragraph_language

# DejaVu Serif Condensed with ligatures switched off, so "ff"/"fi" copy and search as letters
# (renamed as the Bitstream Vera licence requires for modified fonts).
SERIF = resource_path("fonts/ParaqSerif.ttf")
_NUMBER = re.compile(r"^[\d\s.,:;%()\[\]/–—\-+×x°№]+$")


@dataclass
class Item:
    rect: pymupdf.Rect  # the original text, to be covered
    text: str  # English
    size: float  # font size in points
    align: str = "left"  # left | justify | center | right
    line_height: float = 1.2
    indent: float = 0.0  # first-line indent in points
    nowrap: bool = False
    box: pymupdf.Rect | None = None  # where the English may go (defaults to rect)


# ---------------------------------------------------------------------------
# reading the page
# ---------------------------------------------------------------------------

def _gaps(words: list[Word]) -> list[float]:
    return [b.x0 - a.x1 for a, b in zip(words, words[1:])]


def is_tabular_line(line: Line) -> bool:
    """A table row: several numbers, or uneven wide gaps between the words.

    Justified prose can have wide gaps too, but they are all about the same.
    """
    words = line.words
    if len(words) < 2:
        return False
    numbers = sum(1 for w in words if _NUMBER.match(w.text))
    if numbers >= 3 and numbers >= 0.5 * len(words):  # "[8, с. 18]" in prose is not a table row
        return True
    gaps = _gaps(words)
    h = line.size
    widest = max(gaps)
    return widest > 2.0 * h and widest > 2.5 * median(gaps)


def classify(para: Paragraph, lines: list[Line]) -> str:
    """"prose" (reflow the cached translation in the paragraph box) or "cells" (line by line)."""
    tabular = sum(1 for l in lines if is_tabular_line(l))
    if tabular >= 2 or (tabular == 1 and len(lines) <= 3):
        return "cells"
    if len(lines) == 1:
        return "prose" if len(split_runs(lines[0])) == 1 else "cells"
    h = median(l.size for l in lines)
    if median(len(l.words) for l in lines) <= 2:
        return "cells"  # a column of labels (table row headings, a list of names)
    centers = [(l.y0 + l.y1) / 2 for l in lines]
    pitch = median([b - a for a, b in zip(centers, centers[1:])])
    if len(lines) >= 3 and pitch > 1.8 * h:
        return "cells"  # lines spaced like table rows
    rest = [l.x0 for l in lines[1:]]
    left_aligned = max(rest) - min(rest) <= 1.0 * h and -1.0 * h <= lines[0].x0 - min(rest) <= 4.0 * h
    mids = [(l.x0 + l.x1) / 2 for l in lines]
    centred = max(mids) - min(mids) <= 1.5 * h
    return "prose" if left_aligned or centred else "cells"


def _paragraph_lines(lay: PageLayout, para: Paragraph) -> list[Line]:
    ids = {id(w) for w, _a, _b in para.spans}
    return [l for b in lay.blocks for l in b.lines if any(id(w) in ids for w in l.words)]


def split_runs(line: Line) -> list[list[Word]]:
    """Cut a line into cells at gaps much wider than a word space."""
    runs: list[list[Word]] = [[line.words[0]]]
    limit = 1.2 * line.size
    for prev, w in zip(line.words, line.words[1:]):
        if w.x0 - prev.x1 > limit:
            runs.append([w])
        else:
            runs[-1].append(w)
    return runs


def looks_like_text(s: str) -> bool:
    """Real words rather than OCR noise from a photo or a map."""
    letters = sum(c.isalpha() for c in s)
    if letters < 2 or letters < 0.5 * len(s.replace(" ", "")):
        return False
    vowels = set("аеёиоуыэюяәөұүіaeiouıöü")
    return any(len(w) >= 3 and set(w.lower()) & vowels for w in re.findall(r"[^\W\d_]+", s))


_font: pymupdf.Font | None = None


def font_size_of(words: list[Word], h: float) -> float:
    """Font size that gives the original words their measured width in our serif font.

    The height of OCR boxes depends on the letters (a word without tall letters has a
    short box), so width is the steadier measure; it is kept within reason of the height.
    """
    global _font
    if _font is None:
        _font = pymupdf.Font(fontfile=str(SERIF))
    ink = sum(w.x1 - w.x0 for w in words)
    unit = sum(_font.text_length(w.text, fontsize=1) for w in words)
    by_height = h / 1.18
    if unit <= 0 or ink <= 0:
        return max(4.0, by_height)
    by_width = ink / unit
    return max(4.0, min(max(by_width, h / 1.3), h * 1.35))


def _style(lay: PageLayout, para: Paragraph, lines: list[Line]) -> tuple[float, float, str, float]:
    """Font size, line height, alignment and first-line indent of a prose paragraph."""
    h = median(l.size for l in lines)
    size = median(font_size_of(l.words, l.size) for l in lines)
    centers = [(l.y0 + l.y1) / 2 for l in lines]
    pitch = median([b - a for a, b in zip(centers, centers[1:])]) if len(lines) > 1 else 1.2 * size
    line_height = max(1.0, min(1.8, pitch / size))
    width = para.x1 - para.x0
    block = max((b for b in lay.blocks if b.x0 <= para.x0 + 1 and b.x1 >= para.x1 - 1), key=lambda b: b.x1 - b.x0, default=None)
    bx0, bx1 = (block.x0, block.x1) if block else (para.x0, para.x1)
    if len(lines) >= 2 and sum(1 for l in lines[:-1] if l.x1 > para.x1 - 0.06 * width) >= 0.6 * (len(lines) - 1):
        align = "justify"
    else:
        left_margin = min(l.x0 for l in lines) - bx0
        right_margin = bx1 - max(l.x1 for l in lines)
        centred = left_margin > 0.08 * (bx1 - bx0) and abs(left_margin - right_margin) < 0.25 * max(left_margin, right_margin)
        align = "center" if centred else "left"
    indent = 0.0
    if len(lines) >= 2:
        rest = median(l.x0 for l in lines[1:])
        if lines[0].x0 - rest > 0.5 * h:
            indent = lines[0].x0 - rest
    return size, line_height, align, indent


def _line_align(x0: float, x1: float, area: tuple[float, float], h: float) -> str:
    """left / center / right for a lone line such as a caption or a heading."""
    ax0, ax1 = area
    left, right = x0 - ax0, ax1 - x1
    if right < 1.5 * h and left > 4 * h:
        return "right"
    if left > 2 * h and abs(left - right) < max(2 * h, 0.15 * (left + right)):
        return "center"
    return "left"


def _room_below(rect: pymupdf.Rect, obstacles: list[pymupdf.Rect], page_bottom: float) -> float:
    """y where the next text below this rectangle (overlapping it horizontally) starts."""
    limit = page_bottom
    for o in obstacles:
        if o.y0 >= rect.y1 - 0.5 and o.x0 < rect.x1 - 1 and o.x1 > rect.x0 + 1:
            limit = min(limit, o.y0)
    return limit


def _room_right(rect: pymupdf.Rect, obstacles: list[pymupdf.Rect], page_right: float) -> float:
    """x where the next text to the right of this rectangle (on the same line) starts."""
    limit = page_right
    for o in obstacles:
        if o.x0 >= rect.x1 - 0.5 and o.y0 < rect.y1 - 1 and o.y1 > rect.y0 + 1:
            limit = min(limit, o.x0)
    return limit


def _match(entries: list[dict], paras: list[Paragraph]) -> list[tuple[Paragraph, dict | None]]:
    by_text = {e.get("src"): e for e in entries}
    out = []
    for i, p in enumerate(paras):
        e = entries[i] if i < len(entries) and entries[i].get("src") == p.text else by_text.get(p.text)
        out.append((p, e))
    return out


def plan_page(page: pymupdf.Page, entries: list[dict], translate_runs=None, source: str = "auto") -> list[Item]:
    """Decide what to replace on one page.

    translate_runs(list of (text, lang)) -> list of English strings, used for table
    cells and labels; None leaves them in the original language.
    """
    lay = page_layout(page)
    paras = [p for p in lay.paragraphs() if p.text.strip()]
    items: list[Item] = []
    runs: list[tuple[list[Word], float, str, str, str]] = []  # words, size, text, lang, alignment
    if not lay.blocks:
        return items
    area = (min(b.x0 for b in lay.blocks), max(b.x1 for b in lay.blocks))
    # every run of text on the page, for keeping English boxes from running into neighbours
    obstacles = [
        pymupdf.Rect(min(w.x0 for w in r), min(w.y0 for w in r), max(w.x1 for w in r), max(w.y1 for w in r))
        for b in lay.blocks for l in b.lines for r in split_runs(l)
    ]
    right_margin = min(page.rect.x1 - 18, area[1] + 6)
    bottom_margin = page.rect.y1 - 12
    for para, entry in _match(entries, paras):
        lines = _paragraph_lines(lay, para)
        if not lines:
            continue
        if classify(para, lines) == "prose":
            if not entry or not entry.get("lang") or not entry.get("en", "").strip() or not looks_like_text(para.text):
                continue
            size, lh, align, indent = _style(lay, para, lines)
            rect = pymupdf.Rect(para.x0, para.y0, para.x1, para.y1)
            if len(lines) == 1:
                align = _line_align(para.x0, para.x1, area, lines[0].size)
                if align != "left":
                    rect = pymupdf.Rect(area[0], para.y0, area[1], para.y1) if align == "center" else pymupdf.Rect(
                        area[0], para.y0, para.x1, para.y1
                    )
            # The CSS line box is a whole line pitch tall, the ink of the original is not:
            # allow the gap down to the next text below so the English never has to shrink needlessly.
            others = [o for o in obstacles if not (o.x0 >= para.x0 - 0.5 and o.x1 <= para.x1 + 0.5 and o.y0 >= para.y0 - 0.5 and o.y1 <= para.y1 + 0.5)]
            bottom = min(_room_below(pymupdf.Rect(para.x0, para.y0, para.x1, para.y1), others, bottom_margin) - 1, para.y1 + 2 * size * lh)
            lift = max(0.0, (lh - 1.164) * size / 2)  # CSS half-leading above the first line
            box = pymupdf.Rect(rect.x0, rect.y0 - lift, rect.x1, max(bottom, rect.y1 + size * (lh - 1) + 2))
            items.append(Item(pymupdf.Rect(para.x0, para.y0, para.x1, para.y1), entry["en"], size, align, lh, indent, box=box))
            continue
        # table rows, captions over tables, labels: cell by cell
        for line in lines:
            line_runs = split_runs(line)
            for words in line_runs:
                text = " ".join(w.text for w in words)
                lang = paragraph_language(text, source)
                if lang and looks_like_text(text):
                    x0, x1 = min(w.x0 for w in words), max(w.x1 for w in words)
                    align = _line_align(x0, x1, area, line.size) if len(line_runs) == 1 else "left"
                    runs.append((words, line.size, text, lang, align))
    if runs and translate_runs is not None:
        english = translate_runs([(t, lang) for _w, _s, t, lang, _a in runs])
        font = pymupdf.Font(fontfile=str(SERIF))
        for (words, h, src, _lang, align), en in zip(runs, english):
            if not en or not en.strip() or en.strip() == src.strip():
                continue
            en = en.strip()
            rect = pymupdf.Rect(min(w.x0 for w in words), min(w.y0 for w in words), max(w.x1 for w in words), max(w.y1 for w in words))
            size = font_size_of(words, h)
            need = font.text_length(en, fontsize=size) + 2
            others = [o for o in obstacles if not (abs(o.x0 - rect.x0) < 0.5 and abs(o.y0 - rect.y0) < 0.5)]
            if align == "center":
                mid = (rect.x0 + rect.x1) / 2
                half = max(rect.width, need) / 2
                box = pymupdf.Rect(max(area[0], mid - half), rect.y0, min(right_margin, mid + half), rect.y1)
            elif align == "right":
                box = pymupdf.Rect(max(area[0], rect.x1 - max(rect.width, need)), rect.y0, rect.x1, rect.y1)
            else:
                limit = min(_room_right(rect, others, right_margin) - 2, right_margin)
                box = pymupdf.Rect(rect.x0, rect.y0, max(rect.x1, min(rect.x0 + need, limit)), rect.y1)
            mid_y = (rect.y0 + rect.y1) / 2
            box.y0, box.y1 = mid_y - size * 0.62, mid_y + size * 0.62 + 0.5
            items.append(Item(rect, en, size, align, 1.1, 0.0, nowrap=True, box=box))
    return items


# ---------------------------------------------------------------------------
# writing the page
# ---------------------------------------------------------------------------

def _paper_colour(pix: pymupdf.Pixmap, scale: float, rect: pymupdf.Rect) -> tuple[float, float, float]:
    """Colour of the paper around a text area: the bright end of the pixels along its border."""
    x0, y0 = int(max(0, (rect.x0 - 3) * scale)), int(max(0, (rect.y0 - 3) * scale))
    x1, y1 = int(min(pix.width - 1, (rect.x1 + 3) * scale)), int(min(pix.height - 1, (rect.y1 + 3) * scale))
    samples = []
    step = max(1, (x1 - x0) // 40)
    for x in range(x0, x1 + 1, step):
        samples += [pix.pixel(x, y0), pix.pixel(x, y1)]
    step = max(1, (y1 - y0) // 40)
    for y in range(y0, y1 + 1, step):
        samples += [pix.pixel(x0, y), pix.pixel(x1, y)]
    if not samples:
        return (1.0, 1.0, 1.0)
    # Text strokes and table rules are dark and thin: the paper is the bright part of the border.
    samples.sort(key=sum)
    paper = samples[int(0.85 * (len(samples) - 1))]
    if len(paper) == 1:
        return (paper[0] / 255,) * 3  # type: ignore[return-value]
    return tuple(c / 255 for c in paper[:3])  # type: ignore[return-value]


_CSS_FONT = "@font-face {font-family: paraqserif; src: url(ParaqSerif.ttf);}\n"


def _html(item: Item) -> tuple[str, str]:
    # MuPDF keeps a default body margin unless body and p are reset explicitly.
    css = _CSS_FONT + "body, p {margin: 0; padding: 0;}\n" + (
        f"* {{font-family: paraqserif; font-size: {item.size:.2f}pt; line-height: {item.line_height:.2f};}}\n"
        f"p {{text-align: {item.align}; text-indent: {item.indent:.1f}pt;"
        + (" white-space: nowrap;" if item.nowrap else "")
        + "}\n"
    )
    return f"<p>{html.escape(item.text)}</p>", css


def write_page(page: pymupdf.Page, items: list[Item]) -> int:
    """Cover the original text of every item with the paper colour and set the English in its place."""
    if not items:
        return 0
    scale = 2.0
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    pad = 1.2
    covers = []
    for it in items:
        r = pymupdf.Rect(it.rect.x0 - pad, it.rect.y0 - pad, it.rect.x1 + pad, it.rect.y1 + pad) & page.rect
        covers.append((r, _paper_colour(pix, scale, it.rect)))
    for r, colour in covers:
        page.add_redact_annot(r, fill=colour, cross_out=False)
    # Remove the old (invisible OCR or visible) text under the boxes; images and lines stay,
    # and the boxes are painted over the picture of the text.
    page.apply_redactions(
        images=pymupdf.PDF_REDACT_IMAGE_NONE,
        graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
        text=pymupdf.PDF_REDACT_TEXT_REMOVE,
    )
    archive = pymupdf.Archive(str(SERIF.parent))
    done = 0
    for it, (r, _c) in zip(items, covers):
        body, css = _html(it)
        box = (it.box or it.rect) & page.rect
        box = pymupdf.Rect(box.x0 - 0.5, box.y0 - 0.5, box.x1 + 0.5, box.y1)
        spare, _scale = page.insert_htmlbox(box, body, css=css, archive=archive, scale_low=0)
        if spare >= 0:
            done += 1
    return done


# ---------------------------------------------------------------------------
# whole documents
# ---------------------------------------------------------------------------

@dataclass
class ExportResult:
    pages: int
    translated_pages: int
    replaced: int


def export_translated_pdf(
    pdf_path: str | Path,
    store_pages: dict[str, list[dict]],
    out_path: str | Path,
    side_by_side: bool = False,
    translate_runs=None,
    source: str = "auto",
    progress=None,
    cancel: threading.Event | None = None,
) -> ExportResult:
    """Write the translated PDF (or original | translated pages side by side) to out_path."""
    with FITZ_LOCK:
        doc = pymupdf.open(str(pdf_path))
    replaced = 0
    translated = 0
    try:
        n = doc.page_count
        for pno in range(n):
            if cancel is not None and cancel.is_set():
                raise RuntimeError("cancelled")
            entries = store_pages.get(str(pno))
            if progress:
                progress("Building translated PDF", pno, n)
            if entries is None:
                continue
            with FITZ_LOCK:
                page = doc[pno]
                items = plan_page(page, entries, translate_runs, source)
            # translation of table cells may have run outside the lock; write under it
            with FITZ_LOCK:
                replaced += write_page(doc[pno], items)
            translated += 1
        with FITZ_LOCK:
            if side_by_side:
                original = pymupdf.open(str(pdf_path))
                out = pymupdf.open()
                for pno in range(n):
                    r = original[pno].rect
                    gap = 18
                    page = out.new_page(width=2 * r.width + gap, height=r.height)
                    page.show_pdf_page(pymupdf.Rect(0, 0, r.width, r.height), original, pno)
                    page.show_pdf_page(pymupdf.Rect(r.width + gap, 0, 2 * r.width + gap, r.height), doc, pno)
                    page.draw_line((r.width + gap / 2, 12), (r.width + gap / 2, r.height - 12), color=(0.75, 0.75, 0.75), width=0.5)
                out.save(str(out_path), garbage=3, deflate=True)
                out.close()
                original.close()
            else:
                try:
                    doc.subset_fonts()
                except Exception:  # noqa: BLE001
                    pass
                doc.save(str(out_path), garbage=3, deflate=True)
        if progress:
            progress("Building translated PDF", n, n)
        return ExportResult(n, translated, replaced)
    finally:
        with FITZ_LOCK:
            doc.close()
