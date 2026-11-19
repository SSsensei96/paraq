"""Thin helpers around PyMuPDF. MuPDF is not thread-safe, so every call goes through FITZ_LOCK."""

from __future__ import annotations

import io
import threading
from pathlib import Path

import pymupdf

from .layout import PageLayout, Word, analyze

FITZ_LOCK = threading.RLock()

PDF_EXT = {".pdf"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".jp2", ".jpx", ".webp", ".pnm", ".pgm", ".ppm"}
SUPPORTED_EXT = PDF_EXT | IMAGE_EXT

_TEXT_FLAGS = pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP


def natural_key(name: str):
    import re

    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def _image_to_pdf_bytes(path: Path) -> bytes:
    """Convert a raster image (possibly multi-page TIFF) into PDF bytes."""
    try:
        with pymupdf.open(str(path)) as img:
            return img.convert_to_pdf()
    except Exception:
        # Formats MuPDF cannot read directly (e.g. WebP on some builds) go through Pillow.
        from PIL import Image

        im = Image.open(path)
        frames = []
        try:
            while True:
                frames.append(im.convert("RGB").copy())
                im.seek(im.tell() + 1)
        except EOFError:
            pass
        out = pymupdf.open()
        for fr in frames:
            buf = io.BytesIO()
            fr.save(buf, "PNG")
            with pymupdf.open(stream=buf.getvalue(), filetype="png") as one:
                with pymupdf.open("pdf", one.convert_to_pdf()) as pdf:
                    out.insert_pdf(pdf)
        data = out.tobytes()
        out.close()
        return data


def open_as_pdf(paths: list[Path] | Path) -> pymupdf.Document:
    """Open a PDF, or build an in-memory PDF from one or several images."""
    if isinstance(paths, (str, Path)):
        paths = [Path(paths)]
    paths = [Path(p) for p in paths]
    with FITZ_LOCK:
        if len(paths) == 1 and paths[0].suffix.lower() in PDF_EXT:
            doc = pymupdf.open(str(paths[0]))
            if doc.needs_pass:
                doc.close()
                raise ValueError("This PDF is password-protected.")
            return doc
        out = pymupdf.open()
        for p in paths:
            if p.suffix.lower() in PDF_EXT:
                with pymupdf.open(str(p)) as src:
                    out.insert_pdf(src)
            else:
                with pymupdf.open("pdf", _image_to_pdf_bytes(p)) as src:
                    out.insert_pdf(src)
        return out


def page_words(page: pymupdf.Page) -> list[Word]:
    """Words of a page with per-character boundaries, taken from the PDF text layer."""
    words: list[Word] = []
    clip = page.rect
    data = page.get_text("rawdict", flags=_TEXT_FLAGS)
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            dx, dy = line.get("dir", (1.0, 0.0))
            if abs(dy) > 0.3 or dx < 0:  # skip vertical / upside-down text
                continue
            cur_text: list[str] = []
            cur_edges: list[float] = []
            box = [0.0, 0.0, 0.0, 0.0]

            def flush():
                if cur_text:
                    words.append(Word("".join(cur_text), box[0], box[1], box[2], box[3], list(cur_edges)))
                cur_text.clear()
                cur_edges.clear()

            for span in line.get("spans", []):
                for ch in span.get("chars", []):
                    c = ch["c"]
                    x0, y0, x1, y1 = ch["bbox"]
                    if c.isspace() or c == " ":
                        flush()
                        continue
                    if x1 < clip.x0 or x0 > clip.x1 or y1 < clip.y0 or y0 > clip.y1:
                        continue
                    if cur_text and x0 < cur_edges[-1] - 0.6 * max(y1 - y0, 1.0):
                        flush()  # text jumped backwards: a new run
                    if not cur_text:
                        box[:] = [x0, y0, x1, y1]
                        cur_edges.append(x0)
                    else:
                        box[1] = min(box[1], y0)
                        box[2] = max(box[2], x1)
                        box[3] = max(box[3], y1)
                    cur_text.append(c)
                    cur_edges.append(max(x1, cur_edges[-1]))
            flush()
    return words


def page_layout(page: pymupdf.Page) -> PageLayout:
    r = page.rect
    return analyze(page_words(page), r.width, r.height)
