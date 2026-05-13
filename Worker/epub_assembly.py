"""
EPUB Assembly
=============
Converts DocumentStructure into a valid EPUB 3 file using EbookLib.
Supports horizontal and vertical writing modes per chapter.
"""

from __future__ import annotations
import logging
import html as html_module
from pathlib import Path
from typing import List

from ebooklib import epub

from structure_analysis import DocumentStructure, StructuredPage

logger = logging.getLogger(__name__)

HORIZONTAL_CSS = """
body {
    writing-mode: horizontal-tb;
    font-family: "Noto Serif CJK TC", "Source Han Serif TC",
                 "Noto Serif", Georgia, serif;
    line-height: 1.8;
    margin: 1em 1.5em;
}
"""

VERTICAL_CSS = """
body {
    writing-mode: vertical-rl;
    text-orientation: mixed;
    font-family: "Noto Serif CJK TC", "Source Han Serif TC",
                 "Noto Serif", Georgia, serif;
    line-height: 2.0;
    margin: 1em 1.5em;
}
"""

COMMON_CSS = """
h1, h2, h3 {
    font-weight: bold;
    margin: 0.8em 0 0.4em;
}
h1 { font-size: 1.6em; }
h2 { font-size: 1.3em; }
h3 { font-size: 1.1em; }

p {
    margin: 0.4em 0;
    text-indent: 1em;
}

.page-number {
    font-size: 0.75em;
    color: #888;
    display: block;
    text-align: center;
    margin: 0.5em 0;
}

aside.footnote {
    font-size: 0.8em;
    color: #555;
    border-top: 1px solid #ccc;
    margin-top: 1em;
    padding-top: 0.5em;
}

ul, ol {
    margin: 0.4em 0;
    padding-left: 1.5em;
}

li {
    margin: 0.2em 0;
}

figure {
    margin: 1em auto;
    text-align: center;
}

figure img {
    max-width: 100%;
    height: auto;
}

figcaption {
    font-size: 0.85em;
    color: #666;
    margin-top: 0.3em;
}

.image-only-page {
    display: flex;
    justify-content: center;
    align-items: center;
    min-height: 80vh;
}

.empty-page {
    color: #ccc;
    font-size: 0.8em;
    text-align: center;
    margin-top: 2em;
}
"""


def _make_chapter_content(page_number: int, css_file: str, body_html: str) -> bytes:
    """
    Build a complete XHTML chapter and return it as UTF-8 bytes.

    CRITICAL: ebooklib's get_body_content() only works when content is bytes.
    Passing a str to EpubHtml(content=...) causes get_body_content() to return
    b'', which makes lxml raise 'Document is empty' during epub.write_epub().
    """
    xhtml = f"""<?xml version='1.0' encoding='utf-8'?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
  <title>Page {page_number}</title>
  <link rel="stylesheet" type="text/css" href="../{css_file}"/>
</head>
<body>
{body_html}
</body>
</html>"""
    return xhtml.encode("utf-8")


def assemble_epub(
    structure: DocumentStructure,
    output_path: Path,
    writing_mode_override: str = "auto",
) -> None:
    logger.info(f"Assembling EPUB: {output_path}")

    book = epub.EpubBook()
    book.set_identifier(f"pdf2epub-{output_path.stem}")
    book.set_title(structure.title or "Untitled")
    book.set_language("zh")
    if structure.author:
        book.add_author(structure.author)

    css_h = epub.EpubItem(
        uid="css_horizontal", file_name="style/horizontal.css",
        media_type="text/css", content=(HORIZONTAL_CSS + COMMON_CSS).encode(),
    )
    css_v = epub.EpubItem(
        uid="css_vertical", file_name="style/vertical.css",
        media_type="text/css", content=(VERTICAL_CSS + COMMON_CSS).encode(),
    )
    book.add_item(css_h)
    book.add_item(css_v)

    image_items: dict[str, epub.EpubItem] = {}
    for page in structure.pages:
        for img in page.images:
            if img.epub_id in image_items:
                continue
            ext_to_mime = {
                "png": "image/png", "jpeg": "image/jpeg", "jpg": "image/jpeg",
                "gif": "image/gif", "webp": "image/webp",
            }
            mime = ext_to_mime.get(img.ext.lower(), "image/jpeg")
            item = epub.EpubItem(
                uid=img.epub_id, file_name=f"images/{img.epub_id}.{img.ext}",
                media_type=mime, content=img.image_bytes,
            )
            book.add_item(item)
            image_items[img.epub_id] = item

    chapters: List[epub.EpubHtml] = []
    spine = ["nav"]

    for page in structure.pages:
        direction = page.direction
        if writing_mode_override != "auto":
            direction = writing_mode_override

        css_file = "style/vertical.css" if direction == "vertical" else "style/horizontal.css"
        chapter_id = f"page_{page.page_number + 1:04d}"
        body_html = _render_page_html(page, image_items)

        if not body_html.strip():
            body_html = f'<p class="empty-page">[ page {page.page_number + 1} ]</p>'

        chapter = epub.EpubHtml(
            title=f"Page {page.page_number + 1}",
            file_name=f"chapters/{chapter_id}.xhtml",
            lang="zh",
            content=_make_chapter_content(page.page_number + 1, css_file, body_html),
        )
        book.add_item(chapter)
        chapters.append(chapter)
        spine.append(chapter)

    if not chapters:
        logger.warning("No pages — inserting placeholder chapter.")
        placeholder = epub.EpubHtml(
            title="Document", file_name="chapters/placeholder.xhtml", lang="zh",
            content=_make_chapter_content(0, "style/horizontal.css",
                '<p class="empty-page">[ No text content could be extracted ]</p>'),
        )
        book.add_item(placeholder)
        chapters.append(placeholder)
        spine.append(placeholder)

    toc_items = []
    for level, title, page_num in structure.toc:
        if page_num < len(chapters):
            toc_items.append(epub.Link(
                href=chapters[page_num].file_name, title=title, uid=f"toc_{page_num}",
            ))
    book.toc = toc_items if toc_items else (
        [epub.Link(href=chapters[0].file_name, title=structure.title or "Document", uid="toc_0")]
        if chapters else []
    )

    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = spine

    epub.write_epub(str(output_path), book)
    logger.info(f"EPUB written: {output_path} ({output_path.stat().st_size / 1024:.1f} KB)")


def _render_page_html(page: StructuredPage, image_items: dict) -> str:
    parts: List[str] = []

    if page.is_image_only:
        parts.append('<div class="image-only-page">')
        for img in page.images:
            if img.epub_id in image_items:
                src = f"../images/{img.epub_id}.{img.ext}"
                parts.append(
                    f'<figure><img src="{src}" alt="{html_module.escape(img.alt_text)}"/></figure>'
                )
        parts.append("</div>")
        if len(parts) == 2:
            return ""
        return "\n".join(parts)

    img_inserted = set()

    for el in page.elements:
        text_escaped = html_module.escape(el.text)

        if el.element_type == "heading":
            tag = f"h{min(el.level, 3)}"
            parts.append(f"<{tag}>{text_escaped}</{tag}>")
        elif el.element_type == "paragraph":
            if el.href:
                inner = f'<a href="{html_module.escape(el.href)}">{text_escaped}</a>'
            else:
                inner = text_escaped
            parts.append(f"<p>{inner}</p>")
        elif el.element_type == "list-item":
            parts.append(f"<ul><li>{text_escaped}</li></ul>")
        elif el.element_type == "footnote":
            parts.append(f'<aside class="footnote"><p>{text_escaped}</p></aside>')
        elif el.element_type == "page-number":
            parts.append(f'<span class="page-number">{text_escaped}</span>')
        elif el.element_type == "caption":
            parts.append(f"<figcaption>{text_escaped}</figcaption>")
        else:
            parts.append(f"<p>{text_escaped}</p>")

    for img in page.images:
        if img.epub_id not in img_inserted and img.epub_id in image_items:
            src = f"../images/{img.epub_id}.{img.ext}"
            parts.append(f'<figure><img src="{src}" alt=""/></figure>')

    return "\n".join(parts)
