"""Main window: document list, PDF view, text / translation panel."""

from __future__ import annotations

import html
import os
from pathlib import Path

from PySide6.QtCore import QEvent, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QKeySequence, QPalette, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTextBrowser,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import sys

from .. import APP_NAME
from ..config import Settings
from ..config import data_dir
from ..ocr import INSTALL_HINT, available_languages, find_tesseract, output_path_for, user_tessdata_dir
from ..pdfutil import IMAGE_EXT, PDF_EXT, SUPPORTED_EXT, natural_key
from ..layout import word_rects
from ..translate import (
    MODELS,
    OLLAMA_HINT,
    SOURCE_LANGS,
    TranslationStore,
    engine_of,
    find_ollama,
    model_installed,
    model_ready,
    ollama_running,
)
from .dialogs import ModelDialog, OcrDialog, SnippetDialog
from .viewer import PdfView
from .workers import (
    DocItem,
    DownloadJob,
    ExportJob,
    Job,
    OcrJob,
    PrepareJob,
    SnippetJob,
    DiagnoseJob,
    TranslateJob,
    open_folder,
    reveal_in_file_manager,
)

LANG_NAMES = {k: v for k, v in SOURCE_LANGS.items() if k != "auto"}
IS_MAC = sys.platform == "darwin"
FILE_MANAGER = "Finder" if IS_MAC else ("Explorer" if sys.platform.startswith("win") else "File Manager")
COPY_KEY = "⌘C" if IS_MAC else "Ctrl+C"


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings | None = None):
        super().__init__()
        self.settings = settings or Settings()
        self.items: list[DocItem] = []
        self.queue: list[Job] = []
        self.active: Job | None = None
        self.busy_uid: int | None = None
        self.store: TranslationStore | None = None
        # sentence linking for the page shown in the translation panel
        self._links: dict[str, list] = {}  # "para.sentence" -> highlight rects on the page
        self._link_words: dict[str, set[int]] = {}  # "para.sentence" -> ids of its words
        self._anchor_ranges: dict[str, tuple[int, int]] = {}  # "para.sentence" -> characters in the panel
        self._links_page = -1
        self._active_keys: tuple[str, ...] = ()
        self._sel_keys: tuple[str, ...] = ()
        self.setWindowTitle(APP_NAME)
        self.resize(1440, 900)
        self.setAcceptDrops(True)
        self.setUnifiedTitleAndToolBarOnMac(True)
        self._build_ui()
        self._build_actions()
        self._update_actions()
        QTimer.singleShot(0, self._restore_documents)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        self.view = PdfView()
        self.view.confine = bool(self.settings.get("confine_selection"))
        self.view.reflow = bool(self.settings.get("reflow_copy"))
        self.view.show_columns = bool(self.settings.get("show_columns"))
        self.view.pageChanged.connect(self._page_changed)
        self.view.selectionChanged.connect(self._selection_changed)
        self.view.zoomChanged.connect(self._zoom_changed)
        self.view.translateRequested.connect(self.translate_selection)
        self.view.hovered.connect(self._pdf_hovered)
        self.link_on = bool(self.settings.get("link_translation"))

        # left: documents + job progress
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(8, 8, 4, 8)
        title = QLabel("Documents")
        title.setStyleSheet("font-weight: 600;")
        lv.addWidget(title)
        self.list = QListWidget()
        self.list.setWordWrap(True)
        self.list.setSpacing(2)
        self.list.currentRowChanged.connect(self._doc_selected)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._list_menu)
        lv.addWidget(self.list, 1)
        self.job_box = QWidget()
        jv = QVBoxLayout(self.job_box)
        jv.setContentsMargins(0, 4, 0, 0)
        self.job_label = QLabel("")
        self.job_label.setWordWrap(True)
        self.job_bar = QProgressBar()
        self.job_bar.setTextVisible(False)
        self.job_cancel = QPushButton("Cancel")
        self.job_cancel.clicked.connect(self.cancel_job)
        jv.addWidget(self.job_label)
        row = QHBoxLayout()
        row.addWidget(self.job_bar, 1)
        row.addWidget(self.job_cancel)
        jv.addLayout(row)
        self.job_box.hide()
        lv.addWidget(self.job_box)

        # right: text / translation panel
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(4, 8, 8, 8)
        top = QHBoxLayout()
        self.panel_mode = QComboBox()
        self.panel_mode.addItem("English translation", "en")
        self.panel_mode.addItem("Recognised text", "text")
        self.panel_mode.currentIndexChanged.connect(self.refresh_panel)
        top.addWidget(self.panel_mode, 1)
        self.source_lang = QComboBox()
        for code, name in SOURCE_LANGS.items():
            self.source_lang.addItem(("From: " + name) if code != "auto" else "From: auto-detect", code)
        idx = self.source_lang.findData(self.settings.get("translation_source"))
        self.source_lang.setCurrentIndex(max(0, idx))
        self.source_lang.currentIndexChanged.connect(self._source_changed)
        top.addWidget(self.source_lang, 1)
        rv.addLayout(top)
        self.panel = QTextBrowser()
        self.panel.setOpenLinks(False)
        self.panel.setMouseTracking(True)
        self.panel.viewport().setMouseTracking(True)
        self.panel.viewport().installEventFilter(self)
        self.panel.anchorClicked.connect(self._panel_clicked)
        text_color = self.panel.palette().color(QPalette.Text).name()
        self.panel.document().setDefaultStyleSheet(f"a {{ color: {text_color}; text-decoration: none; }}")
        rv.addWidget(self.panel, 1)
        bottom = QHBoxLayout()
        self.btn_page = QPushButton("Translate This Page")
        self.btn_page.clicked.connect(lambda: self.translate("page"))
        self.btn_doc = QPushButton("Translate Whole Document")
        self.btn_doc.clicked.connect(lambda: self.translate("document"))
        bottom.addWidget(self.btn_page)
        bottom.addWidget(self.btn_doc)
        rv.addLayout(bottom)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(left)
        split.addWidget(self.view)
        split.addWidget(right)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 0)
        split.setSizes([250, 760, 430])
        self.setCentralWidget(split)
        self.statusBar().showMessage("Ready")

    def _build_actions(self) -> None:
        def act(text, slot, shortcut=None, checkable=False, checked=False):
            a = QAction(text, self)
            if shortcut is not None:
                a.setShortcut(shortcut)
            if checkable:
                a.setCheckable(True)
                a.setChecked(checked)
                a.toggled.connect(slot)
            else:
                a.triggered.connect(slot)
            return a

        self.a_add = act("Add Files…", self.add_files_dialog, QKeySequence.Open)
        self.a_add_folder = act("Add Folder…", self.add_folder_dialog)
        self.a_remove = act("Remove from List", self.remove_current)
        self.a_reveal = act(f"Show Searchable PDF in {FILE_MANAGER}", self.reveal_current)
        self.a_ocr = act("Run OCR…", self.run_ocr, QKeySequence("Ctrl+R"))
        self.a_translate = act("Translate Whole Document", lambda: self.translate("document"), QKeySequence("Ctrl+T"))
        self.a_translate_page = act("Translate This Page", lambda: self.translate("page"), QKeySequence("Ctrl+Shift+T"))
        self.a_model = act("Translation Model…", self.choose_model)
        self.a_retranslate = act("Translate This Page Again", self.retranslate_page)
        self.a_clear_tr = act("Forget Translations of This Document", self.clear_translations)
        self.a_test_tr = act("Test Translation…", self.test_translation)
        self.a_log = act("Open Log Folder", lambda: open_folder(str(data_dir())))
        self.a_tessdata = act("Open OCR Language Folder", lambda: open_folder(str(user_tessdata_dir())))
        self.a_copy = act("Copy", self.view.copy_selection, QKeySequence.Copy)
        self.a_select_all = act("Select All on Page", self.view.select_all_on_page, QKeySequence.SelectAll)
        self.a_find = act("Find…", self._focus_search, QKeySequence.Find)
        self.a_find_next = act("Find Next", lambda: self._search(False), QKeySequence.FindNext)
        self.a_find_prev = act("Find Previous", lambda: self._search(True), QKeySequence.FindPrevious)
        self.a_zoom_in = act("Zoom In", self.view.zoom_in, QKeySequence.ZoomIn)
        self.a_zoom_out = act("Zoom Out", self.view.zoom_out, QKeySequence.ZoomOut)
        self.a_fit = act("Fit Width", lambda: self.view.set_zoom(1.0, fit=True), QKeySequence("Ctrl+0"))
        self.a_confine = act("Keep Selection Inside One Column", self._toggle_confine, None, True, self.view.confine)
        self.a_reflow = act("Join Lines When Copying", self._toggle_reflow, None, True, self.view.reflow)
        self.a_columns = act("Show Detected Columns", self._toggle_columns, QKeySequence("Ctrl+L"), True, self.view.show_columns)
        self.a_link = act("Link Translation to Original", self._toggle_link, None, True, self.link_on)
        self.exports = [
            act("Translated PDF, Layout Kept (.pdf)…", lambda: self.export("pdf_translated")),
            act("Original and Translated Pages Side by Side (.pdf)…", lambda: self.export("pdf_side")),
            act("English Translation as Word (.docx)…", lambda: self.export("en_docx")),
            act("English Translation as Text (.txt)…", lambda: self.export("en_txt")),
            act("Original and English Side by Side (.docx)…", lambda: self.export("en_bilingual")),
            act("Recognised Text as Word (.docx)…", lambda: self.export("text_docx")),
            act("Recognised Text as Text (.txt)…", lambda: self.export("text_txt")),
        ]

        mb = self.menuBar()
        m = mb.addMenu("File")
        m.addActions([self.a_add, self.a_add_folder])
        m.addSeparator()
        em = m.addMenu("Export")
        em.addActions(self.exports[:2])
        em.addSeparator()
        em.addActions(self.exports[2:5])
        em.addSeparator()
        em.addActions(self.exports[5:])
        m.addAction(self.a_reveal)
        m.addSeparator()
        m.addAction(self.a_remove)
        m = mb.addMenu("Edit")
        m.addActions([self.a_copy, self.a_select_all])
        m.addSeparator()
        m.addActions([self.a_find, self.a_find_next, self.a_find_prev])
        m = mb.addMenu("View")
        m.addActions([self.a_zoom_in, self.a_zoom_out, self.a_fit])
        m.addSeparator()
        m.addActions([self.a_columns, self.a_confine, self.a_reflow, self.a_link])
        m = mb.addMenu("Tools")
        m.addAction(self.a_ocr)
        m.addSeparator()
        m.addActions([self.a_translate, self.a_translate_page, self.a_retranslate, self.a_clear_tr])
        m.addSeparator()
        m.addAction(self.a_model)
        m.addSeparator()
        m.addAction(self.a_tessdata)
        m = mb.addMenu("Help")
        m.addActions([self.a_test_tr, self.a_log])

        tb = self.addToolBar("Main")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
        tb.setIconSize(QSize(16, 16))
        tb.addAction(self.a_add)
        tb.addAction(self.a_ocr)
        t_tr = QAction("Translate", self)
        t_tr.triggered.connect(lambda: self.translate("document"))
        self.t_translate = t_tr
        tb.addAction(t_tr)
        export_btn = QToolButton()
        export_btn.setText("Export")
        export_btn.setPopupMode(QToolButton.InstantPopup)
        export_menu = QMenu(export_btn)
        export_menu.addActions(self.exports[:2])
        export_menu.addSeparator()
        export_menu.addActions(self.exports[2:5])
        export_menu.addSeparator()
        export_menu.addActions(self.exports[5:])
        export_btn.setMenu(export_menu)
        self.export_btn = export_btn
        tb.addWidget(export_btn)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        tb.addWidget(QLabel("Page "))
        self.page_spin = QSpinBox()
        self.page_spin.setMinimum(1)
        self.page_spin.setKeyboardTracking(False)
        self.page_spin.valueChanged.connect(lambda v: self.view.goto_page(v - 1) if v - 1 != self.view.current_page() else None)
        tb.addWidget(self.page_spin)
        self.page_total = QLabel(" of 0  ")
        tb.addWidget(self.page_total)
        tb.addAction(self.a_zoom_out)
        self.a_zoom_out.setIconText("−")
        self.zoom_label = QLabel("100%")
        self.zoom_label.setMinimumWidth(48)
        self.zoom_label.setAlignment(Qt.AlignCenter)
        tb.addWidget(self.zoom_label)
        tb.addAction(self.a_zoom_in)
        self.a_zoom_in.setIconText("+")
        tb.addAction(self.a_fit)
        self.a_fit.setIconText("Fit")
        self.search = QLineEdit()
        self.search.setPlaceholderText("Find in document")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(220)
        self.search.returnPressed.connect(lambda: self._search(False))
        tb.addWidget(self.search)

    # ----------------------------------------------------------- documents
    def current_item(self) -> DocItem | None:
        row = self.list.currentRow()
        return self.items[row] if 0 <= row < len(self.items) else None

    def _item_by_uid(self, uid: int) -> DocItem | None:
        return next((it for it in self.items if it.uid == uid), None)

    def _refresh_row(self, item: DocItem) -> None:
        row = self.items.index(item)
        w = self.list.item(row)
        w.setText(f"{item.title}\n{item.status_text()}")
        w.setToolTip("\n".join(item.sources))

    def _save_documents(self) -> None:
        self.settings.set("documents", [{"sources": it.sources} for it in self.items])

    def _restore_documents(self) -> None:
        docs = self.settings.get("documents") or []
        groups = [d.get("sources", []) for d in docs if d.get("sources")]
        groups = [g for g in groups if all(os.path.exists(s) for s in g)]
        if groups:
            self._add_groups(groups)
        if not find_tesseract():
            self.statusBar().showMessage("Tesseract OCR is not installed - OCR is unavailable (Tools > Run OCR shows how to install it).")

    def add_files_dialog(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(SUPPORTED_EXT))
        files, _ = QFileDialog.getOpenFileNames(self, "Add PDFs or scanned images", "", f"PDFs and images ({exts});;All files (*)")
        if files:
            self.add_paths(files)

    def add_folder_dialog(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Add a folder of PDFs or scans")
        if d:
            self.add_paths([d])

    def add_paths(self, paths: list[str], combine_images: bool | None = None) -> None:
        files: list[str] = []
        for p in paths:
            if os.path.isdir(p):
                for name in sorted(os.listdir(p), key=natural_key):
                    full = os.path.join(p, name)
                    if os.path.isfile(full) and Path(name).suffix.lower() in SUPPORTED_EXT and not name.startswith("."):
                        files.append(full)
            elif Path(p).suffix.lower() in SUPPORTED_EXT:
                files.append(p)
        pdfs = [f for f in files if Path(f).suffix.lower() in PDF_EXT]
        images = sorted((f for f in files if Path(f).suffix.lower() in IMAGE_EXT), key=lambda f: natural_key(os.path.basename(f)))
        groups = [[f] for f in pdfs]
        if len(images) > 1:
            if combine_images is None:
                answer = QMessageBox.question(
                    self,
                    "Combine images?",
                    f"You added {len(images)} images. Treat them as the pages of one document?\n\n"
                    "Yes: one document with the images as pages, in file-name order.\nNo: every image becomes its own document.",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.Yes,
                )
                combine_images = answer == QMessageBox.Yes
            groups += [images] if combine_images else [[f] for f in images]
        else:
            groups += [[f] for f in images]
        if not groups:
            self.statusBar().showMessage("No PDFs or images found.", 5000)
            return
        self._add_groups(groups)

    def _add_groups(self, groups: list[list[str]]) -> None:
        known = {tuple(it.sources) for it in self.items}
        new_items = []
        for sources in groups:
            if tuple(sources) in known:
                continue
            first = Path(sources[0])
            title = first.name if len(sources) == 1 else f"{first.parent.name} ({len(sources)} images)"
            item = DocItem(sources=list(sources), title=title)
            self.items.append(item)
            w = QListWidgetItem(f"{item.title}\n{item.status_text()}")
            self.list.addItem(w)
            new_items.append(item)
        if not new_items:
            return
        self._save_documents()
        job = PrepareJob(new_items, self.settings.get("output_dir"))
        job.itemReady.connect(self._item_ready)
        self.enqueue(job)
        if self.list.currentRow() < 0:
            self.list.setCurrentRow(0)

    def _item_ready(self, uid: int, data: dict) -> None:
        item = self._item_by_uid(uid)
        if item is None:
            return
        for k, v in data.items():
            if hasattr(item, k):
                setattr(item, k, v)
        item.busy = ""
        self._refresh_row(item)
        if item is self.current_item():
            self._load_current(keep_position=True)
        self._update_actions()

    def _doc_selected(self, _row: int) -> None:
        self._load_current()
        self._update_actions()

    def _load_current(self, keep_position: bool = False) -> None:
        item = self.current_item()
        path = item.view_path if item and item.view_path and os.path.exists(item.view_path) else None
        self.view.set_document(path, keep_position)
        self.store = self._make_store(item) if path else None
        n = self.view.page_count()
        self.page_spin.blockSignals(True)
        self.page_spin.setMaximum(max(1, n))
        self.page_spin.setValue(self.view.current_page() + 1)
        self.page_spin.blockSignals(False)
        self.page_total.setText(f" of {n}  ")
        self.setWindowTitle(f"{item.title} — {APP_NAME}" if item else APP_NAME)
        self.refresh_panel()

    def _make_store(self, item: DocItem | None) -> TranslationStore | None:
        if item is None or not item.view_path:
            return None
        key, custom = self._model()
        ident = ("folder-" + Path(custom).name) if custom else key
        return TranslationStore(Path(item.view_path), ident, self.source_lang.currentData())

    def remove_current(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        if self.active is not None and self.busy_uid == self.items[row].uid:
            QMessageBox.information(self, APP_NAME, "This document is being processed. Cancel the job first.")
            return
        self.items.pop(row)
        self.list.takeItem(row)
        self._save_documents()
        if not self.items:
            self._load_current()
        self._update_actions()

    def reveal_current(self) -> None:
        item = self.current_item()
        if item and item.view_path:
            reveal_in_file_manager(item.view_path)

    def _list_menu(self, pos) -> None:
        if self.list.itemAt(pos) is None:
            return
        menu = QMenu(self)
        menu.addActions([self.a_ocr, self.a_translate, self.a_reveal])
        menu.addSeparator()
        menu.addAction(self.a_remove)
        menu.exec(self.list.mapToGlobal(pos))

    # ----------------------------------------------------------------- jobs
    def enqueue(self, job: Job) -> None:
        job.setParent(self)
        job.progress.connect(self._job_progress)
        job.failed.connect(self._job_failed)
        job.finished.connect(self._job_finished)
        self.queue.append(job)
        self._pump()

    def _pump(self) -> None:
        if self.active is None and self.queue:
            self.active = self.queue.pop(0)
            self.job_label.setText(self.active.label + "…")
            self.job_bar.setRange(0, 0)
            self.job_box.show()
            self.active.start()
        self._update_actions()

    def _job_progress(self, stage: str, done: float, total: float) -> None:
        if self.sender() is not self.active:
            return
        if total <= 0:
            self.job_bar.setRange(0, 0)
            text = stage + "…"
        else:
            self.job_bar.setRange(0, 1000)
            self.job_bar.setValue(int(1000 * done / total))
            if total > 100_000:  # bytes
                text = f"{stage}: {done / 1e6:.0f} of {total / 1e6:.0f} MB"
            else:
                text = f"{stage}: {int(done)} of {int(total)}"
        self.job_label.setText(text)
        item = self._item_by_uid(self.busy_uid) if self.busy_uid is not None else None
        if item is not None:
            item.busy = text
            self._refresh_row(item)

    def _job_failed(self, message: str) -> None:
        # Drop follow-up jobs that depended on the failed one, keep document checks.
        self.queue = [j for j in self.queue if isinstance(j, PrepareJob)]
        QMessageBox.warning(self, APP_NAME, message)

    def _job_finished(self) -> None:
        job = self.sender()
        if job is not self.active:
            return
        if job.cancel.is_set():
            self.queue = [j for j in self.queue if isinstance(j, PrepareJob)]
        self.active = None
        item = self._item_by_uid(self.busy_uid) if self.busy_uid is not None else None
        self.busy_uid = None
        if item is not None:
            item.busy = ""
            self._refresh_row(item)
        job.deleteLater()
        if not self.queue:
            self.job_box.hide()
        self._pump()
        self.refresh_panel()

    def cancel_job(self) -> None:
        if self.active is not None:
            self.active.cancel.set()
            self.job_label.setText("Stopping…")

    def _update_actions(self) -> None:
        item = self.current_item()
        has_doc = item is not None and bool(item.view_path)
        busy = self.active is not None and not isinstance(self.active, PrepareJob)
        self.a_ocr.setEnabled(bool(self.items) and not busy)
        for a in (self.a_translate, self.a_translate_page, self.t_translate):
            a.setEnabled(has_doc and not busy)
        self.btn_doc.setEnabled(has_doc and not busy)
        self.btn_page.setEnabled(has_doc and not busy)
        for a in self.exports:
            a.setEnabled(has_doc and not busy)
        self.export_btn.setEnabled(has_doc and not busy)
        self.a_remove.setEnabled(item is not None)
        self.a_reveal.setEnabled(has_doc)
        self.a_retranslate.setEnabled(has_doc and not busy)
        self.a_clear_tr.setEnabled(has_doc and not busy)
        self.a_test_tr.setEnabled(not busy)

    # ------------------------------------------------------------------ OCR
    def run_ocr(self) -> None:
        if not find_tesseract():
            QMessageBox.warning(self, APP_NAME, INSTALL_HINT)
            return
        item = self.current_item()
        pending = [it for it in self.items if it.state == "needs_ocr"]
        dlg = OcrDialog(available_languages(refresh=True), self.settings, item.title if item else None, len(pending), self)
        if dlg.exec() != OcrDialog.Accepted:
            return
        langs = dlg.languages()
        if not langs:
            QMessageBox.information(self, APP_NAME, "Choose at least one language.")
            return
        self.settings.set("ocr_langs", langs)
        self.settings.set("ocr_mode", dlg.mode())
        targets = pending if dlg.all_documents() else ([item] if item else [])
        targets = [t for t in targets if t.state != "error"]
        if not targets:
            return
        # Windows cannot replace a file that is open: let go of an earlier OCR result on screen.
        outputs = {str(output_path_for([Path(s) for s in t.sources], self.settings.get("output_dir"))) for t in targets}
        if self.view.path and self.view.path in outputs:
            self.view.set_document(None)
        self.settings.set("ocr_fix_russian", dlg.fix_russian.isChecked())
        self.settings.set("ocr_second_look", dlg.second_look.isChecked())
        job = OcrJob(
            targets, langs, dlg.mode(), int(self.settings.get("ocr_dpi")), int(self.settings.get("ocr_workers")),
            self.settings.get("output_dir"), fix_russian=dlg.fix_russian.isChecked(), second_look=dlg.second_look.isChecked(),
        )
        job.itemStarted.connect(self._ocr_started)
        job.itemDone.connect(self._ocr_done)
        self.enqueue(job)

    def _ocr_started(self, uid: int) -> None:
        self.busy_uid = uid

    def _ocr_done(self, uid: int, data: dict) -> None:
        item = self._item_by_uid(uid)
        if item is None:
            return
        ocred, words = data.pop("ocred", 0), data.pop("words", 0)
        for k, v in data.items():
            setattr(item, k, v)
        item.busy = ""
        self.busy_uid = None
        self._refresh_row(item)
        if ocred:
            self.statusBar().showMessage(
                f"{item.title}: {ocred} page{'s' if ocred != 1 else ''} recognised, {words} words. Saved as {Path(item.view_path).name}", 15000
            )
        else:
            self.statusBar().showMessage(f"{item.title}: every page already has a usable text layer.", 10000)
        if item is self.current_item():
            self._load_current(keep_position=True)

    # ---------------------------------------------------------- translation
    def _model(self) -> tuple[str, str]:
        """(model key, custom NLLB folder or "")."""
        key = self.settings.get("translation_model")
        if key not in MODELS:
            key = "nllb-1.3B"
        return key, self.settings.get("translation_model_dir") or ""

    def choose_model(self) -> bool:
        dlg = ModelDialog(self.settings, self)
        if dlg.exec() != ModelDialog.Accepted:
            return False
        if dlg.custom_dir:
            if not model_ready(Path(dlg.custom_dir)):
                QMessageBox.warning(self, APP_NAME, "That folder does not contain model.bin and sentencepiece.bpe.model.")
                return False
            self.settings.set("translation_model_dir", dlg.custom_dir)
        else:
            key = dlg.model_key()
            if engine_of(key) == "ollama" and not (ollama_running() or find_ollama()):
                QMessageBox.information(self, APP_NAME, OLLAMA_HINT)
                return False
            self.settings.set("translation_model_dir", "")
            self.settings.set("translation_model", key)
        self.settings.set("translation_model_chosen", True)
        self.store = self._make_store(self.current_item())
        self.refresh_panel()
        return True

    def _ensure_model(self) -> bool:
        """Make sure the chosen model is usable, queueing a download (or Ollama start) when needed."""
        key, custom = self._model()
        if model_installed(key, custom):
            return True
        if not self.settings.get("translation_model_chosen") or custom:
            if not self.choose_model():
                return False
            key, custom = self._model()
            if model_installed(key, custom):
                return True
        if engine_of(key) == "ollama" and not (ollama_running() or find_ollama()):
            QMessageBox.information(self, APP_NAME, OLLAMA_HINT)
            return False
        size = MODELS[key]["bytes"] / 1e9
        self.statusBar().showMessage(
            f"Preparing {MODELS[key]['label'].split(' - ')[0]} (a {size:.1f} GB download the first time).", 15000
        )
        self.enqueue(DownloadJob(key))
        return True

    def translate(self, scope: str) -> None:
        item = self.current_item()
        if item is None or not item.view_path or self.store is None:
            return
        if item.state == "needs_ocr":
            answer = QMessageBox.question(
                self, APP_NAME,
                "Some pages of this document have no usable text yet, so they cannot be translated.\n\nTranslate the pages that do have text?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        if not self._ensure_model():
            return
        current = self.view.current_page()
        n = self.view.page_count()
        pages = [current] if scope == "page" else [current] + [p for p in range(n) if p != current]
        job = TranslateJob(item.view_path, pages, self._model(), self.source_lang.currentData(), self.store)
        job.pageDone.connect(self._page_translated)
        self.panel_mode.setCurrentIndex(0)
        self.enqueue(job)

    def retranslate_page(self) -> None:
        if self.store is None:
            return
        pno = self.view.current_page()
        self.store.pages.pop(str(pno), None)
        self.store.save()
        self.translate("page")

    def clear_translations(self) -> None:
        if self.store is None or not self.store.count():
            self.statusBar().showMessage("This document has no saved translations.", 5000)
            return
        answer = QMessageBox.question(
            self, APP_NAME, f"Forget the saved translation of {self.store.count()} page(s) of this document?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self.store.pages.clear()
            self.store.save()
            self.refresh_panel()

    def test_translation(self) -> None:
        if not self._ensure_model():
            return
        job = DiagnoseJob(self._model())
        job.result.connect(self._show_diagnosis)
        self.enqueue(job)

    def _show_diagnosis(self, report: str) -> None:
        dlg = SnippetDialog(report, "", self)
        dlg.setWindowTitle("Translation test")
        dlg.show_report()
        dlg.show()

    def _page_translated(self, pno: int) -> None:
        if pno == self.view.current_page():
            self.refresh_panel()

    def translate_selection(self, text: str) -> None:
        if not text.strip() or not self._ensure_model():
            return
        job = SnippetJob(text, self._model(), self.source_lang.currentData())
        job.result.connect(lambda src, en: SnippetDialog(src, en, self).show())
        self.enqueue(job)

    def _source_changed(self) -> None:
        self.settings.set("translation_source", self.source_lang.currentData())
        self.store = self._make_store(self.current_item())
        self.refresh_panel()

    # ---------------------------------------------------------------- panel
    def _set_panel(self, html_text: str) -> None:
        self._links, self._link_words, self._anchor_ranges = {}, {}, {}
        self._links_page = -1
        self._active_keys = self._sel_keys = ()
        self.view.set_linked(None, [])
        self.panel.setExtraSelections([])
        self.panel.setHtml(html_text)

    def refresh_panel(self) -> None:
        item = self.current_item()
        if item is None or self.view.page_count() == 0:
            self._set_panel("<p style='color:gray'>No document open.</p>")
            return
        pno = self.view.current_page()
        head = f"<p style='color:gray; font-size:11px; margin:0 0 8px 0'>PAGE {pno + 1}</p>"
        para = "<p style='margin:0 0 11px 0; line-height:135%'>{}</p>"
        if self.panel_mode.currentData() == "text":
            paras = [p.text for p in self.view.layout(pno).paragraphs()]
            if paras:
                body = "".join(para.format(html.escape(t)) for t in paras)
            else:
                body = "<p style='color:gray'>No text on this page yet. Use <b>Run OCR</b> to recognise it.</p>"
            self._set_panel(head + body)
            return
        entries = self.store.get(pno) if self.store else None
        if entries is None:
            if self.view.layout(pno).is_empty():
                msg = "This page has no text yet. Run OCR first, then translate."
            elif isinstance(self.active, TranslateJob):
                msg = "Translation in progress… this page will appear here when it is ready."
            else:
                msg = "This page is not translated yet. Use the buttons below."
            self._set_panel(head + f"<p style='color:gray'>{msg}</p>")
            return
        if not entries:
            self._set_panel(head + "<p style='color:gray'>No text on this page.</p>")
            return
        langs = sorted({LANG_NAMES.get(e.get("lang"), "") for e in entries if e.get("lang")})
        if langs:
            head = head.replace("</p>", f" · translated from {', '.join(l for l in langs if l)}</p>")
        body = []
        old = False
        for i, e in enumerate(entries):
            sents = e.get("sents")
            if not sents:
                old = True
                body.append(para.format(html.escape(e["en"])))
                continue
            parts = [f"<a href='s:{i}.{j}'>{html.escape(t)}</a>" for j, (_a, _b, t) in enumerate(sents) if t]
            body.append(para.format(" ".join(parts)))
        if old:
            body.append(
                "<p style='color:gray; font-size:11px'>Part of this page was translated by an earlier version, "
                "so its sentences are not linked. Tools > Translate This Page Again links them.</p>"
            )
        self._set_panel(head + "".join(body))
        self._index_panel()
        self._build_links(pno, entries)

    # ------------------------------------------------------------- linking
    def _index_panel(self) -> None:
        """Character range of every English sentence in the panel."""
        doc = self.panel.document()
        ranges: dict[str, tuple[int, int]] = {}
        block = doc.begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                if frag.isValid() and frag.charFormat().isAnchor():
                    href = frag.charFormat().anchorHref()
                    if href.startswith("s:"):
                        a, b = frag.position(), frag.position() + frag.length()
                        old = ranges.get(href[2:])
                        ranges[href[2:]] = (min(a, old[0]), max(b, old[1])) if old else (a, b)
                it += 1
            block = block.next()
        self._anchor_ranges = ranges

    def _build_links(self, pno: int, entries: list[dict]) -> None:
        """Where on the page each translated sentence came from."""
        lay = self.view.layout(pno)
        paras = [p for p in lay.paragraphs() if p.text.strip()]
        by_text = {p.text: p for p in paras}
        for i, e in enumerate(entries):
            p = paras[i] if i < len(paras) and paras[i].text == e.get("src") else by_text.get(e.get("src"))
            if p is None:
                continue
            for j, (a, b, _t) in enumerate(e.get("sents") or []):
                words = p.words_in(a, b)
                if words:
                    key = f"{i}.{j}"
                    self._links[key] = word_rects(words, lay.skew)
                    self._link_words[key] = {id(w) for w in words}
        self._links_page = pno

    def _apply_link(self, keys: tuple[str, ...], scroll_panel: bool = False, on_pdf: bool = True) -> None:
        if keys == self._active_keys:
            return
        self._active_keys = keys
        rects = [r for k in keys for r in self._links.get(k, [])] if on_pdf else []
        self.view.set_linked(self._links_page if rects else None, rects)
        fmt = QTextCharFormat()
        fmt.setBackground(QColor(255, 196, 0, 110))
        selections = []
        for k in keys:
            rng = self._anchor_ranges.get(k)
            if not rng:
                continue
            sel = QTextEdit.ExtraSelection()
            cur = QTextCursor(self.panel.document())
            cur.setPosition(rng[0])
            cur.setPosition(rng[1], QTextCursor.KeepAnchor)
            sel.cursor, sel.format = cur, fmt
            selections.append(sel)
        self.panel.setExtraSelections(selections)
        if scroll_panel and keys and keys[0] in self._anchor_ranges:
            cur = QTextCursor(self.panel.document())
            cur.setPosition(self._anchor_ranges[keys[0]][0])
            r = self.panel.cursorRect(cur)
            vb = self.panel.verticalScrollBar()
            if r.top() < 0 or r.bottom() > self.panel.viewport().height():
                vb.setValue(vb.value() + r.top() - self.panel.viewport().height() // 3)

    def _key_at(self, x: float, y: float) -> str | None:
        pad = 1.5
        for key, rects in self._links.items():
            for x0, y0, x1, y1 in rects:
                if x0 - pad <= x <= x1 + pad and y0 - pad <= y <= y1 + pad:
                    return key
        return None

    def _pdf_hovered(self, pno: int, x: float, y: float) -> None:
        if not self.link_on or not self._links:
            return
        key = self._key_at(x, y) if pno == self._links_page else None
        if key:
            self._apply_link((key,), scroll_panel=True)
        else:
            self._apply_link(self._sel_keys, on_pdf=False)

    def _panel_hovered(self, href: str) -> None:
        if not self.link_on:
            return
        if href.startswith("s:") and href[2:] in self._links:
            self._apply_link((href[2:],))
        else:
            self._apply_link(self._sel_keys, on_pdf=False)

    def _panel_clicked(self, url) -> None:
        href = url.toString()
        key = href[2:] if href.startswith("s:") else ""
        rects = self._links.get(key)
        if rects and self.link_on:
            self.view.scroll_to_rect(self._links_page, rects[0])
            self._active_keys = ()
            self._apply_link((key,))

    def eventFilter(self, obj, event) -> bool:
        if obj is self.panel.viewport():
            if event.type() == QEvent.MouseMove:
                self._panel_hovered(self.panel.anchorAt(event.position().toPoint()))
            elif event.type() == QEvent.Leave:
                self._panel_hovered("")
        return super().eventFilter(obj, event)

    def _toggle_link(self, on: bool) -> None:
        self.link_on = on
        self.settings.set("link_translation", on)
        if not on:
            self._sel_keys = ()
            self._apply_link(())

    # ------------------------------------------------------------- exports
    def export(self, kind: str) -> None:
        item = self.current_item()
        if item is None or not item.view_path:
            return
        ext = ".txt" if kind.endswith("txt") else (".pdf" if kind.startswith("pdf") else ".docx")
        suffix = {
            "en_docx": "_en", "en_txt": "_en", "en_bilingual": "_bilingual", "text_docx": "_text", "text_txt": "_text",
            "pdf_translated": "_en", "pdf_side": "_side_by_side",
        }[kind]
        translate_first = False
        if kind.startswith("pdf"):
            n = self.view.page_count()
            done = self.store.count() if self.store else 0
            if done < n:
                box = QMessageBox(self)
                box.setWindowTitle(APP_NAME)
                box.setText(
                    f"{done} of {n} pages are translated."
                    + ("" if done else " Nothing would change yet.")
                    + "\n\nPages that are not translated are copied unchanged."
                )
                first = box.addButton("Translate the Rest First", QMessageBox.AcceptRole)
                now = box.addButton("Export Now", QMessageBox.DestructiveRole) if done else None
                box.addButton(QMessageBox.Cancel)
                box.exec()
                clicked = box.clickedButton()
                if clicked is first:
                    translate_first = True
                elif now is None or clicked is not now:
                    return
        stem = Path(item.view_path).stem
        if stem.endswith("_ocr"):
            stem = stem[:-4]
        start = str(Path(item.sources[0]).parent / f"{stem}{suffix}{ext}")
        out, _ = QFileDialog.getSaveFileName(self, "Export", start, f"*{ext}")
        if not out:
            return
        if not out.lower().endswith(ext):
            out += ext
        if translate_first:
            self.translate("document")
            if not any(isinstance(j, TranslateJob) for j in self.queue + [self.active]):
                return  # translation was not started (e.g. no model chosen)
        job = ExportJob(
            kind, item.view_path, out, Path(item.title).stem, self.store,
            model=self._model() if kind.startswith("pdf") else None, source=self.source_lang.currentData(),
        )
        job.done.connect(lambda p: self.statusBar().showMessage(f"Exported {Path(p).name}", 10000))
        job.done.connect(reveal_in_file_manager)
        self.enqueue(job)

    # ----------------------------------------------------------- view glue
    def _page_changed(self, pno: int) -> None:
        if isinstance(self.active, TranslateJob):
            self.active.wanted = pno  # translate the page being read next
        self.page_spin.blockSignals(True)
        self.page_spin.setValue(pno + 1)
        self.page_spin.blockSignals(False)
        self.refresh_panel()

    def _selection_changed(self, text: str) -> None:
        if self.link_on and self._links and self.view.selection_page() == self._links_page:
            ids = self.view.selected_words()
            self._sel_keys = tuple(k for k, w in self._link_words.items() if w & ids)
            self._apply_link(self._sel_keys, scroll_panel=True, on_pdf=False)
        elif self._sel_keys:
            self._sel_keys = ()
            self._apply_link(())
        if text:
            self.statusBar().showMessage(f"{len(text)} characters selected — {COPY_KEY} to copy")
        else:
            self.statusBar().clearMessage()

    def _zoom_changed(self, zoom: float) -> None:
        self.zoom_label.setText(f"{round(zoom * 100)}%")

    def _toggle_confine(self, on: bool) -> None:
        self.view.confine = on
        self.settings.set("confine_selection", on)

    def _toggle_reflow(self, on: bool) -> None:
        self.view.reflow = on
        self.settings.set("reflow_copy", on)

    def _toggle_columns(self, on: bool) -> None:
        self.view.show_columns = on
        self.settings.set("show_columns", on)
        self.view.viewport().update()

    def _focus_search(self) -> None:
        self.search.setFocus()
        self.search.selectAll()

    def _search(self, backwards: bool) -> None:
        text = self.search.text()
        if not text.strip():
            self.view.find("")
            return
        if not self.view.find(text, backwards):
            self.statusBar().showMessage(f"“{text}” was not found.", 5000)

    # ------------------------------------------------------------ dropping
    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.add_paths(paths)

    def closeEvent(self, event) -> None:
        self.queue.clear()
        if self.active is not None:
            self.active.cancel.set()
            self.active.wait(8000)
        super().closeEvent(event)
