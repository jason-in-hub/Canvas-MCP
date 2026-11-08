"""Extraction (pymupdf4llm for PDF, anydoc for office) and page rendering (view_page)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import httpx
import pytest

from canvas_mcp import extract as ex_mod
from canvas_mcp import render as render_mod
from canvas_mcp.client import CanvasClient, Session
from canvas_mcp.config import Settings
from canvas_mcp.extract import extract, extract_text, html_to_text
from canvas_mcp.server import Ctx, build_server
from canvas_mcp.store import SecretStore

PDF_CT = "application/pdf"
PPTX_CT = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
FIXTURES = Path(__file__).parent / "fixtures"
# Chromium-printed lecture PDF: headings, bullets, a 3x4 Greeks table, 2 pages.
LEC04 = (FIXTURES / "lec04.pdf").read_bytes()


def _png(w=400, h=300, color=(200, 30, 30)) -> bytes:
    import pymupdf

    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, w, h), False)
    pix.set_rect(pix.irect, color)
    return pix.tobytes("png")


def make_scanned_mixed_pdf() -> bytes:
    """Page 1 text, page 2 a 'scan' (image only), page 3 a vector chart with a caption."""
    import pymupdf

    doc = pymupdf.open()
    p1 = doc.new_page()
    p1.insert_text((72, 72), "Week 6: Value at Risk. VaR_alpha = -inf{x : P(L <= x) >= alpha}", fontsize=11)
    p2 = doc.new_page()
    p2.insert_image(p2.rect, stream=_png(1200, 1600, (240, 240, 235)))
    p3 = doc.new_page()
    p3.insert_text((72, 60), "Figure 6.1: P&L distribution", fontsize=11)
    for i in range(60):  # bar chart
        p3.draw_rect(pymupdf.Rect(72 + i * 7, 500 - (i % 17) * 15, 77 + i * 7, 500), color=(0, 0, 1), fill=(0.2, 0.4, 0.8))
    return doc.tobytes()


def make_pptx(untitled_middle: bool = False, with_picture: bool = False) -> bytes:
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    s1 = prs.slides.add_slide(prs.slide_layouts[1])
    s1.shapes.title.text = "Lecture 5: The Greeks"
    s1.placeholders[1].text = "Delta = dV/dS\nGamma = d2V/dS2"
    s1.notes_slide.notes_text_frame.text = "Remind class: gamma is highest ATM near expiry"
    if untitled_middle:
        blank = prs.slides.add_slide(prs.slide_layouts[6])
        tb = blank.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(1))
        tb.text_frame.text = "Untitled interlude slide"
    s2 = prs.slides.add_slide(prs.slide_layouts[5])
    s2.shapes.title.text = "Greeks summary table"
    tbl = s2.shapes.add_table(3, 2, Inches(1), Inches(2), Inches(6), Inches(1.5)).table
    for r, (a, b) in enumerate([("Greek", "Sensitivity to"), ("Vega", "Implied volatility"), ("Theta", "Time decay")]):
        tbl.cell(r, 0).text, tbl.cell(r, 1).text = a, b
    if with_picture:
        s2.shapes.add_picture(io.BytesIO(_png()), Inches(1), Inches(4), Inches(2))
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------------ PDF
def test_pdf_lecture_table_rows_not_merged():
    """The anydoc-0.2.4 regression we chose pymupdf4llm over: Gamma's formula must stay on Gamma's row."""
    r = extract("lec04.pdf", PDF_CT, LEC04)
    assert r.engine == "pymupdf4llm" and r.kind == "pdf" and r.page_count == 2
    rows = [ln.replace("**", "").replace(" ", "") for ln in r.text.splitlines() if ln.startswith("|")]
    assert "|Delta|N(d1)|Hedgeratio|" in rows
    assert "|Gamma|φ(d1)/(Sσ√T)|Convexity|" in rows
    assert "|Vega|Sφ(d1)√T|Volsensitivity|" in rows


def test_pdf_page_markers_and_headings():
    t = extract("lec04.pdf", PDF_CT, LEC04).text
    assert t.index("--- page 1 ---") < t.index("Black-Scholes") < t.index("--- page 2 ---") < t.index("Explain why Γ peaks ATM")
    assert "# " in t and "## " in t  # headings preserved as Markdown
    assert extract("download", PDF_CT, LEC04).engine == "pymupdf4llm"  # detected by content-type alone


def test_pdf_flags_scanned_and_diagram_pages_only():
    r = extract("w6.pdf", PDF_CT, make_scanned_mixed_pdf())
    flagged = {v["page"]: v["reason"] for v in r.visual_pages}
    assert set(flagged) == {2, 3}, flagged
    assert flagged[2].startswith("scanned") and flagged[3].startswith("diagram")
    assert "Value at Risk" in r.text  # page 1 text still extracted
    assert extract("lec04.pdf", PDF_CT, LEC04).visual_pages == []


def test_pdf_corrupt_and_unsupported():
    assert extract_text("broken.pdf", PDF_CT, b"%PDF-1.4 garbage").startswith("[text extraction failed")
    r = extract("video.mp4", "video/mp4", b"\x00\x01")
    assert r.text is None and r.kind == "unsupported"


# ------------------------------------------------------------------ Office via anydoc
def test_pptx_anydoc_markdown_with_slide_markers_table_and_notes():
    r = extract("lec05.pptx", PPTX_CT, make_pptx())
    assert r.engine == "anydoc" and r.kind == "slides" and r.page_count == 2
    t = r.text
    assert t.index("--- slide 1 ---") < t.index("Lecture 5: The Greeks") < t.index("--- slide 2 ---") < t.index("Greeks summary table")
    assert "- Gamma = d2V/dS2" in t
    assert "> [notes] Remind class: gamma is highest ATM near expiry" in t
    assert "| Theta | Time decay |" in t and "| --- | --- |" in t


def test_pptx_untitled_slide_keeps_numbering_and_says_so():
    r = extract("lec05.pptx", PPTX_CT, make_pptx(untitled_middle=True))
    assert r.page_count == 3
    assert "--- slide 1 ---" in r.text and "--- slide 3 ---" in r.text and "--- slide 2 ---" not in r.text
    assert "1 of 3 slides have no title" in r.note


def test_pptx_falls_back_to_python_pptx_if_anydoc_fails(monkeypatch):
    import anydoc

    def boom(*a, **k):
        raise anydoc.MalformedError("synthetic")

    monkeypatch.setattr(anydoc, "to_markdown_bytes", boom)
    r = extract("lec05.pptx", PPTX_CT, make_pptx())
    assert r.engine == "python-pptx (fallback)"
    assert "--- slide 2 ---" in r.text and "Theta | Time decay" in r.text


def test_docx_and_csv_via_anydoc():
    import docx

    d = docx.Document()
    d.add_heading("Problem Set 3", level=1)
    d.add_paragraph("Price a 6-month European put.")
    buf = io.BytesIO()
    d.save(buf)
    r = extract("ps3.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", buf.getvalue())
    assert r.engine == "anydoc" and "# Problem Set 3" in r.text and "European put" in r.text
    csv = extract("prices.csv", "text/csv", b"t,price\n1,100")
    assert "| t | price |" in csv.text and "| 1 | 100 |" in csv.text


def test_ipynb_html_and_plain():
    nb = json.dumps({"cells": [{"cell_type": "markdown", "source": ["# Monte Carlo\n"]},
                               {"cell_type": "code", "source": ["import numpy as np\n", "np.random.seed(0)"]}]}).encode()
    t = extract_text("hw1.ipynb", "application/x-ipynb+json", nb)
    assert "[markdown]\n# Monte Carlo" in t and "np.random.seed(0)" in t
    html = "<html><style>p{}</style><script>alert(1)</script><p>See <a href='https://x.y/z'>notes</a></p></html>"
    assert html_to_text(html) == "See notes [https://x.y/z]"
    assert extract_text("a.txt", "text/plain", b"plain") == "plain"


def test_html_canvas_page_structure():
    html = ("<h2>Week 3 <b>Readings</b></h2><p>Read <a href='https://x/ch3.pdf'>Hull ch.3</a> before <em>Tuesday</em>.<br>Bring a calculator.</p>"
            "<ul><li>Forward pricing</li><li>Hedging with <strong>futures</strong></li></ul>"
            "<table><tr><th>Due</th><th>Item</th></tr><tr><td>Fri</td><td>HW1</td></tr></table>")
    assert html_to_text(html).splitlines() == [
        "Week 3 Readings",
        "Read Hull ch.3 [https://x/ch3.pdf] before Tuesday.",
        "Bring a calculator.",
        "- Forward pricing",
        "- Hedging with futures",
        "Due | Item",
        "Fri | HW1",
    ]


# ------------------------------------------------------------------ through the MCP tools
PPTX_PIC = make_pptx(with_picture=True)
FILES = {
    "77": ("lec04.pdf", PDF_CT, LEC04),
    "78": ("w6.pdf", PDF_CT, make_scanned_mixed_pdf()),
    "88": ("lec05.pptx", PPTX_CT, PPTX_PIC),
    "99": ("readme.txt", "text/plain", b"hello"),
}
downloads: list[str] = []


def handler(req: httpx.Request) -> httpx.Response:
    path = req.url.path
    for fid, (name, ct, data) in FILES.items():
        if path == f"/api/v1/files/{fid}":
            return httpx.Response(200, json={"id": int(fid), "display_name": name, "content-type": ct, "size": len(data),
                                             "updated_at": "2026-10-01T00:00:00Z",
                                             "url": f"https://canvas.ust.hk/files/{fid}/download?verifier=V"})
        if path == f"/files/{fid}/download":
            downloads.append(fid)
            return httpx.Response(200, content=data, headers={"content-type": ct})
    return httpx.Response(404)


def _srv(tmp_path):
    downloads.clear()
    s = Settings(base_url="https://canvas.ust.hk", data_dir=tmp_path, download_dir=tmp_path / "dl",
                 max_text_chars=10_000, max_download_bytes=10 * 1024 * 1024)
    c = CanvasClient(s.base_url, Session(kind="token", token="t"), transport=httpx.MockTransport(handler))
    return build_server(Ctx(s, SecretStore(tmp_path, use_keyring=False), client=c))


def _content(result):
    if isinstance(result, tuple):
        result = result[0]
    return list(getattr(result, "content", result))


def _json(result):
    return json.loads("".join(getattr(c, "text", "") for c in _content(result)))


def _images(result):
    import base64

    return [(base64.b64decode(c.data), c.mimeType if hasattr(c, "mimeType") else c.mime_type)
            for c in _content(result) if getattr(c, "type", "") == "image"]


def _dims(png: bytes) -> tuple[int, int]:
    import pymupdf

    pix = pymupdf.Pixmap(png)
    return pix.width, pix.height


async def test_read_file_reports_visual_pages_and_hint(tmp_path):
    srv = _srv(tmp_path)
    r = _json(await srv.call_tool("read_file", {"file_id": "78"}))
    assert [v["page"] for v in r["visual_pages"]] == [2, 3] and "view_page" in r["hint"]
    r = _json(await srv.call_tool("read_file", {"file_id": "88"}))
    assert r["engine"] == "anydoc" and r["page_count"] == 2 and "--- slide 2 ---" in r["text"]


async def test_read_file_paging_reassembles_exactly_and_downloads_once(tmp_path):
    srv = _srv(tmp_path)
    full = _json(await srv.call_tool("read_file", {"file_id": "77"}))
    parts, offset = [], 0
    while True:
        r = _json(await srv.call_tool("read_file", {"file_id": "77", "offset": offset, "max_chars": 200}))
        parts.append(r["text"])
        offset += 200
        if not r["has_more"]:
            break
    assert "".join(parts) == full["text"] and len(full["text"]) == full["total_chars"]
    assert downloads.count("77") == 1, "bytes should be cached across paging calls"


async def test_view_page_pdf_returns_bounded_image(tmp_path):
    srv = _srv(tmp_path)
    res = await srv.call_tool("view_page", {"file_id": "78", "page": 3})
    imgs = _images(res)
    assert len(imgs) == 1 and imgs[0][1] == "image/png" and imgs[0][0][:8] == b"\x89PNG\r\n\x1a\n"
    w, h = _dims(imgs[0][0])
    assert max(w, h) <= render_mod.MAX_EDGE and max(w, h) >= 1000
    text = "".join(getattr(c, "text", "") for c in _content(res))
    assert "w6.pdf: page 3 of 3" in text


async def test_view_page_out_of_range_and_unsupported(tmp_path):
    srv = _srv(tmp_path)
    r = _json(await srv.call_tool("view_page", {"file_id": "77", "page": 9}))
    assert r["error"] == "cannot_render" and "between 1 and 2" in r["detail"]
    r = _json(await srv.call_tool("view_page", {"file_id": "99", "page": 1}))
    assert r["error"] == "cannot_render"


async def test_view_page_pptx_without_libreoffice_returns_slide_pictures(tmp_path, monkeypatch):
    import canvas_mcp.server as server_mod

    monkeypatch.setattr(server_mod, "soffice_path", lambda: None)
    srv = _srv(tmp_path)
    res = await srv.call_tool("view_page", {"file_id": "88", "page": 2})
    imgs = _images(res)
    assert len(imgs) == 1 and _dims(imgs[0][0]) == (400, 300)
    assert "LibreOffice is not installed" in "".join(getattr(c, "text", "") for c in _content(res))
    r = _json(await srv.call_tool("view_page", {"file_id": "88", "page": 1}))
    assert r["error"] == "no_image"  # slide 1 has no pictures


@pytest.mark.skipif(render_mod.soffice_path() is None, reason="LibreOffice not installed")
async def test_view_page_pptx_with_libreoffice_renders_whole_slide(tmp_path):
    srv = _srv(tmp_path)
    res = await srv.call_tool("view_page", {"file_id": "88", "page": 2})
    imgs = _images(res)
    assert len(imgs) == 1
    w, h = _dims(imgs[0][0])
    assert w > h and max(w, h) <= render_mod.MAX_EDGE  # landscape slide
    res2 = await srv.call_tool("view_page", {"file_id": "88", "page": 1})
    assert _images(res2) and downloads.count("88") == 1


def test_line_chart_page_flagged_but_bordered_table_not():
    import pymupdf

    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 60), "Figure 2: simulated GBM path, S0=100, sigma=20%", fontsize=11)
    pts = [pymupdf.Point(72 + i * 4.5, 400 - 80 * __import__("math").sin(i / 9) - i * 0.6) for i in range(100)]
    p.draw_polyline(pts, color=(0.8, 0, 0), width=1.2)
    p.draw_line((72, 420), (520, 420)); p.draw_line((72, 420), (72, 200))  # hairline axes
    r = extract("fig.pdf", PDF_CT, doc.tobytes())
    assert [v["page"] for v in r.visual_pages] == [1]


def test_pdf_60_pages_is_fast_enough():
    import time

    import pymupdf

    src, big = pymupdf.open(stream=LEC04, filetype="pdf"), pymupdf.open()
    for _ in range(30):
        big.insert_pdf(src)
    t = time.perf_counter()
    r = extract("big.pdf", PDF_CT, big.tobytes())
    assert r.page_count == 60 and "--- page 60 ---" in r.text
    assert time.perf_counter() - t < 5, "default (non-GNN) PDF path should handle a 60-page deck in a few seconds"


def test_pdf_layout_engine_opt_in_still_correct(monkeypatch):
    monkeypatch.setenv("CANVAS_MCP_PDF_LAYOUT", "1")
    rows = [ln.replace("**", "").replace(" ", "") for ln in extract("lec04.pdf", PDF_CT, LEC04).text.splitlines() if ln.startswith("|")]
    assert "|Gamma|φ(d1)/(Sσ√T)|Convexity|" in rows
