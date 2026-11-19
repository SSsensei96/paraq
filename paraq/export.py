"""Export recognised text and translations to .txt and .docx."""

from __future__ import annotations

from pathlib import Path


def export_txt(path: Path, pages: list[tuple[int, list[str]]]) -> None:
    """pages: (page number starting at 1, paragraphs)."""
    chunks = []
    for number, paras in pages:
        chunks.append(f"--- Page {number} ---\n\n" + "\n\n".join(paras))
    Path(path).write_text("\n\n\n".join(chunks) + "\n", "utf-8")


def _page_label(doc, number: int):
    from docx.shared import Pt, RGBColor

    p = doc.add_paragraph()
    run = p.add_run(f"Page {number}")
    run.font.size = Pt(8)
    run.font.color.rgb = RGBColor(0x88, 0x88, 0x88)
    p.paragraph_format.space_before = Pt(10)
    p.paragraph_format.space_after = Pt(2)


def export_docx(path: Path, title: str, pages: list[tuple[int, list[str]]]) -> None:
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    doc.styles["Normal"].font.size = Pt(11)
    doc.add_heading(title, level=1)
    for number, paras in pages:
        _page_label(doc, number)
        for para in paras:
            doc.add_paragraph(para)
    doc.save(str(path))


def export_bilingual_docx(path: Path, title: str, pages: list[tuple[int, list[tuple[str, str]]]]) -> None:
    """pages: (page number, [(source paragraph, English paragraph), ...]) as a two-column table."""
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.shared import Pt

    doc = Document()
    section = doc.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width, section.page_height = section.page_height, section.page_width
    doc.styles["Normal"].font.size = Pt(10)
    doc.add_heading(title, level=1)
    for number, pairs in pages:
        _page_label(doc, number)
        if not pairs:
            continue
        table = doc.add_table(rows=len(pairs), cols=2)
        table.style = "Table Grid"
        for row, (src, en) in zip(table.rows, pairs):
            row.cells[0].text = src
            row.cells[1].text = en
    doc.save(str(path))
