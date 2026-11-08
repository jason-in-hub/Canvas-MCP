"""Text extraction for the formats lecture material actually comes in."""

import io
import json

import httpx

from canvas_mcp.client import CanvasClient, Session
from canvas_mcp.config import Settings
from canvas_mcp.extract import extract_text, html_to_text
from canvas_mcp.server import Ctx, build_server
from canvas_mcp.store import SecretStore

PDF_CT = "application/pdf"
PPTX_CT = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def make_pdf(pages: list[str]) -> bytes:
    """Minimal valid multi-page PDF with real text objects (correct xref offsets)."""
    objs: list[bytes] = []
    n = len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    font_id = 3 + 2 * n
    for i, text in enumerate(pages):
        content_id = 4 + 2 * i
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>".encode())
        esc = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 14 Tf 72 720 Td ({esc}) Tj ET".encode()
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def make_pptx() -> bytes:
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    s1 = prs.slides.add_slide(prs.slide_layouts[1])
    s1.shapes.title.text = "Lecture 5: The Greeks"
    s1.placeholders[1].text = "Delta = dV/dS\nGamma = d2V/dS2"
    s1.notes_slide.notes_text_frame.text = "Remind class: gamma is highest ATM near expiry"
    s2 = prs.slides.add_slide(prs.slide_layouts[5])
    s2.shapes.title.text = "Greeks summary table"
    tbl = s2.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(6), Inches(1)).table
    tbl.cell(0, 0).text, tbl.cell(0, 1).text = "Greek", "Sensitivity to"
    tbl.cell(1, 0).text, tbl.cell(1, 1).text = "Vega", "Implied volatility"
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


PDF = make_pdf(["Put-call parity: C - P = S - K exp(-rT)", "Week 2: Binomial tree, risk-neutral q = (e^{r dt} - d)/(u - d)"])
PPTX = make_pptx()


def test_pdf_pages_and_text():
    text = extract_text("lec02.pdf", PDF_CT, PDF)
    assert "--- page 1 ---" in text and "--- page 2 ---" in text
    assert "Put-call parity: C - P = S - K exp(-rT)" in text
    assert "risk-neutral q = (e^{r dt} - d)/(u - d)" in text


def test_pdf_detected_by_content_type_only():
    assert "Put-call parity" in extract_text("download", PDF_CT, PDF)


def test_pptx_titles_body_table_notes():
    text = extract_text("lec05.pptx", PPTX_CT, PPTX)
    assert "--- slide 1 ---" in text and "--- slide 2 ---" in text
    assert "Lecture 5: The Greeks" in text
    assert "Gamma = d2V/dS2" in text
    assert "[notes] Remind class: gamma is highest ATM near expiry" in text
    assert "Vega | Implied volatility" in text
    # slide order preserved
    assert text.index("Lecture 5") < text.index("Greeks summary table")


def test_ipynb_html_and_plain():
    nb = json.dumps({"cells": [{"cell_type": "markdown", "source": ["# Monte Carlo\n"]},
                               {"cell_type": "code", "source": ["import numpy as np\n", "np.random.seed(0)"]}]}).encode()
    t = extract_text("hw1.ipynb", "application/x-ipynb+json", nb)
    assert "[markdown]\n# Monte Carlo" in t and "np.random.seed(0)" in t
    html = "<html><style>p{}</style><script>alert(1)</script><p>See <a href='https://x.y/z'>notes</a></p></html>"
    assert html_to_text(html) == "See notes [https://x.y/z]"
    assert extract_text("a.csv", "text/csv", b"t,price\n1,100") == "t,price\n1,100"


def test_corrupt_and_unsupported():
    assert extract_text("broken.pdf", PDF_CT, b"%PDF-1.4 garbage").startswith("[text extraction failed")
    assert extract_text("video.mp4", "video/mp4", b"\x00\x01") is None


# ---- through the MCP tool, incl. offset paging ---------------------------
FILES = {"77": ("lec02.pdf", PDF_CT, PDF), "88": ("lec05.pptx", PPTX_CT, PPTX)}


def handler(req: httpx.Request) -> httpx.Response:
    path = req.url.path
    for fid, (name, ct, data) in FILES.items():
        if path == f"/api/v1/files/{fid}":
            return httpx.Response(200, json={"id": int(fid), "display_name": name, "content-type": ct, "size": len(data),
                                             "url": f"https://canvas.ust.hk/files/{fid}/download?verifier=V"})
        if path == f"/files/{fid}/download":
            return httpx.Response(200, content=data, headers={"content-type": ct})
    return httpx.Response(404)


def _srv(tmp_path):
    s = Settings(base_url="https://canvas.ust.hk", data_dir=tmp_path, download_dir=tmp_path / "dl",
                 max_text_chars=10_000, max_download_bytes=10 * 1024 * 1024)
    c = CanvasClient(s.base_url, Session(kind="token", token="t"), transport=httpx.MockTransport(handler))
    return build_server(Ctx(s, SecretStore(tmp_path, use_keyring=False), client=c))


def _json(result):
    if isinstance(result, tuple):
        result = result[0]
    content = getattr(result, "content", result)
    return json.loads("".join(getattr(c, "text", "") for c in content))


async def test_read_file_tool_pdf_and_pptx(tmp_path):
    srv = _srv(tmp_path)
    pdf = _json(await srv.call_tool("read_file", {"file_id": "77"}))
    assert "Put-call parity" in pdf["text"] and pdf["has_more"] is False
    pptx = _json(await srv.call_tool("read_file", {"file_id": "88"}))
    assert "[notes] Remind class" in pptx["text"]


async def test_read_file_paging_reassembles_exactly(tmp_path):
    srv = _srv(tmp_path)
    full = _json(await srv.call_tool("read_file", {"file_id": "88"}))
    total, parts, offset = full["total_chars"], [], 0
    while True:
        r = _json(await srv.call_tool("read_file", {"file_id": "88", "offset": offset, "max_chars": 40}))
        parts.append(r["text"])
        offset += 40
        if not r["has_more"]:
            break
    assert "".join(parts) == full["text"] and len("".join(parts)) == total


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
