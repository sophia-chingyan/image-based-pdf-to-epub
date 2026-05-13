"""
PDF Assembly
============
Two PDF output modes from OCR results:

1. Text-layer PDF (方案 B) — assemble_textlayer_pdf()
   Overlays invisible searchable text on the original scanned pages.
   Visual appearance identical to original; text is selectable/searchable.

2. Clean PDF (方案 A) — assemble_clean_pdf()
   Re-renders OCR text into a cleanly typeset PDF using ReportLab.
"""

from __future__ import annotations
import io
import logging
from pathlib import Path
from typing import List

import fitz  # PyMuPDF

from structure_analysis import DocumentStructure, StructuredPage

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 方案 B: Text-layer PDF
# ─────────────────────────────────────────────────────────────────────────────

def assemble_textlayer_pdf(
    structure: DocumentStructure,
    original_pdf_path: Path,
    output_path: Path,
) -> None:
    """Overlay invisible OCR text on each page of the original PDF."""
    logger.info(f"Assembling text-layer PDF: {output_path}")

    doc = fitz.open(str(original_pdf_path))

    for page_idx, struct_page in enumerate(structure.pages):
        if page_idx >= len(doc):
            break
        page = doc[page_idx]
        if not struct_page.elements:
            continue

        page_rect = page.rect
        total = len(struct_page.elements)

        for idx, el in enumerate(struct_page.elements):
            text = el.text.strip()
            if not text:
                continue

            # Distribute text vertically across the page
            y_ratio = (idx + 0.5) / max(total, 1)
            y_pos = page_rect.y0 + y_ratio * page_rect.height
            y_pos = max(page_rect.y0 + 10, min(y_pos, page_rect.y1 - 20))

            try:
                tw = fitz.TextWriter(page_rect)
                font = fitz.Font("china-s")
                fontsize = 8
                max_width = page_rect.width - 40

                lines = _wrap_text(text, font, fontsize, max_width)
                y_cursor = y_pos
                for line in lines:
                    if y_cursor > page_rect.y1 - 10:
                        break
                    try:
                        tw.append(pos=(page_rect.x0 + 20, y_cursor),
                                  text=line, font=font, fontsize=fontsize)
                    except Exception:
                        pass
                    y_cursor += fontsize * 1.4
                tw.write_text(page, color=(1, 1, 1), render_mode=3)
            except Exception:
                try:
                    page.insert_text(
                        point=(page_rect.x0 + 20, y_pos),
                        text=text[:200], fontsize=1, color=(1, 1, 1))
                except Exception:
                    pass

    doc.save(str(output_path), garbage=4, deflate=True)
    doc.close()
    logger.info(f"Text-layer PDF written: {output_path} ({output_path.stat().st_size/1024:.1f} KB)")


def _wrap_text(text: str, font, fontsize: float, max_width: float) -> List[str]:
    lines = []
    for raw_line in text.split("\n"):
        if not raw_line.strip():
            lines.append("")
            continue
        current = ""
        for char in raw_line:
            test = current + char
            try:
                w = font.text_length(test, fontsize=fontsize)
            except Exception:
                w = len(test) * fontsize * 0.6
            if w > max_width and current:
                lines.append(current)
                current = char
            else:
                current = test
        if current:
            lines.append(current)
    return lines if lines else [""]


# ─────────────────────────────────────────────────────────────────────────────
# 方案 A: Clean PDF — reflowed text with proper typography
# ─────────────────────────────────────────────────────────────────────────────

def assemble_clean_pdf(
    structure: DocumentStructure,
    output_path: Path,
) -> None:
    """
    Re-render OCR text into a cleanly typeset PDF.

    Tries ReportLab first for professional output. If that fails for ANY
    reason (empty document, font issues, XML parsing errors), falls back
    to PyMuPDF-based rendering which always succeeds.
    """
    logger.info(f"Assembling clean PDF: {output_path}")

    try:
        _assemble_clean_pdf_reportlab(structure, output_path)
        logger.info(f"Clean PDF written (ReportLab): {output_path} "
                     f"({output_path.stat().st_size/1024:.1f} KB)")
    except Exception as e:
        logger.warning(f"ReportLab build failed ({e}), falling back to PyMuPDF renderer")
        try:
            _assemble_clean_pdf_pymupdf(structure, output_path)
            logger.info(f"Clean PDF written (PyMuPDF fallback): {output_path} "
                         f"({output_path.stat().st_size/1024:.1f} KB)")
        except Exception as e2:
            logger.error(f"PyMuPDF fallback also failed ({e2}), writing minimal PDF")
            _write_minimal_pdf(output_path, structure.title or "Untitled",
                               f"PDF assembly error: {e}")


def _assemble_clean_pdf_reportlab(
    structure: DocumentStructure,
    output_path: Path,
) -> None:
    """ReportLab-based clean PDF. May raise on empty/problematic content."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, PageBreak, Image as RLImage,
    )
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    cjk_font = _register_best_font(pdfmetrics, UnicodeCIDFont)
    logger.info(f"Clean PDF using font: {cjk_font}")

    styles = getSampleStyleSheet()

    def _style(name, parent_name="Normal", **kw):
        parent = styles.get(parent_name, styles["Normal"])
        return ParagraphStyle(name, parent=parent, fontName=cjk_font, **kw)

    s_title = _style("T", "Title",   fontSize=18, leading=24, spaceAfter=12, alignment=TA_CENTER)
    s_h1    = _style("H1","Heading1",fontSize=16, leading=22, spaceBefore=14, spaceAfter=8)
    s_h2    = _style("H2","Heading2",fontSize=14, leading=19, spaceBefore=10, spaceAfter=6)
    s_h3    = _style("H3","Heading3",fontSize=12, leading=17, spaceBefore=8,  spaceAfter=4)
    s_body  = _style("B", fontSize=11, leading=18, firstLineIndent=22,
                     spaceBefore=2, spaceAfter=2, alignment=TA_JUSTIFY)
    s_fn    = _style("FN",fontSize=9,  leading=13, textColor="#555555")
    s_pn    = _style("PN",fontSize=9,  leading=12, textColor="#888888", alignment=TA_CENTER)
    s_cap   = _style("C", fontSize=10, leading=14, textColor="#666666", alignment=TA_CENTER)
    s_li    = _style("LI",fontSize=11, leading=18, leftIndent=20, bulletIndent=10)
    s_auth  = _style("A", fontSize=12, leading=18, alignment=TA_CENTER)
    hs = {1: s_h1, 2: s_h2, 3: s_h3}

    # ── Collect ALL renderable paragraphs first, then build story ────────────
    # This avoids the "Document is empty" error by ensuring we have real
    # content before adding any PageBreaks.
    content_paragraphs: list = []   # list of flowable-lists, one per page

    for page in structure.pages:
        page_items: list = []

        for img in page.images:
            if img.image_bytes:
                try:
                    page_items.append(
                        RLImage(io.BytesIO(img.image_bytes),
                                width=150 * mm, height=200 * mm, kind="proportional")
                    )
                    page_items.append(Spacer(1, 3 * mm))
                except Exception as e:
                    logger.warning(f"Could not embed image in clean PDF: {e}")

        for el in page.elements:
            t = el.text.strip()
            if not t:
                continue
            safe = _esc(t)
            try:
                if el.element_type == "heading":
                    page_items.append(Paragraph(safe, hs.get(min(el.level, 3), s_h3)))
                elif el.element_type == "paragraph":
                    if el.href:
                        safe = f'<a href="{_esc(el.href)}" color="blue">{safe}</a>'
                    page_items.append(Paragraph(safe, s_body))
                elif el.element_type == "list-item":
                    page_items.append(Paragraph(f"\u2022 {safe}", s_li))
                elif el.element_type == "footnote":
                    page_items.append(Paragraph(safe, s_fn))
                elif el.element_type == "page-number":
                    page_items.append(Paragraph(safe, s_pn))
                elif el.element_type == "caption":
                    page_items.append(Paragraph(safe, s_cap))
                else:
                    page_items.append(Paragraph(safe, s_body))
            except Exception as e:
                logger.warning(f"Skipping element: {e} — text: {t[:50]!r}")

        if page_items:
            content_paragraphs.append(page_items)

    # ── Build story: title page + content pages ──────────────────────────────
    story: list = []

    if structure.title:
        story.append(Spacer(1, 40 * mm))
        story.append(Paragraph(_esc(structure.title), s_title))
        if structure.author:
            story.append(Spacer(1, 5 * mm))
            story.append(Paragraph(_esc(structure.author), s_auth))

    if content_paragraphs:
        # Add page break after title only if there's content following
        if story:
            story.append(PageBreak())
        for i, page_items in enumerate(content_paragraphs):
            story.extend(page_items)
            # Page break between content pages, but NOT after the last one
            if i < len(content_paragraphs) - 1:
                story.append(PageBreak())
    else:
        # No content at all — add a placeholder
        if story:
            story.append(Spacer(1, 10 * mm))
        story.append(Paragraph(
            "[ No body text was extracted from this PDF ]", s_body
        ))

    # ── Final safety: ensure story is never empty ────────────────────────────
    if not story:
        story.append(Paragraph(
            _esc(structure.title or "Untitled"), s_title
        ))
        story.append(Spacer(1, 10 * mm))
        story.append(Paragraph(
            "[ No text content could be extracted from this PDF ]", s_body
        ))

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=25 * mm, rightMargin=25 * mm,
        topMargin=20 * mm, bottomMargin=20 * mm,
        title=structure.title or "Untitled",
        author=structure.author or "",
    )
    doc.build(story)


def _assemble_clean_pdf_pymupdf(
    structure: DocumentStructure,
    output_path: Path,
) -> None:
    """
    Fallback clean PDF renderer using only PyMuPDF.
    Less pretty than ReportLab but handles CJK text reliably and never
    throws "Document is empty".
    """
    doc = fitz.open()
    font = fitz.Font("china-s")

    title = structure.title or "Untitled"
    author = structure.author or ""

    # ── Title page ────────────────────────────────────────────────────────────
    page = doc.new_page(width=595, height=842)  # A4 in points
    try:
        tw = fitz.TextWriter(page.rect)
        tw.append(pos=(72, 200), text=title[:100], font=font, fontsize=20)
        if author:
            tw.append(pos=(72, 240), text=author[:100], font=font, fontsize=14)
        tw.write_text(page)
    except Exception:
        page.insert_text((72, 200), title[:100], fontsize=20)
        if author:
            page.insert_text((72, 240), author[:100], fontsize=14)

    # ── Content pages ─────────────────────────────────────────────────────────
    has_any_content = False

    for struct_page in structure.pages:
        if not struct_page.elements and not struct_page.images:
            continue

        page = doc.new_page(width=595, height=842)
        y_cursor = 60.0
        margin_left = 50.0
        max_width = 495.0  # 595 - 2*50

        for el in struct_page.elements:
            text = el.text.strip()
            if not text:
                continue
            has_any_content = True

            if el.element_type == "heading":
                fs = 16 if el.level == 1 else 14 if el.level == 2 else 12
                y_cursor += 8
            elif el.element_type == "footnote":
                fs = 9
            elif el.element_type == "page-number":
                fs = 8
            elif el.element_type == "caption":
                fs = 10
            else:
                fs = 11

            lines = _wrap_text_fitz(text, font, fs, max_width)
            for line in lines:
                if y_cursor > 790:
                    page = doc.new_page(width=595, height=842)
                    y_cursor = 60.0
                try:
                    tw = fitz.TextWriter(page.rect)
                    tw.append(pos=(margin_left, y_cursor), text=line,
                              font=font, fontsize=fs)
                    tw.write_text(page)
                except Exception:
                    try:
                        page.insert_text((margin_left, y_cursor),
                                         line[:200], fontsize=fs)
                    except Exception:
                        pass
                y_cursor += fs * 1.5
            y_cursor += 4

        for img in struct_page.images:
            if not img.image_bytes:
                continue
            has_any_content = True
            try:
                if y_cursor > 600:
                    page = doc.new_page(width=595, height=842)
                    y_cursor = 60.0
                img_rect = fitz.Rect(margin_left, y_cursor,
                                     margin_left + 400, y_cursor + 300)
                page.insert_image(img_rect, stream=img.image_bytes)
                y_cursor += 310
            except Exception as e:
                logger.warning(f"Could not embed image in PyMuPDF PDF: {e}")

    if not has_any_content:
        title_page = doc[0]
        try:
            tw = fitz.TextWriter(title_page.rect)
            tw.append(pos=(72, 300),
                      text="[ No text content could be extracted ]",
                      font=font, fontsize=12)
            tw.write_text(title_page)
        except Exception:
            title_page.insert_text((72, 300),
                                    "[ No text content could be extracted ]",
                                    fontsize=12)

    doc.save(str(output_path), garbage=4, deflate=True)
    doc.close()


def _wrap_text_fitz(text: str, font, fontsize: float, max_width: float) -> List[str]:
    """Wrap text to fit within max_width using PyMuPDF font metrics."""
    lines = []
    for raw_line in text.split("\n"):
        if not raw_line.strip():
            lines.append("")
            continue
        current = ""
        for char in raw_line:
            test = current + char
            try:
                w = font.text_length(test, fontsize=fontsize)
            except Exception:
                w = len(test) * fontsize * 0.6
            if w > max_width and current:
                lines.append(current)
                current = char
            else:
                current = test
        if current:
            lines.append(current)
    return lines if lines else [""]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _register_best_font(pdfmetrics, UnicodeCIDFont) -> str:
    """Try CJK CID fonts in order; return the name of the first that works."""
    candidates = ["STSong-Light", "MSung-Light", "HeiseiMin-W3", "HYSMyeongJo-Medium"]
    for fname in candidates:
        try:
            pdfmetrics.registerFont(UnicodeCIDFont(fname))
            return fname
        except Exception:
            continue
    return "Helvetica"


def _esc(text: str) -> str:
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;"))


def _write_minimal_pdf(output_path: Path, title: str, message: str) -> None:
    """Write a bare-minimum valid PDF using only PyMuPDF."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), title[:80],   fontsize=16)
    page.insert_text((72, 140), message[:200], fontsize=10)
    doc.save(str(output_path))
    doc.close()
