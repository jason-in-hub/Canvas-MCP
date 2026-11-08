"""Turn Canvas content (HTML, PDF, PPTX, DOCX, text) into plain text for the AI."""

from __future__ import annotations

import io
import logging

log = logging.getLogger(__name__)

TEXT_TYPES = (".txt", ".md", ".csv", ".py", ".r", ".m", ".json", ".tex", ".ipynb", ".sql", ".java", ".c", ".cpp", ".js", ".ts")


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover
        import re

        return re.sub(r"<[^>]+>", " ", html)
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for a in soup.find_all("a"):
        href = a.get("href")
        if href and a.get_text(strip=True):
            a.replace_with(f"{a.get_text(strip=True)} [{href}]")
    text = soup.get_text("\n")
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    out = []
    for i, page in enumerate(reader.pages, 1):
        t = page.extract_text() or ""
        out.append(f"--- page {i} ---\n{t.strip()}")
    return "\n\n".join(out)


def _pptx(data: bytes) -> str:
    from pptx import Presentation

    prs = Presentation(io.BytesIO(data))
    out = []
    for i, slide in enumerate(prs.slides, 1):
        parts = []
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                txt = shape.text_frame.text.strip()
                if txt:
                    parts.append(txt)
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    parts.append(" | ".join(c.text.strip() for c in row.cells))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                parts.append(f"[notes] {notes}")
        out.append(f"--- slide {i} ---\n" + "\n".join(parts))
    return "\n\n".join(out)


def _docx(data: bytes) -> str:
    import docx

    d = docx.Document(io.BytesIO(data))
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for table in d.tables:
        for row in table.rows:
            parts.append(" | ".join(c.text.strip() for c in row.cells))
    return "\n".join(parts)


def _ipynb(data: bytes) -> str:
    import json

    nb = json.loads(data)
    out = []
    for cell in nb.get("cells", []):
        src = "".join(cell.get("source", []))
        out.append(f"[{cell.get('cell_type')}]\n{src}")
    return "\n\n".join(out)


def extract_text(filename: str, content_type: str, data: bytes) -> str | None:
    """Return extracted text, or None if the format isn't supported."""
    name = (filename or "").lower()
    ct = (content_type or "").lower()
    try:
        if name.endswith(".pdf") or "pdf" in ct:
            return _pdf(data)
        if name.endswith(".pptx") or "presentationml" in ct:
            return _pptx(data)
        if name.endswith(".docx") or "wordprocessingml" in ct:
            return _docx(data)
        if name.endswith(".ipynb"):
            return _ipynb(data)
        if name.endswith((".html", ".htm")) or "text/html" in ct:
            return html_to_text(data.decode("utf-8", "replace"))
        if name.endswith(TEXT_TYPES) or ct.startswith("text/"):
            return data.decode("utf-8", "replace")
    except Exception as exc:
        log.warning("Text extraction failed for %s: %s", filename, type(exc).__name__)
        return f"[text extraction failed: {type(exc).__name__}]"
    return None
