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
        SimpleDocTemplate, Paragraph, Spacer, PageBreak, Image as RLImage,
        KeepTogether,
    )
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont

    # ── Font registration: try CJK CID fonts, fall back to Helvetica ─────────
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

    story: list = []

    # ── Title page ────────────────────────────────────────────────────────────
    if structure.title:
        story.append(Spacer(1, 40 * mm))
        story.append(Paragraph(_esc(structure.title), s_title))
        if structure.author:
            story.append(Spacer(1, 5 * mm))
            story.append(Paragraph(_esc(structure.author), s_auth))
        story.append(PageBreak())

    # ── Content pages ─────────────────────────────────────────────────────────
    has_content = False
    for page in structure.pages:
        page_items: list = []

        # Embedded images
        for img in page.images:
            if img.image_bytes:
                try:
                    page_items.append(
                        RLImage(io.BytesIO(img.image_bytes),
                                width=150 * mm, height=200 * mm, kind="proportional")
                    )
                    page_items.append(Spacer(1, 3 * mm))
                    has_content = True
                except Exception as e:
                    logger.warning(f"Could not embed image in clean PDF: {e}")

        # Text elements
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
                    page_items.append(Paragraph(f"• {safe}", s_li))
                elif el.element_type == "footnote":
                    page_items.append(Paragraph(safe, s_fn))
                elif el.element_type == "page-number":
                    page_items.append(Paragraph(safe, s_pn))
                elif el.element_type == "caption":
                    page_items.append(Paragraph(safe, s_cap))
                else:
                    page_items.append(Paragraph(safe, s_body))
                has_content = True
            except Exception as e:
                logger.warning(f"Skipping element due to error: {e} — text: {t[:50]!r}")

        if page_items:
            story.extend(page_items)
            story.append(PageBreak())

    # ── Ensure story is never empty (ReportLab raises "Document is empty") ────
    if not story:
        story.append(Paragraph("[ No text content could be extracted ]", s_body))
    elif not has_content:
        # Story may only contain title/spacer/pagebreak — add a note
        story.append(Paragraph("[ No body text was extracted from this PDF ]", s_body))

    # ── Build ─────────────────────────────────────────────────────────────────
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=25 * mm, rightMargin=25 * mm,
        topMargin=20 * mm, bottomMargin=20 * mm,
        title=structure.title or "Untitled",
        author=structure.author or "",
    )

    try:
        doc.build(story)
    except Exception as e:
        # Last-resort fallback: minimal single-paragraph document
        logger.error(f"ReportLab build failed ({e}), writing minimal fallback PDF")
        _write_minimal_pdf(output_path, structure.title or "Untitled",
                           f"PDF assembly error: {e}")

    logger.info(f"Clean PDF written: {output_path} ({output_path.stat().st_size/1024:.1f} KB)")


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
    # Fall back to built-in Helvetica (no CJK glyphs but won't crash)
    return "Helvetica"


def _esc(text: str) -> str:
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;"))


def _write_minimal_pdf(output_path: Path, title: str, message: str) -> None:
    """Write a bare-minimum valid PDF using only PyMuPDF (no ReportLab)."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), title[:80],   fontsize=16)
    page.insert_text((72, 140), message[:200], fontsize=10)
    doc.save(str(output_path))
    doc.close()
