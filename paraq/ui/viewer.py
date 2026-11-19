"""Continuous-scroll PDF view with column-aware text selection."""

from __future__ import annotations

from bisect import bisect_right
from collections import OrderedDict

import pymupdf
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter, QPalette, QPen
from PySide6.QtWidgets import QAbstractScrollArea, QMenu

from ..layout import PageLayout, Pos
from ..pdfutil import FITZ_LOCK, page_layout

GAP = 14
MIN_ZOOM, MAX_ZOOM = 0.25, 6.0
SEL_COLOR = QColor(40, 120, 255, 85)
HIT_COLOR = QColor(255, 210, 0, 110)
HIT_CURRENT = QColor(255, 130, 0, 150)
LINK_COLOR = QColor(255, 196, 0, 95)  # sentence linked to the translation
COLUMN_COLORS = [QColor(0, 150, 136), QColor(233, 30, 99), QColor(63, 81, 181), QColor(255, 152, 0)]


class PdfView(QAbstractScrollArea):
    pageChanged = Signal(int)  # zero-based page in the middle of the view
    selectionChanged = Signal(str)
    zoomChanged = Signal(float)
    translateRequested = Signal(str)
    hovered = Signal(int, float, float)  # page and PDF point under the mouse; page -1 when it leaves

    def __init__(self, parent=None):
        super().__init__(parent)
        self.doc: pymupdf.Document | None = None
        self.path: str | None = None
        self.sizes: list[tuple[float, float]] = []
        self.tops: list[float] = []
        self.zoom = 1.0
        self.fit_width = True
        self.confine = True
        self.reflow = True
        self.show_columns = False
        self._images: OrderedDict[tuple[int, int], QImage] = OrderedDict()
        self._layouts: OrderedDict[int, PageLayout] = OrderedDict()  # recently viewed pages only
        self._pages_parsed = 0
        self._sel_page: int | None = None
        self._anchor: Pos | None = None
        self._focus: Pos | None = None
        self._dragging = False
        self._current = -1
        self._query = ""
        self._hits: dict[int, list[pymupdf.Rect]] = {}
        self._hit: tuple[int, int] | None = None
        self._linked: tuple[int, list] | None = None  # (page, rects) of the linked sentence(s)
        self._content_w = 0.0
        self._total_h = 0.0
        self.viewport().setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.verticalScrollBar().valueChanged.connect(self._scrolled)
        self.horizontalScrollBar().valueChanged.connect(lambda _v: self.viewport().update())
        self.verticalScrollBar().setSingleStep(40)
        self.horizontalScrollBar().setSingleStep(40)
        self._autoscroll = QTimer(self)
        self._autoscroll.setInterval(40)
        self._autoscroll.timeout.connect(self._autoscroll_tick)
        self._last_mouse = QPointF()

    # ------------------------------------------------------------ document
    def set_document(self, path: str | None, keep_position: bool = False) -> None:
        page = self.current_page() if keep_position else 0
        with FITZ_LOCK:
            if self.doc is not None:
                self.doc.close()
                self.doc = None
            self.sizes = []
            self.path = path
            if path:
                self.doc = pymupdf.open(path)
                self.sizes = [(p.rect.width, p.rect.height) for p in self.doc]
        self._images.clear()
        self._layouts.clear()
        self._hits.clear()
        self._hit = None
        self._linked = None
        self.clear_selection()
        self._current = -1
        self._relayout()
        self.goto_page(min(page, max(0, len(self.sizes) - 1)))
        self._scrolled()
        self.viewport().update()

    def page_count(self) -> int:
        return len(self.sizes)

    MAX_LAYOUTS = 60  # text layout of recently viewed pages (about 0.4 MB each for a dense page)

    def layout(self, pno: int) -> PageLayout:
        lay = self._layouts.get(pno)
        if lay is not None:
            self._layouts.move_to_end(pno)
            return lay
        with FITZ_LOCK:
            lay = page_layout(self.doc[pno]) if self.doc is not None else PageLayout()
        self._layouts[pno] = lay
        keep = {pno, self._sel_page}
        while len(self._layouts) > self.MAX_LAYOUTS:
            old = next(k for k in self._layouts if k not in keep)
            del self._layouts[old]
        self._pages_parsed += 1
        if self._pages_parsed >= 150:
            self._refresh_document()
        return lay

    def _refresh_document(self) -> None:
        """Reopen the PDF: an open document keeps every page it has parsed, which adds up
        while scrolling through a long book. Layouts and rendered images stay valid."""
        self._pages_parsed = 0
        if not self.path:
            return
        with FITZ_LOCK:
            try:
                fresh = pymupdf.open(self.path)
            except Exception:  # noqa: BLE001 - keep the open one
                return
            old, self.doc = self.doc, fresh
            if old is not None:
                old.close()

    # ------------------------------------------------------------- geometry
    def _relayout(self) -> None:
        vw = self.viewport().width()
        if self.fit_width and self.sizes:
            widest = max(w for w, _h in self.sizes)
            self.zoom = max(MIN_ZOOM, min(MAX_ZOOM, (vw - 2 * GAP) / widest))
        self.tops = []
        y = GAP
        widest_px = 0.0
        for w, h in self.sizes:
            self.tops.append(y)
            y += h * self.zoom + GAP
            widest_px = max(widest_px, w * self.zoom)
        self._total_h = y
        self._content_w = max(vw, widest_px + 2 * GAP)
        vb, hb = self.verticalScrollBar(), self.horizontalScrollBar()
        vb.setRange(0, max(0, int(self._total_h - self.viewport().height())))
        vb.setPageStep(self.viewport().height())
        hb.setRange(0, max(0, int(self._content_w - vw)))
        hb.setPageStep(vw)

    def _page_rect(self, pno: int) -> QRectF:
        """Rectangle of a page in viewport coordinates."""
        w, h = self.sizes[pno]
        pw, ph = w * self.zoom, h * self.zoom
        x = (self._content_w - pw) / 2 - self.horizontalScrollBar().value()
        y = self.tops[pno] - self.verticalScrollBar().value()
        return QRectF(x, y, pw, ph)

    def _visible_pages(self) -> range:
        if not self.sizes:
            return range(0)
        top = self.verticalScrollBar().value()
        bottom = top + self.viewport().height()
        first = max(0, bisect_right(self.tops, top) - 1)
        last = max(first, bisect_right(self.tops, bottom) - 1)
        return range(first, min(last, len(self.sizes) - 1) + 1)

    def _page_at(self, pos: QPointF) -> tuple[int, float, float] | None:
        """Page under (or nearest to) a viewport point, with the point in PDF coordinates."""
        if not self.sizes:
            return None
        y = pos.y() + self.verticalScrollBar().value()
        pno = max(0, min(len(self.sizes) - 1, bisect_right(self.tops, y) - 1))
        r = self._page_rect(pno)
        return pno, (pos.x() - r.x()) / self.zoom, (pos.y() - r.y()) / self.zoom

    def current_page(self) -> int:
        if not self.sizes:
            return 0
        mid = self.verticalScrollBar().value() + self.viewport().height() / 2
        return max(0, min(len(self.sizes) - 1, bisect_right(self.tops, mid) - 1))

    def goto_page(self, pno: int) -> None:
        if self.sizes:
            pno = max(0, min(pno, len(self.sizes) - 1))
            self.verticalScrollBar().setValue(int(self.tops[pno] - GAP / 2))

    def _scrolled(self) -> None:
        cur = self.current_page() if self.sizes else -1
        if cur != self._current:
            self._current = cur
            self.pageChanged.emit(cur)
        self.viewport().update()

    def set_zoom(self, zoom: float, fit: bool = False) -> None:
        vb = self.verticalScrollBar()
        frac = (vb.value() + self.viewport().height() / 2) / max(self._total_h, 1.0)
        self.fit_width = fit
        if not fit:
            self.zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))
        self._relayout()
        vb.setValue(int(frac * self._total_h - self.viewport().height() / 2))
        self.zoomChanged.emit(self.zoom)
        self.viewport().update()

    def zoom_in(self) -> None:
        self.set_zoom(self.zoom * 1.2)

    def zoom_out(self) -> None:
        self.set_zoom(self.zoom / 1.2)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        page = self.current_page()
        self._relayout()
        if self.fit_width:
            self.goto_page(page)
            self.zoomChanged.emit(self.zoom)

    # -------------------------------------------------------------- painting
    def _image(self, pno: int) -> QImage:
        dpr = self.devicePixelRatioF()
        scale = self.zoom * dpr
        w, h = self.sizes[pno]
        # Keep single renders to a sane size on very large pages / high zoom.
        scale = min(scale, 6000.0 / max(w, h))
        key = (pno, int(scale * 1000))
        img = self._images.get(key)
        if img is not None:
            self._images.move_to_end(key)
            return img
        with FITZ_LOCK:
            pix = self.doc[pno].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888).copy()
        self._images[key] = img
        while len(self._images) > 12:
            self._images.popitem(last=False)
        self._pages_parsed += 1
        if self._pages_parsed >= 150:
            self._refresh_document()
        return img

    def paintEvent(self, _event) -> None:
        p = QPainter(self.viewport())
        p.fillRect(self.viewport().rect(), self.palette().color(QPalette.Mid))
        if self.doc is None:
            p.setPen(self.palette().color(QPalette.WindowText))
            p.drawText(self.viewport().rect(), Qt.AlignCenter, "Add a PDF or a scanned image to begin\n\n(drag files here, or use Add Files)")
            return
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        for pno in self._visible_pages():
            r = self._page_rect(pno)
            img = self._image(pno)
            p.drawImage(r, img, QRectF(img.rect()))
            z = self.zoom
            if self._query:
                for i, hit in enumerate(self._page_hits(pno)):
                    color = HIT_CURRENT if self._hit == (pno, i) else HIT_COLOR
                    p.fillRect(QRectF(r.x() + hit.x0 * z, r.y() + hit.y0 * z, hit.width * z, hit.height * z), color)
            if self._linked is not None and self._linked[0] == pno:
                for x0, y0, x1, y1 in self._linked[1]:
                    p.fillRect(QRectF(r.x() + x0 * z, r.y() + y0 * z, (x1 - x0) * z, (y1 - y0) * z), LINK_COLOR)
            if self.show_columns:
                self._paint_columns(p, pno, r)
            if self._sel_page == pno and self._anchor is not None and self._focus is not None:
                lay = self.layout(pno)
                for x0, y0, x1, y1 in lay.selection_rects(self._anchor, self._focus):
                    p.fillRect(QRectF(r.x() + x0 * z, r.y() + y0 * z, (x1 - x0) * z, (y1 - y0) * z), SEL_COLOR)

    def _paint_columns(self, p: QPainter, pno: int, r: QRectF) -> None:
        z = self.zoom
        font = QFont(self.font())
        font.setBold(True)
        p.setFont(font)
        for i, b in enumerate(self.layout(pno).blocks):
            color = COLUMN_COLORS[i % len(COLUMN_COLORS)]
            p.setPen(QPen(color, 1.5))
            rect = QRectF(r.x() + b.x0 * z - 3, r.y() + b.y0 * z - 3, (b.x1 - b.x0) * z + 6, (b.y1 - b.y0) * z + 6)
            p.drawRect(rect)
            tag = QRectF(rect.x(), rect.y() - 16, 22, 16)
            p.fillRect(tag, color)
            p.setPen(Qt.white)
            p.drawText(tag, Qt.AlignCenter, str(i + 1))

    # ------------------------------------------------------------- selection
    def clear_selection(self) -> None:
        had = self._anchor is not None
        self._sel_page = self._anchor = self._focus = None
        if had:
            self.selectionChanged.emit("")
            self.viewport().update()

    def selected_text(self) -> str:
        if self._sel_page is None or self._anchor is None or self._focus is None or self._anchor == self._focus:
            return ""
        return self.layout(self._sel_page).selection_text(self._anchor, self._focus, self.reflow)

    def copy_selection(self) -> None:
        text = self.selected_text()
        if text:
            QGuiApplication.clipboard().setText(text)

    def select_all_on_page(self) -> None:
        if not self.sizes:
            return
        pno = self.current_page()
        lay = self.layout(pno)
        if lay.blocks:
            self._sel_page = pno
            self._anchor = lay.start_of(0)
            self._focus = lay.end_of(len(lay.blocks) - 1)
            self._emit_selection()

    def select_block(self, pno: int, block: int) -> None:
        lay = self.layout(pno)
        self._sel_page = pno
        self._anchor = lay.start_of(block)
        self._focus = lay.end_of(block)
        self._emit_selection()

    def _emit_selection(self) -> None:
        self.selectionChanged.emit(self.selected_text())
        self.viewport().update()

    def _focus_from(self, pos: QPointF) -> None:
        if self._sel_page is None or self._anchor is None:
            return
        r = self._page_rect(self._sel_page)
        x, y = (pos.x() - r.x()) / self.zoom, (pos.y() - r.y()) / self.zoom
        lay = self.layout(self._sel_page)
        block = self._anchor[0] if self.confine else None
        focus = lay.position_at(x, y, block)
        if focus is not None and focus != self._focus:
            self._focus = focus
            self._emit_selection()

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton or self.doc is None:
            return super().mousePressEvent(event)
        self.setFocus()
        pos = event.position()
        hit = self._page_at(pos)
        if hit is None:
            return
        pno, x, y = hit
        if event.modifiers() & Qt.ShiftModifier and self._sel_page == pno and self._anchor is not None:
            self._focus_from(pos)
            return
        lay = self.layout(pno)
        start = lay.position_at(x, y)
        if start is None:
            self.clear_selection()
            return
        self._sel_page, self._anchor, self._focus = pno, start, start
        self._dragging = True
        self._emit_selection()

    def mouseMoveEvent(self, event) -> None:
        pos = event.position()
        if self._dragging:
            self._last_mouse = pos
            self._focus_from(pos)
            outside = pos.y() < 0 or pos.y() > self.viewport().height()
            if outside and not self._autoscroll.isActive():
                self._autoscroll.start()
            elif not outside:
                self._autoscroll.stop()
            return
        hit = self._page_at(pos)
        if hit is not None and self.doc is not None:
            self.hovered.emit(*hit)
        over_text = False
        if hit is not None and self.doc is not None:
            pno, x, y = hit
            lay = self._layouts.get(pno)
            if lay is not None:
                over_text = any(b.x0 - 2 <= x <= b.x1 + 2 and b.y0 - 2 <= y <= b.y1 + 2 for b in lay.blocks)
            elif 0 <= x <= self.sizes[pno][0] and 0 <= y <= self.sizes[pno][1]:
                self.layout(pno)
        self.viewport().setCursor(Qt.IBeamCursor if over_text else Qt.ArrowCursor)

    def leaveEvent(self, event) -> None:
        self.hovered.emit(-1, 0.0, 0.0)
        super().leaveEvent(event)

    # -------------------------------------------------------------- linking
    def set_linked(self, pno: int | None, rects: list) -> None:
        new = (pno, list(rects)) if pno is not None and rects else None
        if new != self._linked:
            self._linked = new
            self.viewport().update()

    def scroll_to_rect(self, pno: int, rect) -> None:
        """Bring a PDF rectangle into view if it is not visible already."""
        if not self.sizes:
            return
        top = self.tops[pno] + rect[1] * self.zoom
        bottom = self.tops[pno] + rect[3] * self.zoom
        vb = self.verticalScrollBar()
        if top < vb.value() or bottom > vb.value() + self.viewport().height():
            vb.setValue(int(top - self.viewport().height() / 3))

    def selected_words(self) -> set[int]:
        """ids of the Word objects in the current selection (for linking to the translation)."""
        if self._sel_page is None or self._anchor is None or self._focus is None or self._anchor == self._focus:
            return set()
        lay = self.layout(self._sel_page)
        out = set()
        for _bi, line, w0, _c0, w1, _c1 in lay._iter_selection(self._anchor, self._focus):
            out.update(id(line.words[k]) for k in range(w0, w1 + 1))
        return out

    def selection_page(self) -> int | None:
        return self._sel_page

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._dragging = False
            self._autoscroll.stop()
            if self._anchor == self._focus:
                self.clear_selection()

    def mouseDoubleClickEvent(self, event) -> None:
        hit = self._page_at(event.position())
        if hit is None or self.doc is None:
            return
        pno, x, y = hit
        word = self.layout(pno).word_at(x, y)
        if word:
            self._sel_page, (self._anchor, self._focus) = pno, word
            self._emit_selection()

    def _autoscroll_tick(self) -> None:
        y = self._last_mouse.y()
        vb = self.verticalScrollBar()
        if y < 0:
            vb.setValue(vb.value() - 30)
        elif y > self.viewport().height():
            vb.setValue(vb.value() + 30)
        self._focus_from(self._last_mouse)

    def contextMenuEvent(self, event) -> None:
        if self.doc is None:
            return
        menu = QMenu(self)
        text = self.selected_text()
        copy = menu.addAction("Copy")
        copy.setEnabled(bool(text))
        copy.triggered.connect(self.copy_selection)
        tr = menu.addAction("Translate Selection")
        tr.setEnabled(bool(text))
        tr.triggered.connect(lambda: self.translateRequested.emit(self.selected_text()))
        menu.addSeparator()
        hit = self._page_at(QPointF(self.viewport().mapFromGlobal(event.globalPos())))
        if hit is not None:
            pno, x, y = hit
            lay = self.layout(pno)
            if lay.blocks:
                block = lay.block_at(x, y)
                menu.addAction("Select This Column").triggered.connect(lambda: self.select_block(pno, block))
        menu.addAction("Select All on Page").triggered.connect(self.select_all_on_page)
        menu.exec(event.globalPos())

    # ---------------------------------------------------------------- search
    def _page_hits(self, pno: int) -> list[pymupdf.Rect]:
        hits = self._hits.get(pno)
        if hits is None:
            with FITZ_LOCK:
                hits = self.doc[pno].search_for(self._query) if self._query else []
            self._hits[pno] = hits
        return hits

    def find(self, query: str, backwards: bool = False) -> bool:
        """Jump to the next (or previous) match; returns False when there is none."""
        query = query.strip()
        if query != self._query:
            self._query = query
            self._hits.clear()
            self._hit = None
        if not query or not self.sizes:
            self.viewport().update()
            return False
        n = len(self.sizes)
        step = -1 if backwards else 1
        if self._hit is not None:
            pno, idx = self._hit[0], self._hit[1] + step
        else:
            pno, idx = self.current_page(), None  # None = first (or last) match of the page
        for _ in range(n + 1):
            hits = self._page_hits(pno)
            if idx is None:
                idx = len(hits) - 1 if backwards else 0
            if 0 <= idx < len(hits):
                self._hit = (pno, idx)
                r = hits[idx]
                target = self.tops[pno] + r.y0 * self.zoom - self.viewport().height() / 3
                self.verticalScrollBar().setValue(int(target))
                self.viewport().update()
                return True
            pno = (pno + step) % n
            idx = None
        self._hit = None
        self.viewport().update()
        return False

    # ---------------------------------------------------------------- events
    def wheelEvent(self, event) -> None:
        if event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier):
            delta = event.angleDelta().y()
            if delta:
                self.set_zoom(self.zoom * (1.0 + max(-0.5, min(0.5, delta / 600.0))))
            event.accept()
            return
        super().wheelEvent(event)

    def viewportEvent(self, event) -> bool:
        if event.type() == QEvent.NativeGesture and event.gestureType() == Qt.ZoomNativeGesture:
            self.set_zoom(self.zoom * (1.0 + event.value()))
            return True
        return super().viewportEvent(event)

    def keyPressEvent(self, event) -> None:
        vb = self.verticalScrollBar()
        key = event.key()
        if key in (Qt.Key_PageDown, Qt.Key_Space):
            vb.setValue(vb.value() + int(self.viewport().height() * 0.9))
        elif key == Qt.Key_PageUp:
            vb.setValue(vb.value() - int(self.viewport().height() * 0.9))
        elif key == Qt.Key_Home:
            vb.setValue(0)
        elif key == Qt.Key_End:
            vb.setValue(vb.maximum())
        elif key == Qt.Key_Down:
            vb.setValue(vb.value() + 40)
        elif key == Qt.Key_Up:
            vb.setValue(vb.value() - 40)
        elif key == Qt.Key_Escape:
            self.clear_selection()
        else:
            super().keyPressEvent(event)
