"""Small dialogs: OCR options, translation model, translated selection."""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from ..config import CORE_LANGS
from ..translate import MODELS


class OcrDialog(QDialog):
    def __init__(self, available: dict, settings, current_title: str | None, n_pending: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Run OCR")
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)

        box = QGroupBox("Languages in the document")
        grid = QGridLayout(box)
        chosen = settings.get("ocr_langs") or list(CORE_LANGS)
        self.checks: dict[str, QCheckBox] = {}
        for i, (code, name) in enumerate(CORE_LANGS.items()):
            cb = QCheckBox(name)
            if code in available:
                cb.setChecked(code in chosen)
            else:
                cb.setEnabled(False)
                cb.setText(f"{name} (language data not installed)")
            self.checks[code] = cb
            grid.addWidget(cb, i // 2, i % 2)
        others = sorted(c for c in available if c not in CORE_LANGS)
        self.extra = QComboBox()
        self.extra.addItem("No additional language", "")
        for code in others:
            self.extra.addItem(code, code)
        extra_chosen = [c for c in chosen if c not in CORE_LANGS and c in available]
        if extra_chosen:
            self.extra.setCurrentIndex(self.extra.findData(extra_chosen[0]))
        form = QFormLayout()
        self.primary = QComboBox()
        for code, name in CORE_LANGS.items():
            if code in available:
                self.primary.addItem(name, code)
        if chosen and self.primary.findData(chosen[0]) >= 0:
            self.primary.setCurrentIndex(self.primary.findData(chosen[0]))
        form.addRow("Main language:", self.primary)
        if others:
            form.addRow("Also use:", self.extra)
        grid.addLayout(form, 2, 0, 1, 2)
        hint = QLabel("Tick only the languages that really occur: fewer languages means faster and more accurate OCR.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")
        grid.addWidget(hint, 3, 0, 1, 2)
        lay.addWidget(box)

        box2 = QGroupBox("Pages")
        v2 = QVBoxLayout(box2)
        self.auto = QRadioButton("Only pages with no text layer or a damaged one (recommended)")
        self.force = QRadioButton("Re-OCR every page, replacing any existing text layer")
        (self.force if settings.get("ocr_mode") == "force" else self.auto).setChecked(True)
        v2.addWidget(self.auto)
        v2.addWidget(self.force)
        lay.addWidget(box2)

        box4 = QGroupBox("Accuracy")
        v4 = QVBoxLayout(box4)
        self.second_look = QCheckBox("Look again for text OCR skipped (headings in the margin, notes, captions)")
        self.second_look.setChecked(bool(settings.get("ocr_second_look")))
        self.fix_russian = QCheckBox("Correct typical OCR letter mix-ups in Russian text (и/н, л/д…) with a dictionary")
        self.fix_russian.setChecked(bool(settings.get("ocr_fix_russian")))
        from .. import ocrfix

        if not ocrfix.available():
            self.fix_russian.setChecked(False)
            self.fix_russian.setEnabled(False)
            self.fix_russian.setText(self.fix_russian.text() + " (dictionary not installed)")
        v4.addWidget(self.second_look)
        v4.addWidget(self.fix_russian)
        lay.addWidget(box4)

        box3 = QGroupBox("Documents")
        v3 = QVBoxLayout(box3)
        self.this_doc = QRadioButton(f"This document: {current_title}" if current_title else "This document")
        self.all_docs = QRadioButton(f"All documents that need OCR ({n_pending})")
        self.this_doc.setEnabled(current_title is not None)
        self.all_docs.setEnabled(n_pending > 0)
        group = QButtonGroup(self)
        group.addButton(self.this_doc)
        group.addButton(self.all_docs)
        (self.this_doc if current_title is not None else self.all_docs).setChecked(True)
        v3.addWidget(self.this_doc)
        v3.addWidget(self.all_docs)
        lay.addWidget(box3)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        start = buttons.addButton("Start OCR", QDialogButtonBox.AcceptRole)
        start.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def languages(self) -> list[str]:
        langs = [c for c, cb in self.checks.items() if cb.isChecked()]
        main = self.primary.currentData()
        if main in langs:
            langs.remove(main)
            langs.insert(0, main)
        extra = self.extra.currentData()
        if extra:
            langs.append(extra)
        return langs

    def mode(self) -> str:
        return "force" if self.force.isChecked() else "auto"

    def all_documents(self) -> bool:
        return self.all_docs.isChecked()


class ModelDialog(QDialog):
    """Choose (and then download) the offline translation model."""

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        from ..translate import engine_of, find_ollama, model_dir, model_ready, ollama_has, ollama_running, recommended_model

        self.setWindowTitle("Translation model")
        self.setMinimumWidth(560)
        self.custom_dir = ""
        lay = QVBoxLayout(self)
        text = QLabel(
            "Translation runs entirely on this computer. The model is downloaded once; "
            "after that no internet connection is needed and nothing leaves the computer."
        )
        text.setWordWrap(True)
        lay.addWidget(text)

        running = ollama_running()
        installed_ollama = running or bool(find_ollama())
        recommended = recommended_model()
        current = settings.get("translation_model")
        self.radios: dict[str, QRadioButton] = {}
        groups = {
            "nllb": QGroupBox("Fast: NLLB-200 (translates sentence by sentence)"),
            "ollama": QGroupBox("Smarter: TranslateGemma (reads whole paragraphs; runs in the free Ollama app)"),
        }
        for g in groups.values():
            g.setLayout(QVBoxLayout())
            lay.addWidget(g)
        for key, spec in MODELS.items():
            engine = engine_of(key)
            label = spec["label"]
            if engine == "nllb" and model_ready(model_dir(key)):
                label += "  — installed"
            if engine == "ollama" and running and ollama_has(spec["tag"]):
                label += "  — installed"
            if key == recommended:
                label += "  — recommended here"
            rb = QRadioButton(label)
            rb.setChecked(key == current)
            self.radios[key] = rb
            groups[engine].layout().addWidget(rb)
        if running:
            status = "Ollama is running."
        elif installed_ollama:
            status = "Ollama is installed; Paraq starts it when needed."
        else:
            status = (
                "Ollama is not installed. Get it free from <a href='https://ollama.com/download'>ollama.com/download</a>"
                + (" or run the Paraq installer with -Ai gemma-4b." if os.name == "nt" else ".")
            )
        ol = QLabel(status)
        ol.setOpenExternalLinks(True)
        ol.setWordWrap(True)
        groups["ollama"].layout().addWidget(ol)
        speed = QLabel(
            "TranslateGemma takes roughly a minute per page on an M-series Mac (12B) or a laptop "
            "NVIDIA GPU (4B), and longer on a processor alone. NLLB takes a few seconds per page."
        )
        speed.setWordWrap(True)
        speed.setStyleSheet("color: gray;")
        groups["ollama"].layout().addWidget(speed)
        note = QLabel(
            "Licences: NLLB-200 by Meta, CC BY-NC 4.0 (non-commercial). TranslateGemma by Google, Gemma terms of use."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        lay.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.ok = buttons.addButton("Use This Model", QDialogButtonBox.AcceptRole)
        folder = buttons.addButton("Use an NLLB Folder…", QDialogButtonBox.ActionRole)
        folder.clicked.connect(self._pick_folder)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _pick_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Folder with model.bin and sentencepiece.bpe.model")
        if d:
            self.custom_dir = d
            self.accept()

    def model_key(self) -> str:
        for key, rb in self.radios.items():
            if rb.isChecked():
                return key
        return "nllb-1.3B"


class SnippetDialog(QDialog):
    def __init__(self, source: str, english: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Translation of selection")
        self.resize(640, 420)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Original"))
        src = QPlainTextEdit(source)
        src.setReadOnly(True)
        lay.addWidget(src)
        lay.addWidget(QLabel("English"))
        self.en = QPlainTextEdit(english)
        self.en.setReadOnly(True)
        lay.addWidget(self.en)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        copy = QPushButton("Copy English")
        buttons.addButton(copy, QDialogButtonBox.ActionRole)
        copy.clicked.connect(self._copy)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.setAttribute(Qt.WA_DeleteOnClose)

    def show_report(self) -> None:
        """Reuse the dialog for a plain text report (translation test)."""
        layout = self.layout()
        for i in range(layout.count()):
            w = layout.itemAt(i).widget()
            if isinstance(w, QLabel) or (isinstance(w, QPlainTextEdit) and w is self.en):
                w.hide()
        self.resize(760, 560)

    def _copy(self) -> None:
        from PySide6.QtGui import QGuiApplication

        QGuiApplication.clipboard().setText(self.en.toPlainText())
