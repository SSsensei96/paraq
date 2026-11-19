"""Background jobs. Each job is a QThread; the main window runs them one after another."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf
from PySide6.QtCore import QThread, Signal

from .. import export as exporter
from ..config import data_dir
from ..ocr import Cancelled, OcrError, analyze_document, output_path_for, pages_to_ocr, process_document
from ..pdfutil import FITZ_LOCK, PDF_EXT, open_as_pdf, page_layout
from ..translate import (
    MODELS,
    TranslationError,
    TranslationStore,
    engine_of,
    ensure_ollama,
    install_model,
    load_translator,
    model_installed,
)

# A model choice: (key from MODELS, custom NLLB folder or "")
Model = tuple[str, str]


def _load(model: Model):
    key, custom = model
    if engine_of(key) == "ollama" and not custom:
        ensure_ollama()  # starts the Ollama server if it is installed but not running
    if not model_installed(key, custom):
        raise TranslationError("The translation model is not installed yet.")
    return load_translator(key, custom)


@dataclass
class DocItem:
    sources: list[str]
    title: str
    view_path: str = ""
    pages: int = 0
    need_ocr: int = 0
    damaged: int = 0
    state: str = "checking"  # checking | needs_ocr | ready | error
    note: str = ""
    busy: str = ""  # progress text while a job works on this document
    uid: int = field(default_factory=lambda: DocItem._next_uid())

    _counter = 0

    @staticmethod
    def _next_uid() -> int:
        DocItem._counter += 1
        return DocItem._counter

    def status_text(self) -> str:
        if self.busy:
            return self.busy
        if self.state == "checking":
            return "Checking…"
        if self.state == "error":
            return self.note or "Could not be opened"
        if self.state == "needs_ocr":
            text = f"{self.need_ocr} of {self.pages} pages need OCR"
            if self.damaged:
                text += f" · {self.damaged} with a damaged text layer"
            return text
        return f"Ready · {self.pages} page{'s' if self.pages != 1 else ''}"


class Job(QThread):
    # float, not int: download sizes in bytes exceed what a Qt int can carry (2.1 GB)
    progress = Signal(str, float, float)
    failed = Signal(str)
    label = "Working"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cancel = threading.Event()

    def report(self, stage: str, done: float, total: float) -> None:
        self.progress.emit(stage, float(done), float(total))

    def work(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def run(self) -> None:
        try:
            self.work()
        except Cancelled:
            pass
        except (OcrError, TranslationError, ValueError) as e:
            logging.getLogger("paraq").warning("%s: %s", self.label, e)
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001 - surface unexpected errors to the user
            logging.getLogger("paraq").error("%s failed\n%s", self.label, traceback.format_exc())
            self.failed.emit(f"{type(e).__name__}: {e}")


def _cache_pdf_for(sources: list[str]) -> Path:
    digest = hashlib.sha1("\n".join(sources).encode("utf-8")).hexdigest()[:20]
    folder = data_dir() / "cache"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{digest}.pdf"


class PrepareJob(Job):
    """Open newly added documents and check which pages need OCR."""

    itemReady = Signal(int, dict)
    label = "Checking documents"

    def __init__(self, items: list[DocItem], output_dir: str, parent=None):
        super().__init__(parent)
        self.items = [(it.uid, list(it.sources)) for it in items]
        self.output_dir = output_dir

    def work(self) -> None:
        for uid, sources in self.items:
            if self.cancel.is_set():
                return
            try:
                paths = [Path(s) for s in sources]
                missing = [p for p in paths if not p.exists()]
                if missing:
                    raise ValueError(f"File not found: {missing[0].name}")
                is_pdf = len(paths) == 1 and paths[0].suffix.lower() in PDF_EXT
                view = paths[0]
                # Reuse an earlier OCR result if it is newer than the original.
                out = output_path_for(paths, self.output_dir)
                newest = max(p.stat().st_mtime for p in paths)
                if out.exists() and out.resolve() != paths[0].resolve() and out.stat().st_mtime >= newest:
                    view = out
                elif not is_pdf:
                    view = _cache_pdf_for(sources)
                    if not view.exists() or view.stat().st_mtime < newest:
                        doc = open_as_pdf(paths)
                        with FITZ_LOCK:
                            doc.save(str(view), deflate=True)
                            doc.close()
                doc = open_as_pdf(view)
                try:
                    infos = analyze_document(doc, self.report, self.cancel)
                finally:
                    with FITZ_LOCK:
                        doc.close()
                need = len(pages_to_ocr(infos, "auto"))
                self.itemReady.emit(
                    uid,
                    {
                        "view_path": str(view),
                        "pages": len(infos),
                        "need_ocr": need,
                        "damaged": sum(1 for p in infos if p.status == "damaged"),
                        "state": "needs_ocr" if need else "ready",
                    },
                )
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                self.itemReady.emit(uid, {"state": "error", "note": str(e)})


class OcrJob(Job):
    itemStarted = Signal(int)
    itemDone = Signal(int, dict)
    label = "OCR"

    def __init__(
        self, items: list[DocItem], langs: list[str], mode: str, dpi: int, workers: int, output_dir: str,
        fix_russian: bool = True, second_look: bool = True, parent=None,
    ):
        super().__init__(parent)
        self.items = [(it.uid, list(it.sources), it.view_path) for it in items]
        self.langs, self.mode, self.dpi, self.workers, self.output_dir = langs, mode, dpi, workers, output_dir
        self.fix_russian, self.second_look = fix_russian, second_look

    def work(self) -> None:
        for uid, sources, view in self.items:
            if self.cancel.is_set():
                return
            self.itemStarted.emit(uid)
            paths = [Path(s) for s in sources]
            out = output_path_for(paths, self.output_dir)
            if out.resolve() == paths[0].resolve():
                # The original itself is named *_ocr.pdf: never overwrite the source.
                out = out.with_name(out.stem + "_2.pdf")
            res = process_document(
                paths, out, self.langs, self.mode, self.dpi, self.workers, self.report, self.cancel,
                fix_russian=self.fix_russian, second_look=self.second_look,
            )
            self.itemDone.emit(
                uid,
                {
                    "view_path": str(res.output) if res.output else view,
                    "pages": res.pages,
                    "need_ocr": 0,
                    "damaged": 0,
                    "state": "ready",
                    "ocred": res.ocred,
                    "words": res.words,
                },
            )


class DownloadJob(Job):
    label = "Downloading translation model"

    def __init__(self, key: str, parent=None):
        super().__init__(parent)
        self.key = key

    def work(self) -> None:
        install_model(self.key, self.report, self.cancel)


def page_paragraphs(doc: pymupdf.Document, pno: int) -> list[str]:
    with FITZ_LOCK:
        lay = page_layout(doc[pno])
    return [p.text for p in lay.paragraphs() if p.text.strip()]


class TranslateJob(Job):
    pageDone = Signal(int)
    label = "Translating"

    def __init__(self, pdf_path: str, pages: list[int], model: Model, source: str, store: TranslationStore, parent=None):
        super().__init__(parent)
        self.pdf_path, self.pages, self.model, self.source, self.store = pdf_path, pages, model, source, store
        self.wanted: int | None = None  # page the reader is looking at; translated next

    def work(self) -> None:
        self.report("Loading translation model", 0, 0)
        translator = _load(self.model)
        log = logging.getLogger("paraq.translate")
        with FITZ_LOCK:
            doc = pymupdf.open(self.pdf_path)
        try:
            remaining = list(self.pages)
            total = len(remaining)
            n = 0
            read = 0
            last_save = time.time()
            while remaining and not self.cancel.is_set():
                wanted = self.wanted
                pno = wanted if wanted in remaining else remaining[0]
                remaining.remove(pno)
                self.report("Translating", n, total)
                if self.store.get(pno) is None:
                    t0 = time.time()
                    if read and read % 50 == 0:
                        # An open PDF keeps every page it has parsed; reopening it now and
                        # then keeps memory flat on long books.
                        with FITZ_LOCK:
                            doc.close()
                            doc = pymupdf.open(self.pdf_path)
                    read += 1
                    paras = page_paragraphs(doc, pno)
                    results = translator.translate_paragraphs_full(paras, self.source, self.cancel)
                    if self.cancel.is_set():
                        break
                    self.store.put(
                        pno,
                        [
                            {"src": s, "en": en, "lang": lang, "sents": [[a, b, t] for a, b, t in sents]}
                            for s, (en, lang, sents) in zip(paras, results)
                        ],
                    )
                    log.info(
                        "page %d: %d paragraphs, %d characters, %s, %.1f s",
                        pno + 1, len(paras), sum(len(p) for p in paras),
                        ",".join(sorted({l for _e, l, _s in results if l})) or "nothing to translate", time.time() - t0,
                    )
                    if time.time() - last_save > 30 or read % 20 == 0:
                        self.store.save()  # not after every page: the file grows with the book
                        last_save = time.time()
                n += 1
                self.pageDone.emit(pno)
            self.report("Translating", total, total)
        finally:
            self.store.save()
            with FITZ_LOCK:
                doc.close()


class SnippetJob(Job):
    """Translate a piece of selected text."""

    result = Signal(str, str)
    label = "Translating selection"

    def __init__(self, text: str, model: Model, source: str, parent=None):
        super().__init__(parent)
        self.text, self.model, self.source = text, model, source

    def work(self) -> None:
        self.report("Loading translation model", 0, 0)
        translator = _load(self.model)
        paras = [p for p in self.text.split("\n\n") if p.strip()]
        out = translator.translate_paragraphs(paras, self.source, self.cancel)
        self.result.emit(self.text, "\n\n".join(en for en, _lang in out))


class DiagnoseJob(Job):
    """Translate one sample sentence per language and time it."""

    result = Signal(str)
    label = "Testing translation"

    def __init__(self, model: Model, parent=None):
        super().__init__(parent)
        self.model = model

    def work(self) -> None:
        import platform

        from ..translate import SAMPLES

        key, custom = self.model
        name = custom or MODELS.get(key, {}).get("label", key)
        lines = [f"Model: {name}", f"Computer: {platform.platform()}, {os.cpu_count()} cores"]
        if engine_of(key) == "ollama" and not custom:
            from ..translate import ollama_version

            lines.append(f"Ollama version: {ollama_version()}")
        self.report("Loading translation model", 0, 0)
        t0 = time.time()
        translator = _load(self.model)
        lines.append(f"Model loaded in {time.time() - t0:.1f} s")
        for i, (lang, text) in enumerate(SAMPLES.items()):
            self.report("Testing translation", i, len(SAMPLES))
            t0 = time.time()
            (en, detected), = translator.translate_paragraphs([text], "auto")
            mark = "" if detected == lang else f"  (!) detected as {detected}"
            lines.append(f"\n{lang}{mark} - {time.time() - t0:.1f} s\n{text}\n→ {en}")
        report = "\n".join(lines)
        logging.getLogger("paraq.translate").info("translation test\n%s", report)
        self.result.emit(report)


class ExportJob(Job):
    done = Signal(str)
    label = "Exporting"

    def __init__(
        self, kind: str, pdf_path: str, out_path: str, title: str, store: TranslationStore | None,
        model: Model | None = None, source: str = "auto", parent=None,
    ):
        super().__init__(parent)
        self.kind, self.pdf_path, self.out_path, self.title, self.store = kind, pdf_path, out_path, title, store
        self.model, self.source = model, source

    def _cell_translator(self):
        """Table cells and labels are translated while the PDF is built; None if no model is usable."""
        if self.model is None:
            return None
        try:
            translator = _load(self.model)
        except TranslationError as e:
            logging.getLogger("paraq").warning("table cells left untranslated: %s", e)
            return None
        from ..translate import modernize_russian, soften_caps

        def translate_runs(items: list[tuple[str, str]]) -> list[str]:
            out = [""] * len(items)
            by_lang: dict[str, list[int]] = {}
            for i, (_t, lang) in enumerate(items):
                by_lang.setdefault(lang, []).append(i)
            for lang, idx in by_lang.items():
                texts = [items[i][0] for i in idx]
                texts = [soften_caps(modernize_russian(t) if lang == "rus_Cyrl" else t) for t in texts]
                for i, en in zip(idx, translator.translate_sentences(texts, lang)):
                    out[i] = en
            return out

        return translate_runs

    def work(self) -> None:
        out = Path(self.out_path)
        if self.kind in ("pdf_translated", "pdf_side"):
            if self.store is None or self.store.count() == 0:
                raise TranslationError("Nothing is translated yet. Translate the document first.")
            from ..pdfexport import export_translated_pdf

            self.report("Loading translation model", 0, 0)
            res = export_translated_pdf(
                self.pdf_path, dict(self.store.pages), out, side_by_side=self.kind == "pdf_side",
                translate_runs=self._cell_translator(), source=self.source, progress=self.report, cancel=self.cancel,
            )
            logging.getLogger("paraq").info(
                "translated PDF %s: %d of %d pages, %d text areas replaced", out.name, res.translated_pages, res.pages, res.replaced
            )
        elif self.kind in ("text_txt", "text_docx"):
            with FITZ_LOCK:
                doc = pymupdf.open(self.pdf_path)
            try:
                pages = []
                for pno in range(doc.page_count):
                    if self.cancel.is_set():
                        raise Cancelled()
                    pages.append((pno + 1, page_paragraphs(doc, pno)))
                    if pno % 10 == 0:
                        self.report("Collecting text", pno + 1, doc.page_count)
            finally:
                with FITZ_LOCK:
                    doc.close()
            if self.kind == "text_txt":
                exporter.export_txt(out, pages)
            else:
                exporter.export_docx(out, self.title, pages)
        else:
            if self.store is None or self.store.count() == 0:
                raise TranslationError("Nothing is translated yet. Translate the document first.")
            numbers = sorted(int(k) for k in self.store.pages)
            if self.kind == "en_bilingual":
                data = [(n + 1, [(it["src"], it["en"]) for it in self.store.get(n)]) for n in numbers]
                exporter.export_bilingual_docx(out, self.title, data)
            else:
                data = [(n + 1, [it["en"] for it in self.store.get(n)]) for n in numbers]
                if self.kind == "en_txt":
                    exporter.export_txt(out, data)
                else:
                    exporter.export_docx(out, self.title + " (English)", data)
        self.done.emit(str(out))


def reveal_in_file_manager(path: str) -> None:
    import subprocess
    import sys

    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        elif os.name == "nt":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
    except OSError:
        pass


def open_folder(path: str) -> None:
    import subprocess
    import sys

    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        elif os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError:
        pass
