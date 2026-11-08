"""Turn Canvas files into Markdown for the AI, choosing the engine by format.

* PDF                -> pymupdf4llm (page-by-page Markdown; tables and headings
                        come out right). anydoc 0.2.x was tested and rejected
                        for PDF: it merged adjacent table rows, which put a
                        formula in the wrong cell without any error.
* Office/ebook files -> anydoc (Firecrawl, local, MIT). PPTX also gets
                        `--- slide N ---` markers so the AI can cite slides
                        and call view_page.
* Scanned or diagram-heavy PDF pages are listed in `visual_pages`, so the AI
  knows to look at them with view_page instead of trusting the text.
Nothing is uploaded anywhere: anydoc's hosted OCR is never enabled.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_BLOCK_TAGS = ["p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table",
               "section", "article", "pre", "blockquote", "hr", "header", "footer"]

TEXT_TYPES = (".txt", ".md", ".py", ".r", ".m", ".json", ".tex", ".ipynb", ".sql", ".java", ".c", ".cpp", ".js", ".ts")


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover
        return re.sub(r"<[^>]+>", " ", html)
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for a in soup.find_all("a"):
        href = a.get("href")
        if href and a.get_text(strip=True):
            a.replace_with(f"{a.get_text(strip=True)} [{href}]")
    # Break lines only at block boundaries so inline <a>/<b>/<span> keep sentences intact.
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        li.insert(0, "- ")
    for td in soup.find_all(["td", "th"]):
        td.append(" | ")
    for blk in soup.find_all(_BLOCK_TAGS):
        blk.insert_before("\n")
        blk.insert_after("\n")
    text = soup.get_text("")
    lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip().rstrip("|").strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


OFFICE_EXT = (".docx", ".doc", ".docm", ".pptx", ".ppt", ".pptm", ".pps", ".ppsx", ".pot", ".xlsx", ".xls", ".xlsm",
              ".xlsb", ".odt", ".ods", ".odp", ".rtf", ".epub", ".csv")
OFFICE_CT = ("officedocument", "msword", "ms-powerpoint", "ms-excel", "opendocument", "rtf", "epub", "text/csv")
SLIDE_EXT = (".pptx", ".ppt", ".pptm", ".pps", ".ppsx", ".pot", ".odp")

# Heuristics for "look at the picture, the text won't tell the story".
SCAN_MAX_CHARS = 40          # basically no text layer -> scanned page
DIAGRAM_IMAGE_COVER = 0.35   # images cover >35% of the page
DIAGRAM_MIN_DRAWINGS = 15    # solid vector shapes (bars, boxes, arrows) -> chart / diagram


@dataclass
class Extracted:
    text: str | None
    kind: str
    engine: str
    page_count: int | None = None
    visual_pages: list[dict] = field(default_factory=list)
    note: str | None = None


def kind_of(filename: str, content_type: str) -> str:
    name, ct = (filename or "").lower(), (content_type or "").lower()
    if name.endswith(".pdf") or "pdf" in ct:
        return "pdf"
    if name.endswith(SLIDE_EXT) or "presentation" in ct or "powerpoint" in ct:
        return "slides"
    if name.endswith(".ipynb"):
        return "notebook"
    if name.endswith(OFFICE_EXT) or any(k in ct for k in OFFICE_CT):
        return "office"
    if name.endswith((".html", ".htm")) or "text/html" in ct:
        return "html"
    if name.endswith(TEXT_TYPES) or ct.startswith("text/"):
        return "text"
    return "unsupported"


@contextlib.contextmanager
def _quiet():
    """Belt and braces: Python-level prints from parsers go to stderr (fd-level is
    handled by stdio_guard when serving)."""
    with contextlib.redirect_stdout(sys.stderr):
        yield


# ---------------------------------------------------------------- PDF
def _chart_score(page, area: float) -> int:
    """Count chart-like vector shapes. Table borders and rules are hairlines (<2pt
    thick) and are ignored. A long curve/polyline over a large area (a plotted
    series) counts as a whole chart on its own."""
    score = 0
    for d in page.get_drawings():
        r = d["rect"]
        if min(r.width, r.height) < 2:
            continue
        if len(d.get("items", ())) >= 8 and abs(r) > 0.08 * area:
            return DIAGRAM_MIN_DRAWINGS
        score += 1
    return score


def _pdf_visual_pages(doc) -> list[dict]:
    out = []
    for i, page in enumerate(doc, 1):
        chars = len(page.get_text().strip())
        area = abs(page.rect) or 1.0
        cover = 0.0
        for img in page.get_images(full=True):
            try:
                for r in page.get_image_rects(img[0]):
                    cover += abs(r & page.rect)
            except Exception:
                pass
        cover = min(cover / area, 1.0)
        drawings = _chart_score(page, area) if chars < 1500 else 0
        if drawings >= DIAGRAM_MIN_DRAWINGS:  # vector chart/diagram, maybe with a short caption
            out.append({"page": i, "reason": "diagram/figure-heavy: text may miss the key content"})
        elif chars < SCAN_MAX_CHARS and cover > 0.3:
            out.append({"page": i, "reason": "scanned/image-only page: no usable text"})
        elif chars >= SCAN_MAX_CHARS and cover > DIAGRAM_IMAGE_COVER:
            out.append({"page": i, "reason": "diagram/figure-heavy: text may miss the key content"})
    return out


def _pdf(data: bytes) -> Extracted:
    import pymupdf
    import pymupdf4llm

    with _quiet():
        doc = pymupdf.open(stream=data, filetype="pdf")
        if doc.needs_pass:
            return Extracted(None, "pdf", "pymupdf4llm", note="PDF is password-protected.")
        # The GNN layout engine is ~12x slower (60 pages: 9.3 s vs 0.7 s) for the same
        # tables/headings on lecture PDFs, so it is opt-in.
        layout = os.environ.get("CANVAS_MCP_PDF_LAYOUT") == "1"
        pymupdf4llm.use_layout(layout)
        kwargs = {"page_chunks": True, "show_progress": False}
        if layout:
            kwargs["use_ocr"] = False  # never OCR implicitly
        chunks = pymupdf4llm.to_markdown(doc, **kwargs)
        visual = _pdf_visual_pages(doc)
    parts = []
    for n, ch in enumerate(chunks, 1):
        num = (ch.get("metadata") or {}).get("page_number") or n
        parts.append(f"--- page {num} ---\n{(ch.get('text') or '').strip()}")
    return Extracted("\n\n".join(parts), "pdf", "pymupdf4llm", page_count=doc.page_count, visual_pages=visual)


# ---------------------------------------------------------------- Office (anydoc)
def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-z]+", " ", re.sub(r"[*_`#]", "", s).lower()).strip()


def _slide_titles(data: bytes) -> list[str | None]:
    from pptx import Presentation

    titles: list[str | None] = []
    for slide in Presentation(io.BytesIO(data)).slides:
        t = slide.shapes.title.text if slide.shapes.title is not None and slide.shapes.title.has_text_frame else ""
        titles.append(t.strip() or None)
    return titles


def _insert_slide_markers(md: str, titles: list[str | None]) -> tuple[str, int]:
    """anydoc has no slide boundaries; re-insert them by matching headings to slide titles in order."""
    lines, out, i, marked = md.splitlines(), [], 0, 0
    norm_titles = [(_norm(t) if t else None) for t in titles]
    for ln in lines:
        if ln.startswith("#"):
            h = _norm(ln.lstrip("#"))
            for j in range(i, len(norm_titles)):
                if norm_titles[j] and norm_titles[j] == h:
                    out.append(f"--- slide {j + 1} ---")
                    i, marked = j + 1, marked + 1
                    break
        out.append(ln)
    return "\n".join(out), marked


def _pptx_fallback(data: bytes) -> str:
    from pptx import Presentation

    out = []
    for i, slide in enumerate(Presentation(io.BytesIO(data)).slides, 1):
        parts = []
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.has_text_frame and shape.text_frame.text.strip():
                parts.append(shape.text_frame.text.strip())
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    parts.append(" | ".join(c.text.strip() for c in row.cells))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None and slide.notes_slide.notes_text_frame.text.strip():
            parts.append(f"> {slide.notes_slide.notes_text_frame.text.strip()}")
        out.append(f"--- slide {i} ---\n" + "\n".join(parts))
    return "\n\n".join(out)


def _office(filename: str, data: bytes, kind: str) -> Extracted:
    import anydoc

    name = (filename or "").lower()
    fmt = "csv" if name.endswith(".csv") else None
    try:
        with _quiet():
            md = anydoc.to_markdown_bytes(data, fmt) if fmt else anydoc.to_markdown_bytes(data)
    except anydoc.EncryptedError:
        return Extracted(None, kind, "anydoc", note="Document is password-protected.")
    except Exception as exc:
        if name.endswith(".pptx"):  # keep slides readable even if anydoc chokes
            log.warning("anydoc failed on %s (%s); using python-pptx fallback", filename, type(exc).__name__)
            return Extracted(_pptx_fallback(data), kind, "python-pptx (fallback)", page_count=None)
        raise
    count, note = None, None
    if name.endswith((".pptx", ".pptm", ".ppsx")):
        try:
            titles = _slide_titles(data)
            md, marked = _insert_slide_markers(md, titles)
            count = len(titles)
            if marked < count:
                note = f"{count - marked} of {count} slides have no title, so they carry no '--- slide N ---' marker."
        except Exception:
            pass
    if kind == "slides":
        md = _label_notes(md)
    return Extracted(md, kind, "anydoc", page_count=count, note=note)


def _label_notes(md: str) -> str:
    """anydoc renders speaker notes as a blockquote; label the first line of each."""
    out, prev = [], ""
    for ln in md.splitlines():
        if ln.startswith(">") and not prev.startswith(">"):
            ln = "> [notes] " + ln[1:].lstrip()
        out.append(ln)
        prev = ln
    return "\n".join(out)


def _ipynb(data: bytes) -> str:
    nb = json.loads(data)
    out = []
    for cell in nb.get("cells", []):
        src = "".join(cell.get("source", []))
        out.append(f"[{cell.get('cell_type')}]\n{src}")
    return "\n\n".join(out)


def extract(filename: str, content_type: str, data: bytes) -> Extracted:
    kind = kind_of(filename, content_type)
    try:
        if kind == "pdf":
            return _pdf(data)
        if kind in ("slides", "office"):
            return _office(filename, data, kind)
        if kind == "notebook":
            return Extracted(_ipynb(data), kind, "json")
        if kind == "html":
            return Extracted(html_to_text(data.decode("utf-8", "replace")), kind, "beautifulsoup")
        if kind == "text":
            return Extracted(data.decode("utf-8", "replace"), kind, "utf-8")
    except Exception as exc:
        log.warning("Text extraction failed for %s: %s", filename, type(exc).__name__)
        return Extracted(f"[text extraction failed: {type(exc).__name__}]", kind, "error")
    return Extracted(None, kind, "none")


def extract_text(filename: str, content_type: str, data: bytes) -> str | None:
    """Back-compat: text only."""
    return extract(filename, content_type, data).text
