"""Offscreen GUI smoke test: add documents, run OCR, select text in a column, take screenshots."""
import os, sys, time, shutil, tempfile
from pathlib import Path
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
samples, shots = Path(sys.argv[1]), Path(sys.argv[2])
shots.mkdir(parents=True, exist_ok=True)
work = Path(tempfile.mkdtemp(prefix="paraq_gui_"))
os.environ["XDG_DATA_HOME"] = str(work / "data")
for name in ("scan_2col_mixed.pdf", "damaged_layer.pdf", "digital_2col.pdf", "scan_2col_skewed.pdf"):
    shutil.copy(samples / name, work / name)

from PySide6.QtCore import QPointF, Qt, QEvent
from PySide6.QtGui import QMouseEvent, QGuiApplication
from PySide6.QtWidgets import QApplication
from paraq.ui.mainwindow import MainWindow
import pymupdf

app = QApplication([])
win = MainWindow()
win.resize(1500, 950)
win.show()

def pump(cond, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    raise SystemExit("timeout waiting for " + str(cond))

win.add_paths([str(work / n) for n in ("scan_2col_mixed.pdf", "damaged_layer.pdf", "digital_2col.pdf")])
pump(lambda: win.active is None and all(it.state != "checking" for it in win.items))
print([ (it.title, it.state, it.status_text()) for it in win.items])
assert [it.state for it in win.items] == ["needs_ocr", "needs_ocr", "ready"], [it.state for it in win.items]
win.grab().save(str(shots / "1_added.png"))

# run OCR on everything pending (bypassing the dialog)
from paraq.ui.workers import OcrJob
job = OcrJob([it for it in win.items if it.state == "needs_ocr"], ["rus", "kaz", "eng", "tur"], "auto", 300, 0, "")
job.itemStarted.connect(win._ocr_started); job.itemDone.connect(win._ocr_done)
win.enqueue(job)
pump(lambda: win.active is None and not win.queue)
print([ (it.title, it.state, Path(it.view_path).name) for it in win.items])
assert all(it.state == "ready" for it in win.items)

# drag-select in the left column of page 1, far past the gutter into the right column
view = win.view
win.list.setCurrentRow(0)
app.processEvents()
win.a_columns.setChecked(True)
r = view._page_rect(0)
lay = view.layout(0)
left, right = lay.blocks[1], lay.blocks[2]
def vp(x, y):
    return QPointF(r.x() + x * view.zoom, r.y() + y * view.zoom)
def mouse(kind, pos, buttons=Qt.LeftButton):
    ev = QMouseEvent(kind, pos, view.viewport().mapToGlobal(pos.toPoint()), Qt.LeftButton, buttons, Qt.NoModifier)
    QApplication.sendEvent(view.viewport(), ev)
start = vp(left.x0 + 30, left.lines[2].y0 + 3)
end = vp(right.x1 - 20, right.lines[8].y1 - 2)   # ends deep inside the right column
mouse(QEvent.MouseButtonPress, start)
mouse(QEvent.MouseMove, end)
mouse(QEvent.MouseButtonRelease, end, Qt.NoButton)
app.processEvents()
sel = view.selected_text()
right_text = " ".join(l.text for l in right.lines)
print("confined selection:", len(sel), "chars:", sel[:90], "...", sel[-60:])
first_right_words = right.lines[0].text.split()[:3]
assert sel and " ".join(first_right_words) not in sel, "selection leaked into the right column"
assert view._focus[0] == view._anchor[0] == 1
view.copy_selection()
assert QGuiApplication.clipboard().text() == sel
win.grab().save(str(shots / "2_selection_confined.png"))

# same drag with confinement off -> flows on into the next column in reading order
win.a_confine.setChecked(False)
mouse(QEvent.MouseButtonPress, start); mouse(QEvent.MouseMove, end); mouse(QEvent.MouseButtonRelease, end, Qt.NoButton)
sel2 = view.selected_text()
assert " ".join(first_right_words) in sel2 and len(sel2) > len(sel)
win.grab().save(str(shots / "3_selection_flowing.png"))
win.a_confine.setChecked(True)
view.clear_selection()

# recognised-text panel + search
win.panel_mode.setCurrentIndex(1)
win.search.setText("ярмарк")
win._search(False)
app.processEvents()
assert view._hit is not None
win.grab().save(str(shots / "4_text_panel_search.png"))

# page 2 (Kazakh, with a column rule)
view.goto_page(1); app.processEvents()
win.grab().save(str(shots / "5_kazakh_page.png"))

# crooked scan: add, OCR, select inside the left column
win.search.setText(""); win._search(False)
win.add_paths([str(work / "scan_2col_skewed.pdf")])
pump(lambda: win.active is None and all(it.state != "checking" for it in win.items))
job = OcrJob([win.items[3]], ["rus", "kaz"], "auto", 300, 0, "")
job.itemStarted.connect(win._ocr_started); job.itemDone.connect(win._ocr_done)
win.enqueue(job)
pump(lambda: win.active is None and not win.queue)
win.list.setCurrentRow(3); app.processEvents()
r = view._page_rect(0); lay = view.layout(0)
left, right = lay.blocks[1], lay.blocks[2]
start = vp(left.x0 + 40, left.lines[3].y0 + 4)
end = vp(right.x1 - 30, right.lines[14].y1)
mouse(QEvent.MouseButtonPress, start); mouse(QEvent.MouseMove, end); mouse(QEvent.MouseButtonRelease, end, Qt.NoButton)
app.processEvents()
sel = view.selected_text()
assert len(sel) > 200 and view._focus[0] == 1 and sel in lay.selection_text(lay.start_of(1), lay.end_of(1))
win.grab().save(str(shots / "6_skewed_selection.png"))
view.clear_selection()

# offline translation with the tiny stand-in model (random output - checks the plumbing only)
import test_translate_mechanics as ttm
tiny = work / "tinymodel"; tiny.mkdir()
ttm.build_tiny_model(tiny)
win.settings.set("translation_model_dir", str(tiny))
win.list.setCurrentRow(0); app.processEvents()
win.a_columns.setChecked(False)
win.translate("document")
pump(lambda: win.active is None and not win.queue)
assert win.store.count() == 3, win.store.count()
entries = win.store.get(2)
assert [e["lang"] for e in entries if e["lang"]].count("tur_Latn") >= 3 and any(e["lang"] is None and e["en"].startswith("The archive") for e in entries)
view.goto_page(2); app.processEvents()
assert "The archive preserves" in win.panel.toPlainText()
win.grab().save(str(shots / "7_translation_panel.png"))

# exports
from paraq.ui.workers import ExportJob
import docx
for kind, name in (("en_docx", "en.docx"), ("en_bilingual", "bi.docx"), ("text_docx", "text.docx"), ("en_txt", "en.txt"), ("text_txt", "text.txt")):
    job = ExportJob(kind, win.items[0].view_path, str(work / name), "scan_2col_mixed", win.store)
    win.enqueue(job)
    pump(lambda: win.active is None and not win.queue)
    assert (work / name).stat().st_size > 500, name
d = docx.Document(str(work / "bi.docx"))
assert len(d.tables) == 3 and "Торговые пути" in d.tables[0].rows[0].cells[0].text
assert "Қазақ даласында" in (work / "text.txt").read_text("utf-8")
# new commands: translation test, retranslate page, forget translations, reading-order priority
from PySide6.QtWidgets import QMessageBox
from paraq.ui.workers import TranslateJob
reports = []
from paraq.ui.workers import DiagnoseJob
job = DiagnoseJob(("", str(tiny))); job.result.connect(reports.append); win.enqueue(job)
pump(lambda: win.active is None and not win.queue)
assert reports and "rus_Cyrl" in reports[0] and "kaz_Cyrl" in reports[0] and "Model loaded" in reports[0], reports
win._show_diagnosis(reports[0]); app.processEvents()
win.grab().save(str(shots / "8_translation_test.png"))
view.goto_page(1); app.processEvents()
before = win.store.count()
win.retranslate_page()
pump(lambda: win.active is None and not win.queue)
assert win.store.count() == before and win.store.get(1)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
win.clear_translations()
assert win.store.count() == 0
order = []
job = TranslateJob(win.items[0].view_path, [0, 1, 2], ("", str(tiny)), "auto", win.store)
job.pageDone.connect(order.append)
job.wanted = 2
job.work()
app.processEvents()
assert order[0] == 2 and sorted(order) == [0, 1, 2], order
# ---- sentence linking between the PDF and the translation
from PySide6.QtCore import QUrl
win.translate("document")
pump(lambda: win.active is None and not win.queue)
view.goto_page(0); app.processEvents()
win.panel_mode.setCurrentIndex(0); win.refresh_panel(); app.processEvents()
assert win._links and win._anchor_ranges, "no sentence links built"
assert set(win._links) <= set(win._anchor_ranges)
keys = sorted(win._links, key=lambda k: tuple(map(int, k.split("."))))
# hover over a word of the 3rd sentence in the PDF
key = keys[2]
x0, y0, x1, y1 = win._links[key][0]
win._pdf_hovered(0, (x0 + x1) / 2, (y0 + y1) / 2)
assert win._active_keys == (key,), (win._active_keys, key)
assert view._linked and view._linked[1] == win._links[key]
assert len(win.panel.extraSelections()) == 1
sels = win.panel.extraSelections()
hl = sels[0].cursor.selectedText()
assert hl and hl in win.panel.toPlainText()
# hover over another English sentence in the panel
other = keys[5]
win._panel_hovered("s:" + other)
assert win._active_keys == (other,) and view._linked[1] == win._links[other]
# leaving clears
win._panel_hovered("")
assert win._active_keys == () and view._linked is None
# selecting text in the PDF highlights the translations of the sentences it touches
r = view._page_rect(0); lay = view.layout(0); blk = lay.blocks[1]
mouse(QEvent.MouseButtonPress, vp(blk.x0 + 5, blk.lines[1].y0 + 3))
mouse(QEvent.MouseMove, vp(blk.x1 - 5, blk.lines[6].y1 - 2))
mouse(QEvent.MouseButtonRelease, vp(blk.x1 - 5, blk.lines[6].y1 - 2), Qt.NoButton)
app.processEvents()
assert len(win._sel_keys) >= 2 and len(win.panel.extraSelections()) == len(win._sel_keys), win._sel_keys
view.clear_selection(); app.processEvents()
assert win._sel_keys == ()
# clicking an English sentence scrolls the PDF to it
view.goto_page(0); app.processEvents()
assert win._links_page == 0
win._panel_clicked(QUrl("s:" + keys[-1]))
assert win._active_keys == (keys[-1],)
lr = win._links[keys[-1]][0]
top = view.tops[0] + lr[1] * view.zoom
assert view.verticalScrollBar().value() <= top <= view.verticalScrollBar().value() + view.viewport().height()
# turning linking off
win.a_link.setChecked(False)
win._pdf_hovered(0, (x0 + x1) / 2, (y0 + y1) / 2)
assert win._active_keys == ()
win.a_link.setChecked(True)
print("   sentence linking OK:", len(win._links), "sentences linked on page 1")

# progress with byte counts above 2^31 (a 3.3 GB model) must not overflow the Qt signal
from paraq.ui.workers import DownloadJob
got = []
dj = DownloadJob("gemma-4b"); dj.progress.connect(lambda *a: got.append(a))
dj.report("Downloading translation model", 3_300_000_000, 8_100_000_000)
assert got and got[0][1] == 3_300_000_000.0, got
win.active = dj; dj_sender = win.sender; win.sender = lambda: dj; win._job_progress("Downloading translation model", 3_300_000_000.0, 8_100_000_000.0); win.active = None; win.sender = dj_sender
assert "3300 of 8100 MB" in win.job_label.text(), win.job_label.text()
print("   large progress values OK")

# ---- translated PDF exports through the app (table cells via the tiny stand-in model)
for kind, name in (("pdf_translated", "doc_en.pdf"), ("pdf_side", "doc_side.pdf")):
    job = ExportJob(kind, win.items[0].view_path, str(work / name), "scan_2col_mixed", win.store, model=("", str(tiny)))
    win.enqueue(job)
    pump(lambda: win.active is None and not win.queue)
    out = pymupdf.open(str(work / name))
    assert out.page_count == 3, (name, out.page_count)
import pymupdf as _p
en_doc = _p.open(str(work / "doc_en.pdf"))
orig = _p.open(win.items[0].view_path)
assert en_doc[0].get_text() != orig[0].get_text() and len(en_doc[0].get_images()) == len(orig[0].get_images())
print("   PDF exports through the app OK")

# ---- TranslateGemma engine (stand-in Ollama server) on the real article, for a screenshot
from fake_ollama import FakeOllama
import paraq.translate as T
fake = FakeOllama(); os.environ["OLLAMA_HOST"] = fake.host; T._loaded.clear()
ww2 = Path(sys.argv[3]) if len(sys.argv) > 3 else None
if ww2 and ww2.exists():
    shutil.copy(ww2, work / "article.pdf")
    win.settings.set("translation_model_dir", "")
    win.settings.set("translation_model", "gemma-4b")
    win.settings.set("translation_model_chosen", True)
    win.add_paths([str(work / "article.pdf")])
    pump(lambda: win.active is None and all(it.state != "checking" for it in win.items))
    win.list.setCurrentRow(len(win.items) - 1); app.processEvents()
    view.goto_page(2); app.processEvents()
    win.a_columns.setChecked(False)
    win.translate("page")
    pump(lambda: win.active is None and not win.queue)
    assert "[EN]" in win.panel.toPlainText(), win.panel.toPlainText()[:200]
    keys = sorted(win._links, key=lambda k: tuple(map(int, k.split("."))))
    key = keys[len(keys) // 2]
    x0, y0, x1, y1 = win._links[key][0]
    view.scroll_to_rect(win._links_page, win._links[key][0]); app.processEvents()
    win._pdf_hovered(win._links_page, (x0 + x1) / 2, (y0 + y1) / 2); app.processEvents()
    win.grab().save(str(shots / "9_linked_sentence.png"))
    print("   TranslateGemma page in the app OK:", len(keys), "sentences linked; prompts sent:", len(fake.prompts))
fake.close()
print("GUI smoke test passed")
win.close()
