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
    """Re-render OCR text into a cleanly typeset PDF using ReportLab."""
    logger.info(f"Assembling clean PDF: {output_path}")

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, PageBreak, Image as RLImage
    )
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    # Register CJK font
    cjk_font = "Helvetica"
    for fname in ("STSong-Light", "MSung-Light"):
        try:
            pdfmetrics.registerFont(UnicodeCIDFont(fname))
            cjk_font = fname
            break
        except Exception:
            continue

    styles = getSampleStyleSheet()
    s_title = ParagraphStyle("T", parent=styles["Title"], fontName=cjk_font, fontSize=18, leading=24, spaceAfter=12, alignment=TA_CENTER)
    s_h1 = ParagraphStyle("H1", parent=styles["Heading1"], fontName=cjk_font, fontSize=16, leading=22, spaceBefore=14, spaceAfter=8)
    s_h2 = ParagraphStyle("H2", parent=styles["Heading2"], fontName=cjk_font, fontSize=14, leading=19, spaceBefore=10, spaceAfter=6)
    s_h3 = ParagraphStyle("H3", parent=styles["Heading3"], fontName=cjk_font, fontSize=12, leading=17, spaceBefore=8, spaceAfter=4)
    s_body = ParagraphStyle("B", parent=styles["Normal"], fontName=cjk_font, fontSize=11, leading=18, firstLineIndent=22, spaceBefore=2, spaceAfter=2, alignment=TA_JUSTIFY)
    s_fn = ParagraphStyle("FN", parent=styles["Normal"], fontName=cjk_font, fontSize=9, leading=13, textColor="#555555")
    s_pn = ParagraphStyle("PN", parent=styles["Normal"], fontName=cjk_font, fontSize=9, leading=12, textColor="#888888", alignment=TA_CENTER)
    s_cap = ParagraphStyle("C", parent=styles["Normal"], fontName=cjk_font, fontSize=10, leading=14, textColor="#666666", alignment=TA_CENTER)
    s_li = ParagraphStyle("LI", parent=styles["Normal"], fontName=cjk_font, fontSize=11, leading=18, leftIndent=20, bulletIndent=10)
    hs = {1: s_h1, 2: s_h2, 3: s_h3}

    story = []
    if structure.title:
        story.append(Spacer(1, 40*mm))
        story.append(Paragraph(_esc(structure.title), s_title))
        if structure.author:
            s_auth = ParagraphStyle("A", parent=s_body, alignment=TA_CENTER, fontSize=12)
            story.append(Spacer(1, 5*mm))
            story.append(Paragraph(_esc(structure.author), s_auth))
        story.append(PageBreak())

    has_content = False
    for page in structure.pages:
        page_has = False
        for img in page.images:
            if img.image_bytes:
                try:
                    story.append(RLImage(io.BytesIO(img.image_bytes), width=150*mm, height=200*mm, kind="proportional"))
                    story.append(Spacer(1, 3*mm))
                    page_has = True
                except Exception:
                    pass
        for el in page.elements:
            t = el.text.strip()
            if not t:
                continue
            safe = _esc(t)
            page_has = True
            if el.element_type == "heading":
                story.append(Paragraph(safe, hs.get(min(el.level, 3), s_h3)))
            elif el.element_type == "paragraph":
                if el.href:
                    safe = f'<a href="{_esc(el.href)}" color="blue">{safe}</a>'
                story.append(Paragraph(safe, s_body))
            elif el.element_type == "list-item":
                story.append(Paragraph(f"• {safe}", s_li))
            elif el.element_type == "footnote":
                story.append(Paragraph(safe, s_fn))
            elif el.element_type == "page-number":
                story.append(Paragraph(safe, s_pn))
            elif el.element_type == "caption":
                story.append(Paragraph(safe, s_cap))
            else:
                story.append(Paragraph(safe, s_body))
        if page_has:
            has_content = True
            story.append(PageBreak())

    if not has_content:
        story.append(Paragraph("[ No text content could be extracted ]", s_body))

    doc = SimpleDocTemplate(str(output_path), pagesize=A4,
        leftMargin=25*mm, rightMargin=25*mm, topMargin=20*mm, bottomMargin=20*mm,
        title=structure.title or "Untitled", author=structure.author or "")
    doc.build(story)
    logger.info(f"Clean PDF written: {output_path} ({output_path.stat().st_size/1024:.1f} KB)")


def _esc(text: str) -> str:
    return text.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;").replace("'","&apos;")
