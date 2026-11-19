"""Page layout analysis: columns, lines, paragraphs and column-aware text selection.

Everything here works on word boxes only (text + rectangle in PDF points), so the
same code serves freshly OCRed pages and PDFs that already carry a text layer.

Scans are rarely straight, so the page skew is estimated first and all geometry
is done in deskewed (u, v) coordinates; the original (x, y) boxes are kept for
drawing.

The segmentation is a band-based variant of the XY-cut:

1. words are grouped into horizontal *strips* (sets of words whose vertical
   extents chain together - usually one text line, or a whole multi-column body
   when the lines of neighbouring columns do not align);
2. consecutive strips that share a vertical white *gutter* form a multi-column
   band; the band is split at its gutters and every column is analysed again;
3. strips that share no gutter with their neighbours (headings, footnotes,
   single-column text) are merged into full-width blocks.

A gutter only counts if text runs on both sides of it over several lines and
the lines to its right start at a common left edge. That is what tells a column
gap from the white "rivers" that open up inside justified text.

Reading order is: bands top to bottom, columns left to right inside a band.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from statistics import median

# A selection position: (block index, line index, word index, character offset).
Pos = tuple[int, int, int, int]

MAX_DEPTH = 3
SENTENCE_END = ".?!:…»”\")"
HYPHENS = "-‐‑¬"


@dataclass
class Word:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    edges: list[float] | None = None  # len(text)+1 x-positions of character boundaries
    # Deskewed coordinates, filled in by analyze().
    u0: float = 0.0
    v0: float = 0.0
    u1: float = 0.0
    v1: float = 0.0

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    @property
    def cu(self) -> float:
        return (self.u0 + self.u1) / 2

    @property
    def cv(self) -> float:
        return (self.v0 + self.v1) / 2

    def char_edges(self) -> list[float]:
        n = len(self.text)
        if self.edges is not None and len(self.edges) == n + 1:
            return self.edges
        if n == 0:
            return [self.x0, self.x1]
        step = (self.x1 - self.x0) / n
        return [self.x0 + i * step for i in range(n + 1)]


class _Box:
    """Bounding boxes of a group of words, on the page (x, y) and deskewed (u, v)."""

    def _fit(self, words: list[Word]) -> None:
        self.x0 = min(w.x0 for w in words)
        self.x1 = max(w.x1 for w in words)
        self.y0 = min(w.y0 for w in words)
        self.y1 = max(w.y1 for w in words)
        self.u0 = min(w.u0 for w in words)
        self.u1 = max(w.u1 for w in words)
        self.v0 = min(w.v0 for w in words)
        self.v1 = max(w.v1 for w in words)


class Line(_Box):
    def __init__(self, words: list[Word]):
        self.words = sorted(words, key=lambda w: w.u0)
        self._fit(self.words)
        self.size = median(w.h for w in self.words)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


class Block(_Box):
    def __init__(self, lines: list[Line], kind: str = "full", band_cols: int = 1):
        self.lines = lines
        self.kind = kind  # "column" when the block is one column of a multi-column band
        self.band_cols = band_cols
        self._fit([w for l in lines for w in l.words])


@dataclass
class Paragraph:
    block: int
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    # (word, start, end): where each word of the page sits in `text`
    spans: list = field(default_factory=list)

    def words_in(self, start: int, end: int) -> list[Word]:
        """Words whose characters overlap text[start:end]."""
        return [w for w, a, b in self.spans if a < end and b > start]


@dataclass
class PageLayout:
    blocks: list[Block] = field(default_factory=list)
    width: float = 0.0
    height: float = 0.0
    skew: float = 0.0  # slope dy/dx of the text lines

    @property
    def n_columns(self) -> int:
        """Largest number of side-by-side columns found on the page."""
        return max((b.band_cols for b in self.blocks), default=0)

    def is_empty(self) -> bool:
        return not self.blocks

    def to_uv(self, x: float, y: float) -> tuple[float, float]:
        return x + self.skew * y, y - self.skew * x

    # ---------------------------------------------------------------- text
    def paragraphs(self) -> list[Paragraph]:
        groups: list[tuple[int, list[Line]]] = []
        for bi, block in enumerate(self.blocks):
            for k, lines in enumerate(_split_paragraphs(block)):
                beside_heading = k == 0 and bi > 0 and self.blocks[bi - 1].kind == "column" and block.kind == "full"
                if groups and _continues(groups[-1][1], lines, beside_heading):
                    groups[-1] = (groups[-1][0], groups[-1][1] + lines)
                else:
                    groups.append((bi, lines))
        out: list[Paragraph] = []
        for bi, lines in groups:
            text, spans = join_words(lines)
            out.append(
                Paragraph(
                    bi,
                    text,
                    min(l.x0 for l in lines),
                    min(l.y0 for l in lines),
                    max(l.x1 for l in lines),
                    max(l.y1 for l in lines),
                    spans,
                )
            )
        return out

    def text(self) -> str:
        return "\n\n".join(p.text for p in self.paragraphs())

    # ----------------------------------------------------------- positions
    def start_of(self, b: int) -> Pos:
        return (b, 0, 0, 0)

    def end_of(self, b: int) -> Pos:
        block = self.blocks[b]
        li = len(block.lines) - 1
        wi = len(block.lines[li].words) - 1
        return (b, li, wi, len(block.lines[li].words[wi].text))

    def block_at(self, x: float, y: float) -> int | None:
        """Index of the block under the point, else the nearest block."""
        u, v = self.to_uv(x, y)
        best, best_d = None, None
        for i, b in enumerate(self.blocks):
            du = max(b.u0 - u, 0.0, u - b.u1)
            dv = max(b.v0 - v, 0.0, v - b.v1)
            d = du * du + dv * dv
            if best_d is None or d < best_d:
                best, best_d = i, d
        return best

    def position_at(self, x: float, y: float, block: int | None = None) -> Pos | None:
        """Nearest text position to a point; `block` confines the answer to one block."""
        if not self.blocks:
            return None
        b = self.block_at(x, y) if block is None else block
        u, v = self.to_uv(x, y)
        blk = self.blocks[b]
        if v < blk.lines[0].v0:
            return self.start_of(b)
        if v > blk.lines[-1].v1:
            return self.end_of(b)
        li = min(
            range(len(blk.lines)),
            key=lambda i: max(blk.lines[i].v0 - v, 0.0, v - blk.lines[i].v1),
        )
        line = blk.lines[li]
        if u <= line.u0:
            return (b, li, 0, 0)
        if u >= line.u1:
            return (b, li, len(line.words) - 1, len(line.words[-1].text))
        for wi, w in enumerate(line.words):
            if u < w.u0:  # in the gap before this word
                prev = line.words[wi - 1]
                if u - prev.u1 <= w.u0 - u:
                    return (b, li, wi - 1, len(prev.text))
                return (b, li, wi, 0)
            if u <= w.u1:
                # Same relative position inside the word on the page as in deskewed space.
                px = w.x0 + (u - w.u0) / max(w.u1 - w.u0, 1e-6) * (w.x1 - w.x0)
                edges = w.char_edges()
                ci = min(range(len(edges)), key=lambda i: abs(edges[i] - px))
                return (b, li, wi, ci)
        return (b, li, len(line.words) - 1, len(line.words[-1].text))

    def word_at(self, x: float, y: float) -> tuple[Pos, Pos] | None:
        u, v = self.to_uv(x, y)
        for bi, blk in enumerate(self.blocks):
            if not (blk.u0 <= u <= blk.u1 and blk.v0 <= v <= blk.v1):
                continue
            for li, line in enumerate(blk.lines):
                if not (line.v0 <= v <= line.v1):
                    continue
                for wi, w in enumerate(line.words):
                    if w.u0 <= u <= w.u1:
                        return (bi, li, wi, 0), (bi, li, wi, len(w.text))
        return None

    # ----------------------------------------------------------- selection
    def _iter_selection(self, a: Pos, b: Pos):
        """Yield (block index, line, first word, c0, last word, c1) for every selected line."""
        if a > b:
            a, b = b, a
        for bi in range(a[0], b[0] + 1):
            blk = self.blocks[bi]
            l_first = a[1] if bi == a[0] else 0
            l_last = b[1] if bi == b[0] else len(blk.lines) - 1
            for li in range(l_first, l_last + 1):
                line = blk.lines[li]
                w0, c0 = (a[2], a[3]) if (bi, li) == (a[0], a[1]) else (0, 0)
                if (bi, li) == (b[0], b[1]):
                    w1, c1 = b[2], b[3]
                else:
                    w1, c1 = len(line.words) - 1, len(line.words[-1].text)
                yield bi, line, w0, c0, w1, c1

    def selection_rects(self, a: Pos, b: Pos) -> list[tuple[float, float, float, float]]:
        """Highlight rectangles (page coordinates) for the text between two positions."""
        rects = []
        for _bi, line, w0, c0, w1, c1 in self._iter_selection(a, b):
            xa = line.words[w0].char_edges()[c0]
            xb = line.words[w1].char_edges()[c1]
            if xb <= xa:
                continue
            if abs(self.skew) < 1e-4:
                rects.append((xa, line.y0, xb, line.y1))
                continue
            # On a skewed scan the band follows the line word by word.
            half = (line.v1 - line.v0) / 2
            mid_v = (line.v0 + line.v1) / 2
            for wi in range(w0, w1 + 1):
                w = line.words[wi]
                left = xa if wi == w0 else w.x0
                right = xb if wi == w1 else line.words[wi + 1].x0
                cy = mid_v + self.skew * (left + right) / 2  # back from v to page y
                if right > left:
                    rects.append((left, cy - half, right, cy + half))
        return rects

    def selection_text(self, a: Pos, b: Pos, reflow: bool = True) -> str:
        blocks: list[list[str]] = []
        last_block = None
        for bi, line, w0, c0, w1, c1 in self._iter_selection(a, b):
            parts = []
            for wi in range(w0, w1 + 1):
                t = line.words[wi].text
                lo = c0 if wi == w0 else 0
                hi = c1 if wi == w1 else len(t)
                if t[lo:hi]:
                    parts.append(t[lo:hi])
            if bi != last_block:
                blocks.append([])
                last_block = bi
            if parts:
                blocks[-1].append(" ".join(parts))
        if reflow:
            return "\n\n".join(join_lines(ls) for ls in blocks if ls)
        return "\n\n".join("\n".join(ls) for ls in blocks if ls)


# ---------------------------------------------------------------------------
# text helpers
# ---------------------------------------------------------------------------

def join_lines(lines: list[str]) -> str:
    """Join the lines of a paragraph, undoing end-of-line hyphenation."""
    out = ""
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        if not out:
            out = ln
        elif len(out) > 1 and out[-1] in HYPHENS and out[-2].isalpha() and ln[0].isalpha() and ln[0].islower():
            if _breaks_word(out.rsplit(" ", 1)[-1][:-1], ln.split(" ", 1)[0]):
                out = out[:-1] + ln  # a word broken at the line end
            else:
                out += ln  # a compound: keep the hyphen, no space
        else:
            out += " " + ln
    return out


def _continues(prev: list[Line], nxt: list[Line], below_columns: bool = False) -> bool:
    """Does `nxt` (first lines of the next block) carry on the sentence of `prev`?

    True when the text runs on straight below: the previous paragraph ends without a
    full stop, the next starts in lowercase, directly underneath and overlapping it.
    That happens below a margin heading, where the first lines of a paragraph form
    a block of their own.
    """
    a, b = prev[-1], nxt[0]
    end = a.words[-1].text.rstrip()
    start = b.words[0].text.lstrip("«\"(")
    if not end or not start:
        return False
    h = max(a.size, b.size)
    gap = b.v0 - a.v1
    overlap = min(a.u1, b.u1) - max(a.u0, b.u0)
    if not (-0.3 * h <= gap <= 1.2 * h and overlap > 0.5 * min(a.u1 - a.u0, b.u1 - b.u0)):
        return False
    if end[-1] not in SENTENCE_END and start[0].islower():
        return True  # the sentence runs on
    # Below lines set beside a margin heading, an unindented full-width line in the same
    # type size continues the paragraph even when a new sentence starts there.
    if below_columns and len(nxt) >= 2 and abs(a.size - b.size) < 0.2 * h:
        left = median(l.u0 for l in nxt[1:])
        return abs(b.u0 - left) < 0.5 * h
    return False


# Optional judge for a hyphen at the end of a line: keep_hyphen(left, right) -> True keeps it
# ("феодально-" + "монархические" is a compound, not a word broken in two). Set by ocrfix
# when a dictionary is available; without it every such hyphen is taken as a word break.
keep_hyphen = None


def _breaks_word(left: str, right: str) -> bool:
    if keep_hyphen is None:
        return True
    try:
        return not keep_hyphen(left, right)
    except Exception:  # noqa: BLE001
        return True


def join_words(lines: list[Line]) -> tuple[str, list[tuple[Word, int, int]]]:
    """join_lines() for real lines, also returning where every word lands in the text.

    A word hyphenated across a line break becomes one word in the text; the two
    halves on the page both point into it.
    """
    text = ""
    spans: list[tuple[Word, int, int]] = []
    for line in lines:
        for wi, w in enumerate(line.words):
            t = w.text.strip()
            if not t:
                continue
            hyphen_end = (
                wi == 0 and len(text) > 1 and text[-1] in HYPHENS and text[-2].isalpha() and t[0].isalpha() and t[0].islower()
            )
            if not text:
                start = 0
            elif hyphen_end and _breaks_word(text.rsplit(" ", 1)[-1][:-1], t):
                text = text[:-1]  # a word broken at the line end
                pw, pa, pb = spans[-1]
                spans[-1] = (pw, pa, pb - 1)
                start = len(text)
            elif hyphen_end:
                start = len(text)  # a compound: keep the hyphen, no space
            else:
                text += " "
                start = len(text)
            text += t
            spans.append((w, start, start + len(t)))
    return text, spans


def word_rects(words: list[Word], skew: float = 0.0) -> list[tuple[float, float, float, float]]:
    """Highlight rectangles for a run of words: one band per line, bridging word gaps."""
    rects = []
    for i, w in enumerate(words):
        right = w.x1
        if i + 1 < len(words):
            n = words[i + 1]
            same_line = abs(n.cv - w.cv) < 0.5 * max(w.h, n.h) and n.u0 >= w.u0
            if same_line and n.x0 - w.x1 < 3 * w.h:
                right = n.x0
        rects.append((w.x0, w.y0, right, w.y1))
    return rects


def _split_paragraphs(block: Block) -> list[list[Line]]:
    lines = block.lines
    if len(lines) <= 1:
        return [lines]
    # OCR line boxes shrink and grow with ascenders/descenders, so sizes are taken
    # from the median word of each line and distances from line centres.
    sizes = [l.size for l in lines]
    h = median(sizes)
    centers = [(l.v0 + l.v1) / 2 for l in lines]
    pitches = [b - a for a, b in zip(centers, centers[1:])]
    pitch = median(pitches) if pitches else h
    width = block.u1 - block.u0
    # Typical left edge of the block (ignores indented first lines).
    left = median(sorted(l.u0 for l in lines)[: max(1, len(lines) // 2)])
    paras: list[list[Line]] = [[lines[0]]]
    for i in range(1, len(lines)):
        prev, cur = lines[i - 1], lines[i]
        gap_break = pitches[i - 1] > 1.5 * pitch and (cur.v0 - prev.v1) > 0.6 * h
        indent = cur.u0 - left > 0.7 * h and not (prev.u0 - left > 0.7 * h)
        short_prev = (block.u1 - prev.u1) > 0.15 * width and prev.text[-1:] in SENTENCE_END
        big, small = max(sizes[i], sizes[i - 1]), min(sizes[i], sizes[i - 1])
        size_change = big > 1.5 * small
        if gap_break or indent or short_prev or size_change:
            paras.append([cur])
        else:
            paras[-1].append(cur)
    return paras


# ---------------------------------------------------------------------------
# skew
# ---------------------------------------------------------------------------

def line_slope(points: list[tuple[float, float]]) -> float | None:
    """Robust slope (Theil-Sen over well separated pairs) of (x, y) points on one text line."""
    n = len(points)
    if n < 4:
        return None
    pts = sorted(points)
    half = max(1, n // 2)
    slopes = [
        (pts[j][1] - pts[i][1]) / (pts[j][0] - pts[i][0])
        for i in range(n - half)
        for j in range(i + half, n)
        if pts[j][0] - pts[i][0] > 1e-6
    ]
    return median(slopes) if slopes else None


def page_skew(lines: list[list[tuple[float, float]]], min_span: float) -> float:
    """Skew of a page as the median slope of its long text lines; 0 when it is negligible."""
    slopes = []
    for pts in lines:
        if len(pts) >= 4 and max(p[0] for p in pts) - min(p[0] for p in pts) >= min_span:
            s = line_slope(pts)
            if s is not None:
                slopes.append(s)
    if len(slopes) < 3:
        return 0.0
    s = median(slopes)
    if abs(s) < 0.003:  # under about 0.2 degrees: treat as straight
        return 0.0
    return max(-0.12, min(0.12, s))


def estimate_skew(words: list[Word], med_h: float) -> float:
    """Chain neighbouring words into provisional lines and measure their slope."""
    if len(words) < 12:
        return 0.0
    chains: list[list[Word]] = []
    open_chains: list[list[Word]] = []
    for w in sorted(words, key=lambda w: w.x0):
        best = None
        best_gap = 4.0 * med_h
        still_open = []
        for ch in open_chains:
            p = ch[-1]
            gap = w.x0 - p.x1
            if gap > 60 * med_h:
                continue  # far behind: this chain can no longer grow
            still_open.append(ch)
            if -0.5 * med_h <= gap < best_gap:
                overlap = min(p.y1, w.y1) - max(p.y0, w.y0)
                if overlap > 0.5 * min(p.h, w.h):
                    best, best_gap = ch, gap
        open_chains = still_open
        if best is None:
            best = []
            chains.append(best)
            open_chains.append(best)
        best.append(w)
    pts = [[((w.x0 + w.x1) / 2, (w.y0 + w.y1) / 2) for w in ch] for ch in chains]
    return page_skew(pts, 8.0 * med_h)


# ---------------------------------------------------------------------------
# segmentation (all in deskewed u, v coordinates)
# ---------------------------------------------------------------------------

def _free_intervals(strip: list[Word], u0: float, u1: float, min_w: float):
    """White intervals of a strip inside [u0, u1], at least min_w wide."""
    spans = sorted((w.u0, w.u1) for w in strip)
    free = []
    cur = u0
    for a, b in spans:
        if a - cur >= min_w:
            free.append((cur, a))
        cur = max(cur, b)
    if u1 - cur >= min_w:
        free.append((cur, u1))
    return free


def _intersect(a, b, min_w: float):
    out = []
    i = j = 0
    while i < len(a) and j < len(b):
        lo = max(a[i][0], b[j][0])
        hi = min(a[i][1], b[j][1])
        if hi - lo >= min_w:
            out.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def _make_strips(words: list[Word]) -> list[list[Word]]:
    """Group words whose central vertical bands chain together."""
    items = sorted(words, key=lambda w: w.v0 + 0.25 * w.h)
    strips: list[list[Word]] = []
    cur: list[Word] = []
    cur_bottom = 0.0
    for w in items:
        top, bottom = w.v0 + 0.25 * w.h, w.v1 - 0.25 * w.h
        if cur and top >= cur_bottom:
            strips.append(cur)
            cur = []
        if not cur:
            cur_bottom = bottom
        cur.append(w)
        cur_bottom = max(cur_bottom, bottom)
    if cur:
        strips.append(cur)
    return strips


def _two_sided(words: list[Word], inter) -> bool:
    """True if some interval has words of this group on both of its sides."""
    for a, b in inter:
        mid = (a + b) / 2
        if any(w.cu < mid for w in words) and any(w.cu >= mid for w in words):
            return True
    return False


def _line_starts(words: list[Word]) -> list[float]:
    """Left edge of the first word of every text line in a group of words."""
    ws = sorted(words, key=lambda w: w.cv)
    starts = []
    n = len(ws)
    for i, w in enumerate(ws):
        first = True
        for step in (-1, 1):
            k = i + step
            while 0 <= k < n and abs(ws[k].cv - w.cv) < max(w.h, ws[k].h):
                o = ws[k]
                if o.u0 < w.u0 and min(o.v1, w.v1) - max(o.v0, w.v0) > 0.5 * min(o.h, w.h):
                    first = False
                    break
                k += step
            if not first:
                break
        if first:
            starts.append(w.u0)
    return starts


def _aligned_left_edge(words: list[Word], med_h: float, strict: bool) -> bool:
    """Do the lines of this text start at a common left edge (allowing indented first lines)?

    True for a column of text; false for the ragged word starts along a white
    river inside justified text. A narrow gap (`strict`) must prove itself over
    more lines than a wide one, because rivers are narrow.
    """
    starts = sorted(_line_starts(words))
    n = len(starts)
    min_lines = 5 if strict else 3
    if n < min_lines:
        return False
    tol = 0.5 * med_h
    best = 0
    j = 0
    for i in range(n):
        while starts[i] - starts[j] > tol:
            j += 1
        best = max(best, i - j + 1)
    return best >= max(min_lines - 1, math.ceil((0.7 if strict else 0.6) * n))


def _side_heading(left: list[Word], right: list[Word], region_w: float, med_h: float, region_u0: float | None = None) -> bool:
    """A narrow heading set in the margin beside the start of a paragraph (common in older books).

    The left part is a few short lines of real words, starting no higher than the text beside it.
    """
    if not left or len(right) < 4 or len(left) > 25:
        return False
    l_w = max(w.u1 for w in left) - min(w.u0 for w in left)
    if l_w > 0.3 * region_w:
        return False
    if region_u0 is not None and min(w.u0 for w in left) - region_u0 > max(1.5 * med_h, 0.08 * region_w):
        return False  # not in the margin (headings there are often centred in it)
    gap = min(w.u0 for w in right) - max(w.u1 for w in left)
    if gap < 1.2 * med_h:
        return False
    if gap < 2.0 * med_h:
        # A white "river" inside justified text splits lines: its left words sit on the same
        # lines as the right ones. A margin heading has lines of its own.
        shared = sum(1 for l in left if any(abs(l.cv - r.cv) < 0.25 * med_h for r in right))
        if shared > 0.5 * len(left):
            return False
    if len(_line_starts(left)) > 5 or min(w.v0 for w in left) < min(w.v0 for w in right) - 1.0 * med_h:
        return False
    letters = sum(c.isalpha() for w in left for c in w.text)
    return letters >= 6 and any(sum(c.isalpha() for c in w.text) >= 4 for w in left)


def _candidate_gutters(inter, run_words: list[Word], region_w: float, min_overlap: float, med_h: float = 0.0, region_u0: float | None = None):
    """Cheap test: intervals with a body of text on both sides, side by side over several lines."""
    good = []
    for a, b in inter:
        mid = (a + b) / 2
        left = [w for w in run_words if w.cu < mid]
        right = [w for w in run_words if w.cu >= mid]
        if med_h and region_u0 is not None and _side_heading(left, right, region_w, med_h, region_u0):
            good.append((a, b))
            continue
        if len(left) < 4 or len(right) < 4:
            continue
        l_top, l_bot = min(w.v0 for w in left), max(w.v1 for w in left)
        r_top, r_bot = min(w.v0 for w in right), max(w.v1 for w in right)
        if min(l_bot, r_bot) - max(l_top, r_top) < min_overlap:
            continue
        l_w = max(w.u1 for w in left) - min(w.u0 for w in left)
        r_w = max(w.u1 for w in right) - min(w.u0 for w in right)
        if min(l_w, r_w) < 0.08 * region_w:
            continue
        good.append((a, b))
    return good


def _confirm_gutters(gutters, run_words: list[Word], med_h: float, region_w: float = 0.0, region_u0: float | None = None):
    """Expensive test: keep gutters whose right-hand text starts at a straight left edge."""
    good = []
    for a, b in gutters:
        mid = (a + b) / 2
        strict = (b - a) < 2.0 * med_h
        left = [w for w in run_words if w.cu < mid]
        right = [w for w in run_words if w.cu >= mid]
        if region_w and region_u0 is not None and _side_heading(left, right, region_w, med_h, region_u0):
            if _aligned_left_edge(right, med_h, False):
                good.append((a, b))
            continue
        if len(_line_starts(left)) < (5 if strict else 3):
            continue
        if _aligned_left_edge(right, med_h, strict):
            good.append((a, b))
    return good


def _segment(words: list[Word], med_h: float, min_gutter: float, depth: int, kind: str):
    """Return a list of (words, kind, n_cols_in_band) groups in reading order."""
    if len(words) < 8 or depth > MAX_DEPTH:
        return [(words, kind, 1)]
    u0 = min(w.u0 for w in words)
    u1 = max(w.u1 for w in words)
    strips = _make_strips(words)
    # Large type has large word spaces: a strip's own size sets its minimum gap.
    free = [_free_intervals(s, u0, u1, max(min_gutter, 0.8 * median(w.h for w in s))) for s in strips]
    n = len(strips)
    out = []
    pending: list[Word] = []

    def flush():
        nonlocal pending
        if pending:
            out.append((pending, kind, 1))
            pending = []

    tops = [min(w.v0 for w in s) for s in strips]
    bottoms = [max(w.v1 for w in s) for s in strips]
    sizes = [median(w.h for w in s) for s in strips]
    # Inside an already detected column a further split needs stronger evidence.
    min_overlap = (5.0 if depth == 0 else 8.0) * med_h

    i = 0
    while i < n:
        candidates = []  # (last strip of the run, gutters), shortest run first
        inter = free[i]
        run: list[Word] = list(strips[i])
        j = i
        while inter:
            # margin headings are only looked for on the page as a whole, not inside columns
            gutters = _candidate_gutters(inter, run, u1 - u0, min_overlap, med_h, u0 if depth == 0 else None)
            if gutters:
                candidates.append((j, gutters))
            if j + 1 >= n:
                break
            nxt = _intersect(inter, free[j + 1], min_gutter)
            if not nxt:
                break
            # A one-sided strip after a wide blank gap, or after a smaller gap in another
            # type size (a footnote under the left column, a heading above it), is not
            # part of the columns.
            gap = tops[j + 1] - bottoms[j]
            if gap > 1.2 * med_h and not (_two_sided(run, nxt) and _two_sided(strips[j + 1], nxt)):
                run_size = median(w.h for w in run)
                if gap > 2.5 * med_h or abs(sizes[j + 1] - run_size) > 0.12 * run_size:
                    break
            inter = nxt
            j += 1
            run.extend(strips[j])
        best = None
        for j, gutters in reversed(candidates):  # prefer the longest run
            band = [w for s in strips[i : j + 1] for w in s]
            confirmed = _confirm_gutters(gutters, band, med_h, u1 - u0, u0 if depth == 0 else None)
            if confirmed:
                best = (j, confirmed, band)
                break
        if best is None:
            pending.extend(strips[i])
            i += 1
            continue
        flush()
        j, gutters, band = best
        cuts = [(a + b) / 2 for a, b in gutters]
        cols: list[list[Word]] = [[] for _ in range(len(cuts) + 1)]
        for w in band:
            cols[sum(1 for c in cuts if w.cu >= c)].append(w)
        cols = [c for c in cols if c]
        for col in cols:
            for sub_words, sub_kind, sub_n in _segment(col, med_h, min_gutter, depth + 1, "column"):
                out.append((sub_words, sub_kind, max(sub_n, len(cols))))
        i = j + 1
    flush()
    return out


def _make_lines(words: list[Word]) -> list[Line]:
    lines: list[list[Word]] = []
    boxes: list[list[float]] = []  # running [v0, v1] of each line
    for w in sorted(words, key=lambda w: (w.cv, w.u0)):
        placed = False
        for k in range(len(lines) - 1, max(-1, len(lines) - 4), -1):
            v0, v1 = boxes[k]
            overlap = min(v1, w.v1) - max(v0, w.v0)
            if overlap > 0.4 * min(v1 - v0, w.h) and (v0 <= w.cv <= v1 or w.v0 <= (v0 + v1) / 2 <= w.v1):
                lines[k].append(w)
                # Keep the line box close to its typical word so tall outliers do not swallow neighbours.
                if w.h <= 1.3 * (v1 - v0):
                    boxes[k] = [min(v0, w.v0), max(v1, w.v1)]
                placed = True
                break
        if not placed:
            lines.append([w])
            boxes.append([w.v0, w.v1])
    result = [Line(ws) for ws in lines]
    result.sort(key=lambda l: ((l.v0 + l.v1) / 2, l.u0))
    return result


def analyze(words: list[Word], width: float, height: float) -> PageLayout:
    """Build the layout of one page from its word boxes."""
    words = [w for w in words if w.text.strip() and w.x1 > w.x0 and w.y1 > w.y0]
    if not words:
        return PageLayout([], width, height)
    med_h = median(w.h for w in words)
    # Drop vertical rules and similar tall slivers that OCR sometimes reports as words.
    words = [
        w
        for w in words
        if not (w.h > 3 * med_h and (w.x1 - w.x0) < 0.8 * med_h and len(w.text) <= 2)
    ]
    if not words:
        return PageLayout([], width, height)
    skew = estimate_skew(words, med_h)
    for w in words:
        # Shear the word about its own centre so that its size is unchanged.
        cx, cy = (w.x0 + w.x1) / 2, (w.y0 + w.y1) / 2
        du, dv = skew * cy, -skew * cx
        w.u0, w.u1 = w.x0 + du, w.x1 + du
        w.v0, w.v1 = w.y0 + dv, w.y1 + dv
    min_gutter = 0.8 * med_h
    blocks = [
        Block(_make_lines(group), kind, ncols)
        for group, kind, ncols in _segment(words, med_h, min_gutter, 0, "full")
    ]
    return PageLayout(blocks, width, height, skew)


_ws = re.compile(r"\s+")


def normalize(text: str) -> str:
    return _ws.sub(" ", text).strip()
