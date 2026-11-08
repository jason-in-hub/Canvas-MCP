"""Render one page or slide as an image so a multimodal AI can see it.

* PDF: PyMuPDF renders the page directly.
* Slides and other office files: LibreOffice (`soffice`, if installed)
  converts the file to PDF once, in a sandboxed temp profile, then the
  PDF page is rendered.
* No LibreOffice: for PPTX, the pictures embedded in that slide are
  returned instead.

Images are sized so the long edge is at most MAX_EDGE px (≈1.5k tokens each).
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

MAX_EDGE = 1400
MAX_BYTES = 1_500_000
SOFFICE_TIMEOUT_S = 120


class RenderError(RuntimeError):
    pass


@dataclass
class Rendered:
    images: list[tuple[bytes, str]]  # (data, "png" | "jpeg")
    page_count: int
    note: str | None = None


def soffice_path() -> str | None:
    return os.environ.get("CANVAS_MCP_SOFFICE") or shutil.which("soffice") or shutil.which("libreoffice")


def office_to_pdf(data: bytes, filename: str) -> bytes:
    exe = soffice_path()
    if not exe:
        raise RenderError("LibreOffice not installed")
    ext = os.path.splitext(filename or "")[1].lower() or ".pptx"
    with tempfile.TemporaryDirectory(prefix="canvas-mcp-") as tmp:
        src = os.path.join(tmp, "in" + ext)
        with open(src, "wb") as fh:
            fh.write(data)
        profile = "file://" + os.path.join(tmp, "lo-profile").replace(os.sep, "/")
        cmd = [exe, f"-env:UserInstallation={profile}", "--headless", "--norestore", "--nolockcheck",
               "--convert-to", "pdf", "--outdir", tmp, src]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=SOFFICE_TIMEOUT_S, check=False)
        except subprocess.TimeoutExpired as exc:
            raise RenderError("LibreOffice conversion timed out") from exc
        out = os.path.join(tmp, "in.pdf")
        if not os.path.exists(out):
            raise RenderError("LibreOffice could not convert this file")
        with open(out, "rb") as fh:
            return fh.read()


def _encode(pix) -> tuple[bytes, str]:
    png = pix.tobytes("png")
    if len(png) <= MAX_BYTES:
        return png, "png"
    return pix.tobytes("jpg", jpg_quality=80), "jpeg"


def render_pdf_page(pdf: bytes, page: int) -> Rendered:
    import pymupdf

    doc = pymupdf.open(stream=pdf, filetype="pdf")
    if doc.needs_pass:
        raise RenderError("PDF is password-protected")
    n = doc.page_count
    if not 1 <= page <= n:
        raise RenderError(f"page must be between 1 and {n}")
    p = doc[page - 1]
    zoom = min(MAX_EDGE / max(p.rect.width, p.rect.height), 3.0)
    pix = p.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    return Rendered([_encode(pix)], n)


def pptx_slide_pictures(data: bytes, slide: int, limit: int = 4) -> Rendered:
    """Fallback without LibreOffice: return the pictures placed on that slide."""
    import pymupdf
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    slides = list(Presentation(io.BytesIO(data)).slides)
    if not 1 <= slide <= len(slides):
        raise RenderError(f"slide must be between 1 and {len(slides)}")
    imgs: list[tuple[bytes, str]] = []

    def walk(shapes):
        for sh in shapes:
            if sh.shape_type == MSO_SHAPE_TYPE.GROUP:
                yield from walk(sh.shapes)
            elif sh.shape_type == MSO_SHAPE_TYPE.PICTURE:
                yield sh

    for pic in walk(slides[slide - 1].shapes):
        if len(imgs) >= limit:
            break
        try:  # normalise any format (emf/tiff/...) to a bounded png/jpeg
            pix = pymupdf.Pixmap(pic.image.blob)
            if pix.alpha or pix.n > 3:
                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
            if max(pix.width, pix.height) > MAX_EDGE:
                f = MAX_EDGE / max(pix.width, pix.height)
                pix = pymupdf.Pixmap(pix, int(pix.width * f), int(pix.height * f))
            imgs.append(_encode(pix))
        except Exception:
            continue
    note = ("LibreOffice is not installed, so the whole slide could not be rendered. "
            f"Returned {len(imgs)} picture(s) embedded on slide {slide}. Install LibreOffice for full slide images.")
    return Rendered(imgs, len(slides), note)
