"""OCR engine: decide which pages need OCR, run Tesseract, write a searchable PDF.

The output PDF keeps the original page images untouched and carries a new
invisible text layer. A damaged old text layer is removed first so that only the
new one is found by selection, search and copy.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import shutil
import subprocess
import sys
import threading
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from statistics import median

import pymupdf

from .config import CORE_LANGS, FONT_PATH, data_dir, resource_path
from .layout import page_skew
from .pdfutil import FITZ_LOCK, PDF_EXT, _TEXT_FLAGS, open_as_pdf
from .textquality import assess

OCR_SUFFIX = "_ocr"
log = logging.getLogger("paraq.ocr")


class OcrError(RuntimeError):
    pass


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------------------
# Tesseract discovery
# ---------------------------------------------------------------------------

_TESS_CANDIDATES = [
    "/opt/homebrew/bin/tesseract",  # Apple Silicon Homebrew
    "/usr/local/bin/tesseract",  # Intel Homebrew
    "/opt/local/bin/tesseract",  # MacPorts
    "/usr/bin/tesseract",
]
if os.name == "nt":
    for base in (
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
        os.environ.get("LOCALAPPDATA", ""),
    ):
        if base:
            _TESS_CANDIDATES.append(os.path.join(base, "Tesseract-OCR", "tesseract.exe"))

if sys.platform == "darwin":
    INSTALL_HINT = (
        "Tesseract OCR is not installed.\n\n"
        "Open Terminal and run:\n\n"
        "    brew install tesseract\n\n"
        "(Homebrew itself comes from https://brew.sh). Then start Paraq again."
    )
elif os.name == "nt":
    INSTALL_HINT = (
        "Tesseract OCR is not installed.\n\n"
        "Run \"Install Paraq (Windows).cmd\" again - it installs Tesseract - or install it from\n"
        "https://github.com/UB-Mannheim/tesseract/wiki and start Paraq again."
    )
else:
    INSTALL_HINT = "Tesseract OCR is not installed. Install it with your package manager (e.g. apt install tesseract-ocr)."


def _registry_tesseract() -> str | None:
    """Install folder recorded by the UB Mannheim Windows installer."""
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:
        return None
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Tesseract-OCR", 0, winreg.KEY_READ | view) as key:
                    folder, _ = winreg.QueryValueEx(key, "InstallDir")
                exe = os.path.join(folder, "tesseract.exe")
                if os.path.isfile(exe):
                    return exe
            except OSError:
                continue
    return None


def _configured_tesseract() -> str:
    """Path saved by the installer (setting "tesseract_path")."""
    try:
        from .config import Settings

        return str(Settings().get("tesseract_path") or "")
    except Exception:  # noqa: BLE001
        return ""


_tess_cache: str | None = None


def find_tesseract() -> str | None:
    global _tess_cache
    if _tess_cache and os.path.isfile(_tess_cache):
        return _tess_cache
    found = None
    for configured in (os.environ.get("PARAQ_TESSERACT", ""), _configured_tesseract()):
        if configured and os.path.isfile(configured):
            found = configured
            break
    if not found:
        bundled = resource_path("bin/tesseract.exe" if os.name == "nt" else "bin/tesseract")
        if bundled.exists():
            found = str(bundled)
    found = found or shutil.which("tesseract") or _registry_tesseract()
    if not found:
        found = next((c for c in _TESS_CANDIDATES if os.path.isfile(c)), None)
    _tess_cache = found
    return found


def tess_path(path: Path | str) -> str:
    """A form of the path that Tesseract can open.

    Tesseract on Windows reads its arguments in the legacy code page, so a folder
    under a user name with Kazakh or other non-Latin letters is not found. Use the
    8.3 short name there, or copy the language data to an ASCII-only folder.
    """
    path = str(path)
    if os.name != "nt" or path.isascii():
        return path
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(path, buf, 1024) and buf.value.isascii():
            return buf.value
    except Exception:  # noqa: BLE001
        pass
    src = Path(path)
    if src.is_dir():
        target = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Paraq" / "tessdata"
        try:
            target.mkdir(parents=True, exist_ok=True)
            for f in src.glob("*.traineddata"):
                dst = target / f.name
                if not dst.exists() or dst.stat().st_size != f.stat().st_size:
                    shutil.copyfile(f, dst)
            return str(target)
        except OSError:
            pass
    return path


def _run(cmd: list[str], timeout: float | None = None, input: bytes | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["OMP_THREAD_LIMIT"] = "1"  # one core per page; pages run in parallel instead
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    if input is None:
        kwargs["stdin"] = subprocess.DEVNULL  # a windowed app has no console to inherit
    return subprocess.run(cmd, input=input, capture_output=True, env=env, timeout=timeout, **kwargs)


def user_tessdata_dir() -> Path:
    d = data_dir() / "tessdata"
    d.mkdir(parents=True, exist_ok=True)
    return d


_lang_cache: dict[str, Path | None] | None = None


def available_languages(refresh: bool = False) -> dict[str, Path | None]:
    """Map language code -> tessdata folder (None = Tesseract's own default folder).

    Search order: the user's own folder (drop extra .traineddata files there),
    language data bundled with the app, then the system installation.
    """
    global _lang_cache
    if _lang_cache is not None and not refresh:
        return _lang_cache
    langs: dict[str, Path | None] = {}
    for d in (user_tessdata_dir(), resource_path("tessdata")):
        if d.is_dir():
            for f in sorted(d.glob("*.traineddata")):
                langs.setdefault(f.stem, d)
    tess = find_tesseract()
    if tess:
        try:
            res = _run([tess, "--list-langs"], timeout=20)
            for line in res.stdout.decode("utf-8", "replace").splitlines()[1:]:
                code = line.strip()
                if code and "/" not in code and " " not in code:
                    langs.setdefault(code, None)
        except (OSError, subprocess.SubprocessError):
            pass
    langs.pop("osd", None)
    langs.pop("equ", None)
    _lang_cache = langs
    return langs


def pick_tessdata(langs: list[str]) -> Path | None:
    """One Tesseract run reads one tessdata folder: find a folder holding every language."""
    avail = available_languages()
    missing = [l for l in langs if l not in avail]
    if missing:
        names = ", ".join(CORE_LANGS.get(m, m) for m in missing)
        how = "    brew install tesseract-lang\n\n" if sys.platform == "darwin" else ""
        raise OcrError(
            f"Language data missing for: {names}.\n\n"
            + (f"Install all Tesseract languages with:\n\n{how}or copy" if how else "Copy")
            + f" the .traineddata files (from github.com/tesseract-ocr/tessdata_best) into:\n{user_tessdata_dir()}"
        )
    dirs = {avail[l] for l in langs}
    if len(dirs) == 1:
        return dirs.pop()
    # Languages are spread over several folders: gather them in the user folder.
    target = user_tessdata_dir()
    default_dir = _system_tessdata_dir()
    for l in langs:
        src_dir = avail[l] if avail[l] is not None else default_dir
        if src_dir is None:
            raise OcrError("Could not locate Tesseract's language folder.")
        src = Path(src_dir) / f"{l}.traineddata"
        dst = target / f"{l}.traineddata"
        if not dst.exists():
            shutil.copyfile(src, dst)
    available_languages(refresh=True)
    return target


def _system_tessdata_dir() -> Path | None:
    tess = find_tesseract()
    if not tess:
        return None
    try:
        res = _run([tess, "--list-langs"], timeout=20)
        first = res.stdout.decode("utf-8", "replace").splitlines()[0]
        if '"' in first:
            return Path(first.split('"')[1])
    except (OSError, subprocess.SubprocessError, IndexError):
        pass
    return None


# ---------------------------------------------------------------------------
# Running Tesseract
# ---------------------------------------------------------------------------

_last_stderr = threading.local()


def last_stderr() -> str:
    return getattr(_last_stderr, "value", "")


def tesseract_version() -> str:
    tess = find_tesseract()
    if not tess:
        return "not installed"
    try:
        out = _run([tess, "--version"], timeout=20)
        text = (out.stdout or out.stderr).decode("utf-8", "replace").strip().splitlines()
        return text[0] if text else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _ocr_via_file(image: bytes, langs: list[str], tessdata: Path | None) -> list[TessWord]:
    """Fallback: hand the page to Tesseract as a temporary file instead of through stdin."""
    import tempfile

    fd, path = tempfile.mkstemp(prefix="paraq_", suffix=".pnm")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(image)
        return run_tesseract(path, langs, tessdata)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@dataclass
class TessWord:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    conf: float
    line: tuple[int, int, int]


def run_tesseract(image: bytes | str, langs: list[str], tessdata: Path | None, psm: int = 3) -> list[TessWord]:
    """OCR one page image; coordinates are pixels of that image.

    `image` is PNM/PNG data (sent through stdin) or the path of an image file.
    The text Tesseract printed on stderr is kept in run_tesseract.last_stderr.
    """
    tess = find_tesseract()
    if not tess:
        raise OcrError(INSTALL_HINT)
    source = "stdin" if isinstance(image, bytes) else tess_path(image)
    cmd = [tess, source, "stdout", "-l", "+".join(langs), "--psm", str(psm), "--dpi", "300"]
    if tessdata is not None:
        cmd += ["--tessdata-dir", tess_path(tessdata)]
    # Set the option directly rather than through the "tsv" config file: that file lives in
    # tessdata/configs, which a folder holding only .traineddata files (ours) does not have.
    cmd += ["-c", "tessedit_create_tsv=1"]
    res = _run(cmd, timeout=900, input=image if isinstance(image, bytes) else None)
    stderr = res.stderr.decode("utf-8", "replace").strip()
    _last_stderr.value = stderr
    if res.returncode != 0:
        log.error("tesseract failed (%s): %s\n%s", res.returncode, " ".join(cmd), stderr)
        msg = stderr.splitlines()
        raise OcrError("Tesseract failed: " + (msg[-1] if msg else f"exit code {res.returncode}"))
    words: list[TessWord] = []
    reader = csv.reader(io.StringIO(res.stdout.decode("utf-8", "replace")), delimiter="\t", quoting=csv.QUOTE_NONE)
    for row in reader:
        if len(row) < 12 or row[0] != "5":
            continue
        text = row[11].strip()
        if not text:
            continue
        try:
            x, y, w, h = (float(row[i]) for i in (6, 7, 8, 9))
            conf = float(row[10])
        except ValueError:
            continue
        if w <= 0 or h <= 0:
            continue
        words.append(TessWord(text, x, y, x + w, y + h, conf, (int(row[2]), int(row[3]), int(row[4]))))
    return words


# ---------------------------------------------------------------------------
# Second look: text Tesseract skipped (margin headings, notes, small captions)
# ---------------------------------------------------------------------------

def _pnm_array(data: bytes):
    import numpy as np

    tokens: list[bytes] = []
    i = 0
    while len(tokens) < 4:
        while data[i : i + 1].isspace():
            i += 1
        if data[i : i + 1] == b"#":
            while data[i : i + 1] not in (b"\n", b""):
                i += 1
            continue
        j = i
        while not data[j : j + 1].isspace():
            j += 1
        tokens.append(data[i:j])
        i = j
    i += 1  # the single whitespace after maxval
    w, h = int(tokens[1]), int(tokens[2])
    return np.frombuffer(data, dtype=np.uint8, count=w * h, offset=i).reshape(h, w)


def _components(mask) -> list[tuple[int, int, int, int]]:
    """Bounding boxes (row0, col0, row1, col1, inclusive) of 4-connected True areas."""
    import numpy as np

    seen = np.zeros_like(mask)
    rows, cols = mask.shape
    boxes = []
    for r0, c0 in zip(*np.nonzero(mask)):
        if seen[r0, c0]:
            continue
        stack = [(r0, c0)]
        seen[r0, c0] = True
        top, left, bottom, right = r0, c0, r0, c0
        while stack:
            r, c = stack.pop()
            top, bottom, left, right = min(top, r), max(bottom, r), min(left, c), max(right, c)
            for rr, cc in ((r + 1, c), (r - 1, c), (r, c + 1), (r, c - 1)):
                if 0 <= rr < rows and 0 <= cc < cols and mask[rr, cc] and not seen[rr, cc]:
                    seen[rr, cc] = True
                    stack.append((rr, cc))
        boxes.append((int(top), int(left), int(bottom), int(right)))
    return boxes


def rescue_missed_text(image: bytes, words: list[TessWord], langs: list[str], tessdata, max_regions: int = 12) -> list[TessWord]:
    """Find printed areas no confidently recognised word covers, OCR each on its own, keep what
    reads well. Returns the page's words with those additions (and without the low-confidence
    junk the first pass produced in those places).

    Tesseract's page analysis sometimes drops small text standing apart, such as a
    section title set in the margin. Read in isolation it comes out fine. Maps, photos,
    stamps and page-edge shadows are skipped by size, position and confidence.
    """
    import numpy as np

    if len(words) < 5:
        return words
    try:
        arr = _pnm_array(image)
    except Exception:  # noqa: BLE001
        return words
    h, w = arr.shape
    med_h = median(wd.y1 - wd.y0 for wd in words)
    cell = max(3, int(med_h / 4))
    paper = float(np.percentile(arr, 85))
    ink = arr < paper * 0.55
    gh, gw = h // cell, w // cell
    if gh < 4 or gw < 4:
        return words
    grid = ink[: gh * cell, : gw * cell].reshape(gh, cell, gw, cell).mean(axis=(1, 3)) > 0.04
    covered = np.zeros_like(grid)
    pad = 0.6 * med_h
    # Junk read with low confidence does not count as covered: that is exactly where a
    # margin heading ends up when the page analysis gets it wrong.
    for wd in (wd for wd in words if wd.conf >= 40):
        y0, y1 = int(max(0, (wd.y0 - pad) // cell)), int(min(gh - 1, (wd.y1 + pad) // cell))
        x0, x1 = int(max(0, (wd.x0 - pad) // cell)), int(min(gw - 1, (wd.x1 + pad) // cell))
        covered[y0 : y1 + 1, x0 : x1 + 1] = True
    rest = grid & ~covered
    if not rest.any():
        return words
    # join letters into words and words into lines
    joined = rest.copy()
    for d in range(1, int(med_h / cell) + 2):
        joined[:, d:] |= rest[:, :-d]
        joined[:, :-d] |= rest[:, d:]
    joined[1:, :] |= joined[:-1, :].copy()
    rescued: list[TessWord] = []
    replaced: list[tuple[int, int, int, int]] = []
    regions = 0
    edge_y, edge_x = 0.015 * h, 0.015 * w
    for r0, c0, r1, c1 in sorted(_components(joined), key=lambda b: (b[0], b[1])):
        y0, x0, y1, x1 = r0 * cell, c0 * cell, (r1 + 1) * cell, (c1 + 1) * cell
        bw, bh = x1 - x0, y1 - y0
        if bw < 1.5 * med_h or bh < 0.6 * med_h or bh > 8 * med_h or bw * bh > 0.06 * w * h:
            continue  # specks, rules, pictures
        if y0 < edge_y or x0 < edge_x or y1 > h - edge_y or x1 > w - edge_x:
            continue  # shadows and dirt along the edge of the scan
        density = float(ink[y0:y1, x0:x1].mean())
        if not 0.03 <= density <= 0.5:
            continue
        p = int(0.4 * med_h)
        cy0, cy1, cx0, cx1 = max(0, y0 - p), min(h, y1 + p), max(0, x0 - p), min(w, x1 + p)
        crop = np.ascontiguousarray(arr[cy0:cy1, cx0:cx1])
        pnm = f"P5\n{crop.shape[1]} {crop.shape[0]}\n255\n".encode() + crop.tobytes()
        try:
            found = run_tesseract(pnm, langs, tessdata, psm=6)
        except OcrError:
            continue
        good = [f for f in found if f.conf >= 70 and sum(c.isalpha() for c in f.text) >= 2]
        if not any(f.conf >= 80 and sum(c.isalpha() for c in f.text) >= 3 for f in good):
            continue
        regions += 1
        replaced.append((cx0, cy0, cx1, cy1))
        for f in good:
            rescued.append(
                TessWord(f.text, f.x0 + cx0, f.y0 + cy0, f.x1 + cx0, f.y1 + cy0, f.conf, (100000 + regions, f.line[1], f.line[2]))
            )
        if regions >= max_regions:
            break
    if not rescued:
        return words

    def inside_new(wd: TessWord) -> bool:
        return wd.conf < 40 and any(wd.x0 < x1 and wd.x1 > x0 and wd.y0 < y1 and wd.y1 > y0 for x0, y0, x1, y1 in replaced)

    kept = [wd for wd in words if not inside_new(wd)]
    log.info("second look found %d words in %d places Tesseract had skipped", len(rescued), regions)
    return kept + rescued


def _russian_lines(words: list[TessWord]) -> list[list[TessWord]] | None:
    """The page's words as lines in reading order, if the page is in Russian."""
    from .translate import detect_language

    if not words or detect_language(" ".join(w.text for w in words)) != "rus_Cyrl":
        return None
    lines: dict[tuple, list[TessWord]] = defaultdict(list)
    for w in words:
        lines[w.line].append(w)
    return [sorted(ws, key=lambda w: w.x0) for _k, ws in sorted(lines.items(), key=lambda kv: kv[0])]


def postprocess_pages(pages: dict[int, list[TessWord]]) -> int:
    """Dictionary clean-up of all Russian pages of a document (see ocrfix).

    Done once all pages are read: a word the dictionary does not know but that is spelled
    the same way three or more times in the document is a real term (a name, an
    archaeological culture...), not an OCR error, and is left alone.
    """
    from collections import Counter

    from . import ocrfix

    if not ocrfix.available():
        return 0
    per_page = {i: _russian_lines(ws) for i, ws in pages.items()}
    counts = Counter(w for lines in per_page.values() if lines for w in ocrfix.unknown_words(lines))
    protected = frozenset(w for w, n in counts.items() if n >= 3)
    changed = 0
    for lines in per_page.values():
        if lines:
            changed += ocrfix.fix_lines(lines, protected)
    return changed


# ---------------------------------------------------------------------------
# Page analysis
# ---------------------------------------------------------------------------

@dataclass
class PageInfo:
    index: int
    status: str  # none | good | damaged
    reason: str = ""
    scan: bool = False


def _scan_image(page: pymupdf.Page):
    """Largest image covering at least half the page, as (pixel long side) or 0."""
    area = abs(page.rect) or 1.0
    best = 0
    try:
        infos = page.get_image_info()
    except Exception:
        infos = []
    for im in infos:
        r = pymupdf.Rect(im["bbox"]) & page.rect
        if abs(r) >= 0.5 * area:
            best = max(best, im.get("width", 0), im.get("height", 0))
    return best


def analyze_page(page: pymupdf.Page) -> PageInfo:
    text = page.get_text("text", flags=_TEXT_FLAGS)
    q = assess(text)
    scan = _scan_image(page) > 0
    status, reason = q.status, q.reason
    if status == "good" and scan and q.chars < 120:
        status, reason = "none", "only a few characters over a scanned image"
    return PageInfo(page.number, status, reason, scan)


OCR_KEY = "ParaqOCR"  # in the PDF catalog: pages Paraq has already OCRed, e.g. "0-27,30"


def _encode_pages(pages: set[int]) -> str:
    out, run = [], []
    for p in sorted(pages):
        if run and p == run[-1] + 1:
            run.append(p)
        else:
            if run:
                out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
            run = [p]
    if run:
        out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
    return ",".join(out)


def _decode_pages(text: str) -> set[int]:
    pages: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        try:
            if "-" in part:
                a, b = part.split("-", 1)
                pages.update(range(int(a), int(b) + 1))
            elif part:
                pages.add(int(part))
        except ValueError:
            continue
    return pages


def ocred_pages(doc: pymupdf.Document) -> set[int]:
    """Pages an earlier Paraq run has already OCRed (so blank or picture pages are not redone)."""
    try:
        kind, value = doc.xref_get_key(doc.pdf_catalog(), OCR_KEY)
    except Exception:  # noqa: BLE001
        return set()
    if kind != "string":
        return set()
    return {p for p in _decode_pages(value) if 0 <= p < doc.page_count}


def _mark_ocred(doc: pymupdf.Document, pages: set[int]) -> None:
    try:
        value = _encode_pages(ocred_pages(doc) | pages)
        doc.xref_set_key(doc.pdf_catalog(), OCR_KEY, pymupdf.get_pdf_str(value))
    except Exception:  # noqa: BLE001
        log.warning("could not record the OCRed pages in the PDF")


def analyze_document(doc: pymupdf.Document, progress=None, cancel: threading.Event | None = None) -> list[PageInfo]:
    infos = []
    n = doc.page_count
    with FITZ_LOCK:
        done_before = ocred_pages(doc)
    for i in range(n):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        with FITZ_LOCK:
            info = analyze_page(doc[i])
        if i in done_before and info.status != "good":
            # OCRed by Paraq before and still no usable text: a blank page, a photo, a map.
            info.status, info.reason = "ocred", "already OCRed; no text found on it"
        infos.append(info)
        if progress and (i % 10 == 0 or i == n - 1):
            progress("Checking text layer", i + 1, n)
    return infos


def pages_to_ocr(infos: list[PageInfo], mode: str) -> list[int]:
    if mode == "force":
        return [p.index for p in infos]
    return [p.index for p in infos if p.status in ("none", "damaged")]


# ---------------------------------------------------------------------------
# Building the searchable PDF
# ---------------------------------------------------------------------------

def _ocr_zoom(page: pymupdf.Page, dpi: int) -> float:
    """Scale at which a page is handed to Tesseract.

    Scans are judged by their own pixels, not by the page size in the PDF: Tesseract
    reads best when the text is about as large as on an A4 page at 300 dpi, so small
    scans (a book page of 1700 px, say) are enlarged up to 2.2 times. In tests on a
    1957 book this cut OCR errors by a quarter. Pages that are not scans are
    rendered at `dpi`.
    """
    long_pt = max(page.rect.width, page.rect.height) or 1.0
    native = _scan_image(page)
    if native:
        target = min(max(native, 3300), native * 2.2)
    else:
        target = long_pt * dpi / 72.0
    return min(target, 7000) / long_pt


def _has_visible_text(page: pymupdf.Page) -> bool:
    try:
        for span in page.get_texttrace():
            if span.get("type", 0) != 3 and span.get("opacity", 1) > 0 and span.get("chars"):
                return True
    except Exception:
        return True
    return False


def _strip_text(page: pymupdf.Page) -> None:
    """Remove every text object from the page but keep images and line art."""
    page.add_redact_annot(page.rect, fill=False, cross_out=False)
    page.apply_redactions(
        images=pymupdf.PDF_REDACT_IMAGE_NONE,
        graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
        text=pymupdf.PDF_REDACT_TEXT_REMOVE,
    )


def _rasterize(doc: pymupdf.Document, index: int, zoom: float) -> None:
    """Replace a page by a picture of itself (used when visible text is itself garbled)."""
    page = doc[index]
    rect = page.rect
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    data = pix.tobytes("jpg", jpg_quality=88)
    new = doc.new_page(pno=index + 1, width=rect.width, height=rect.height)
    new.insert_image(new.rect, stream=data)
    doc.delete_page(index)


def _prepare_page(doc: pymupdf.Document, index: int, info: PageInfo, dpi: int) -> None:
    """Get rid of the old text layer of a page that is about to be OCRed."""
    page = doc[index]
    if page.rotation:
        page.remove_rotation()
        page = doc[index]
    if not page.get_text("text", flags=_TEXT_FLAGS).strip():
        return
    if info.scan or not _has_visible_text(page):
        _strip_text(page)
    else:
        _rasterize(doc, index, _ocr_zoom(page, dpi))


def _clean_word(text: str, font: pymupdf.Font) -> str:
    return "".join(c for c in text if c >= " " and font.has_glyph(ord(c)))


def write_text_layer(page: pymupdf.Page, words: list[TessWord], zoom: float, font: pymupdf.Font) -> int:
    """Write OCR words as invisible text, each word stretched onto its place in the scan."""
    if not words:
        return 0
    ox, oy = page.rect.x0, page.rect.y0
    page_h = page.rect.height
    lines: dict[tuple, list[TessWord]] = defaultdict(list)
    for w in words:
        if w.conf < 20 and not any(c.isalnum() for c in w.text):
            continue  # specks and rules read as punctuation
        if (w.y1 - w.y0) / zoom > 0.25 * page_h:
            continue
        lines[w.line].append(w)
    if not lines:
        return 0
    span = font.ascender - font.descender
    # Scans are rarely straight: measure the skew and place every word on its own slanted line.
    all_h = [w.y1 - w.y0 for ws in lines.values() for w in ws]
    skew = page_skew(
        [[((w.x0 + w.x1) / 2, (w.y0 + w.y1) / 2) for w in ws] for ws in lines.values()],
        8.0 * median(all_h),
    )

    def flat(w: TessWord) -> tuple[float, float]:
        """Top and bottom of a word as if the page were straight (pixels, at x = 0).

        The upright box around a slanted word is taller than the word by width * |skew|.
        """
        pad = min(abs(skew) * (w.x1 - w.x0), 0.6 * (w.y1 - w.y0)) / 2
        shift = skew * (w.x0 + w.x1) / 2
        return w.y0 + pad - shift, w.y1 - pad - shift

    # OCR line boxes grow and shrink with ascenders, descenders and specks. Lines of
    # one text block are given a common size so that selection bands look even.
    raw_h: dict[tuple, float] = {}
    mid: dict[tuple, float] = {}
    by_block: dict[int, list[float]] = defaultdict(list)
    for key, ws in lines.items():
        spans = [flat(w) for w in ws]
        union = max(b for _t, b in spans) - min(t for t, _b in spans)
        med = median(b - t for t, b in spans)
        raw_h[key] = min(union, 2.2 * med) / zoom
        mid[key] = median((t + b) / 2 for t, b in spans)
        by_block[key[0]].append(raw_h[key])
    block_h = {b: median(hs) for b, hs in by_block.items()}
    shape = page.new_shape()
    count = 0
    for key, ws in lines.items():
        ws.sort(key=lambda w: w.x0)
        typical = block_h[key[0]]
        h = min(max(raw_h[key], 0.85 * typical), 1.15 * typical)
        if h < 1.0:
            continue
        fs = h / span
        mid0 = mid[key]
        for i, w in enumerate(ws):
            text = _clean_word(w.text, font)
            if not text:
                continue
            natural = font.text_length(text, fontsize=fs)
            width = (w.x1 - w.x0) / zoom
            if natural <= 0 or width <= 0:
                continue
            center = (mid0 + skew * (w.x0 + w.x1) / 2) / zoom
            base = oy + center + h / 2 + font.descender * fs
            pt = pymupdf.Point(ox + w.x0 / zoom, base)
            shape.insert_text(
                pt,
                text + (" " if i < len(ws) - 1 else ""),
                fontname="ParaqOCR",
                fontfile=str(FONT_PATH),
                fontsize=fs,
                render_mode=3,
                morph=(pt, pymupdf.Matrix(width / natural, 1)),
            )
            count += 1
    shape.commit(overlay=True)
    return count


def default_workers() -> int:
    n = os.cpu_count() or 2
    return max(1, n - 1) if n <= 4 else min(n - 2, 6)


def output_path_for(sources: list[Path], output_dir: str = "") -> Path:
    first = Path(sources[0])
    stem = first.stem
    if stem.endswith(OCR_SUFFIX):
        stem = stem[: -len(OCR_SUFFIX)]
    folder = Path(output_dir) if output_dir else first.parent
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / f".paraq_write_test_{os.getpid()}"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError:
        folder = Path.home() / "Documents" / "Paraq"
        folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{stem}{OCR_SUFFIX}.pdf"


def _move_into_place(tmp: Path, out_path: Path) -> Path:
    """Rename the finished file; if the target is locked (open in another program on
    Windows), retry briefly and then fall back to a new name."""
    import time

    for _ in range(6):
        try:
            os.replace(tmp, out_path)
            return out_path
        except PermissionError:
            time.sleep(0.5)
    for k in range(2, 100):
        alt = out_path.with_name(f"{out_path.stem}-{k}{out_path.suffix}")
        try:
            os.replace(tmp, alt)
            return alt
        except PermissionError:
            continue
    raise OcrError(f"Could not write {out_path.name}: the file is open in another program.")


@dataclass
class OcrResult:
    output: Path | None  # None when nothing had to be written
    pages: int
    ocred: int
    kept: int
    damaged: int
    words: int


def process_document(
    sources: list[Path],
    out_path: Path,
    langs: list[str],
    mode: str = "auto",
    dpi: int = 300,
    workers: int = 0,
    progress=None,
    cancel: threading.Event | None = None,
    fix_russian: bool = True,
    second_look: bool = True,
) -> OcrResult:
    """OCR one document (a PDF, or one or more images) into a searchable PDF at out_path.

    fix_russian: dictionary clean-up of typical OCR letter confusions on Russian pages.
    second_look: OCR printed areas Tesseract skipped (margin headings, notes) on their own.
    """
    sources = [Path(s) for s in sources]
    if not find_tesseract():
        raise OcrError(INSTALL_HINT)
    if not langs:
        raise OcrError("Choose at least one OCR language.")
    tessdata = pick_tessdata(langs)
    workers = workers or default_workers()
    is_pdf = len(sources) == 1 and sources[0].suffix.lower() in PDF_EXT

    def check():
        if cancel is not None and cancel.is_set():
            raise Cancelled()

    doc = open_as_pdf(sources)
    try:
        infos = analyze_document(doc, progress, cancel)
        todo = pages_to_ocr(infos, mode)
        damaged = sum(1 for p in infos if p.status == "damaged")
        if not todo:
            if is_pdf:
                return OcrResult(None, len(infos), 0, len(infos), 0, 0)
        font = pymupdf.Font(fontfile=str(FONT_PATH))
        total = len(todo)
        done = 0
        n_words = 0
        if progress:
            progress("Recognising text", 0, total)
        log.info(
            "OCR %s: %d of %d pages, languages %s, %s, tessdata %s, %d workers",
            sources[0].name, total, len(infos), "+".join(langs), tesseract_version(), tessdata, workers,
        )

        fixed = [0]
        results: dict[int, tuple[float, list[TessWord]]] = {}

        def finish(index: int, zoom: float, words: list[TessWord]):
            nonlocal done
            results[index] = (zoom, words)  # written once every page is read (see postprocess_pages)
            done += 1
            if progress:
                progress("Recognising text", done, total)

        problems: list[str] = []
        use_files = [False]  # switch every later page to files once stdin is seen to fail

        def job(image: bytes):
            words = _read_page(image)
            if second_look and words:
                words = rescue_missed_text(image, words, langs, tessdata)
            return words

        def _read_page(image: bytes):
            if not use_files[0]:
                words = run_tesseract(image, langs, tessdata)
                if words:
                    return words
                said = last_stderr()
                # Tesseract either found an empty page or could not read the image from stdin.
                log.warning("no words via stdin; retrying with a file. Tesseract said: %s", said or "(nothing)")
            words = _ocr_via_file(image, langs, tessdata)
            if words and not use_files[0]:
                log.warning("reading pages through stdin fails with this Tesseract; using files from now on")
                use_files[0] = True
            if not words and last_stderr():
                problems.append(last_stderr())
            return words

        pending: dict = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            try:
                for index in todo:
                    check()
                    with FITZ_LOCK:
                        _prepare_page(doc, index, infos[index], dpi)
                        page = doc[index]
                        zoom = _ocr_zoom(page, dpi)
                        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY, alpha=False)
                        image = pix.tobytes("pnm")
                        del pix
                    pending[pool.submit(job, image)] = (index, zoom)
                    del image
                    while len(pending) >= workers * 2:
                        finished, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                        for f in finished:
                            idx, z = pending.pop(f)
                            finish(idx, z, f.result())
                        check()
                while pending:
                    finished, _ = wait(list(pending), timeout=0.5, return_when=FIRST_COMPLETED)
                    for f in finished:
                        idx, z = pending.pop(f)
                        finish(idx, z, f.result())
                    check()
            except BaseException:
                for f in pending:
                    f.cancel()
                raise

        if fix_russian and results:
            if progress:
                progress("Checking spelling", total, total)
            try:
                fixed[0] = postprocess_pages({i: ws for i, (_z, ws) in results.items()})
            except Exception:  # noqa: BLE001 - a clean-up problem must never lose the text
                log.exception("OCR clean-up failed")
        with FITZ_LOCK:
            for index in sorted(results):
                zoom, words = results[index]
                n_words += write_text_layer(doc[index], words, zoom, font)
        if total and n_words == 0:
            detail = problems[0].splitlines()[-1] if problems else "Tesseract reported no error."
            log.error("OCR found no words on %d pages. Tesseract: %s. Messages: %s", total, tesseract_version(), problems[:3])
            raise OcrError(
                f"OCR found no text on any of the {total} pages.\n\n"
                f"Tesseract: {tesseract_version()}\nIts last message: {detail}\n\n"
                "Help > Open Log Folder has the details (paraq.log)."
            )
        log.info("OCR %s: %d words, %d corrected by the dictionary", sources[0].name, n_words, fixed[0])
        if progress:
            progress("Saving searchable PDF", total, total)
        out_path = Path(out_path)
        tmp_out = out_path.with_name(out_path.name + ".part")
        with FITZ_LOCK:
            _mark_ocred(doc, set(todo))
            try:
                doc.subset_fonts()
            except Exception:
                pass
            doc.save(str(tmp_out), garbage=3, deflate=True)
        out_path = _move_into_place(tmp_out, out_path)
        return OcrResult(out_path, len(infos), total, len(infos) - total, damaged, n_words)
    finally:
        with FITZ_LOCK:
            doc.close()


def main(argv: list[str] | None = None) -> int:
    """Command-line use: python -m paraq.ocr input.pdf [output.pdf] [--langs rus+kaz] [--force]"""
    import argparse

    ap = argparse.ArgumentParser(description="OCR a PDF or image into a searchable PDF")
    ap.add_argument("input")
    ap.add_argument("output", nargs="?")
    ap.add_argument("--langs", default="rus+kaz+eng+tur")
    ap.add_argument("--force", action="store_true", help="re-OCR every page")
    ap.add_argument("--dpi", type=int, default=300)
    args = ap.parse_args(argv)
    src = Path(args.input)
    out = Path(args.output) if args.output else output_path_for([src])
    try:
        from .ocrfix import install_hyphen_judge

        install_hyphen_judge()
    except Exception:  # noqa: BLE001
        pass

    def progress(stage, done, total):
        print(f"\r{stage}: {done}/{total}   ", end="", file=sys.stderr, flush=True)

    res = process_document([src], out, args.langs.split("+"), "force" if args.force else "auto", args.dpi, progress=progress)
    print(file=sys.stderr)
    if res.output is None:
        print("All pages already have a usable text layer; nothing to do.")
    else:
        print(f"{res.output}: {res.ocred} of {res.pages} pages recognised ({res.damaged} had a damaged text layer)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
