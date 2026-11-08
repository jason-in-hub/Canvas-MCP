import io
import json
import os
import stat

import httpx
import pytest

from canvas_mcp.client import AuthRequired, CanvasClient, Session
from canvas_mcp.config import Settings
from canvas_mcp.redact import scrub_text, scrub_url
from canvas_mcp.server import Ctx, build_server
from canvas_mcp.store import SecretStore

BASE = "https://canvas.ust.hk"
TOKEN = "1234~" + "A" * 64
PASSWORD = "Hunter2-SuperSecret!"
seen: list[httpx.Request] = []


def _docx_bytes(text: str) -> bytes:
    import docx

    d = docx.Document()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


DOCX = _docx_bytes("Black-Scholes lecture: d1 = (ln(S/K) + (r + sigma^2/2)T) / (sigma sqrt(T))")


def handler(req: httpx.Request) -> httpx.Response:
    seen.append(req)
    host, path = req.url.host, req.url.path
    if host == "s3.example.com":
        return httpx.Response(200, content=DOCX, headers={"content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"})
    if host != "canvas.ust.hk":
        return httpx.Response(404)
    if req.headers.get("authorization") != f"Bearer {TOKEN}":
        return httpx.Response(302, headers={"location": f"{BASE}/login"})
    if path == "/api/v1/users/self":
        return httpx.Response(200, json={"id": 1, "name": "Test Student"})
    if path == "/api/v1/courses":
        page = req.url.params.get("page", "1")
        if page == "1":
            return httpx.Response(200, json=[{"id": 101, "name": "FINA 5210"}],
                                  headers={"link": f'<{BASE}/api/v1/courses?page=2&per_page=100>; rel="next"'})
        return httpx.Response(200, json=[{"id": 102, "name": "MAFS 5030", "term": {"name": "2026-27 Fall"}}])
    if path == "/api/v1/files/55":
        return httpx.Response(200, json={"id": 55, "display_name": "lec1.docx", "size": len(DOCX),
                                         "content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                         "url": f"{BASE}/files/55/download?download_frd=1&verifier=SECRETVERIFIER"})
    if path == "/files/55/download":
        return httpx.Response(302, headers={"location": "https://s3.example.com/obj?X-Amz-Signature=abc"})
    if path == "/api/v1/courses/101/files":
        return httpx.Response(200, json=[{"id": 55, "display_name": "lec1.docx", "folder_id": 9,
                                          "url": f"{BASE}/files/55/download?verifier=SECRETVERIFIER"}])
    if path == "/api/v1/courses/101/folders":
        return httpx.Response(200, json=[{"id": 9, "full_name": "course files/Lectures"}])
    return httpx.Response(404, json={"errors": "not found"})


@pytest.fixture
def client():
    seen.clear()
    return CanvasClient(BASE, Session(kind="token", token=TOKEN), transport=httpx.MockTransport(handler))


@pytest.fixture
def settings(tmp_path):
    return Settings(base_url=BASE, data_dir=tmp_path / "home", download_dir=tmp_path / "home" / "dl",
                    max_text_chars=10_000, max_download_bytes=10 * 1024 * 1024)


async def test_pagination_follows_link_header(client):
    courses = await client.paginate("/api/v1/courses")
    assert [c["id"] for c in courses] == [101, 102]


async def test_credentials_not_sent_off_host(client):
    data, _ = await client.stream_download(f"{BASE}/files/55/download", 10**7)
    assert data == DOCX
    s3 = [r for r in seen if r.url.host == "s3.example.com"]
    assert s3 and "authorization" not in s3[0].headers and "cookie" not in s3[0].headers


async def test_expired_session_detected():
    c = CanvasClient(BASE, Session(kind="token", token="bad"), transport=httpx.MockTransport(handler))
    with pytest.raises(AuthRequired):
        await c.whoami()


def test_client_is_get_only():
    public = [m for m in dir(CanvasClient) if not m.startswith("_")]
    assert not any(m in public for m in ("post", "put", "patch", "delete", "request"))


def test_store_encrypts_and_restricts(settings):
    store = SecretStore(settings.data_dir, use_keyring=False)
    store.save({"kind": "token", "token": TOKEN})
    raw = store.blob_path.read_bytes()
    assert TOKEN.encode() not in raw
    assert stat.S_IMODE(store.blob_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.keyfile_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(settings.data_dir.stat().st_mode) == 0o700
    assert store.load()["token"] == TOKEN
    os.chmod(store.blob_path, 0o644)
    with pytest.raises(PermissionError):
        store.load()
    store.clear()
    assert not store.blob_path.exists() and not store.keyfile_path.exists()


def test_redaction():
    assert "SECRET" not in scrub_url(f"{BASE}/files/1/download?download_frd=1&verifier=SECRET")
    assert "download_frd=1" in scrub_url(f"{BASE}/files/1/download?download_frd=1&verifier=SECRET")
    s = scrub_text(f"Authorization: Bearer {TOKEN} cookie: canvas_session=abc123; password={PASSWORD}")
    assert TOKEN not in s and "abc123" not in s and PASSWORD not in s


def _ctx(settings):
    store = SecretStore(settings.data_dir, use_keyring=False)
    c = CanvasClient(BASE, Session(kind="token", token=TOKEN), transport=httpx.MockTransport(handler))
    return Ctx(settings, store, client=c)


def _payload(result):
    # mcp 1.x returns (content, structured) or list; 2.x returns CallToolResult-like
    if isinstance(result, tuple):
        result = result[0]
    content = getattr(result, "content", result)
    return "".join(getattr(c, "text", "") for c in content)


async def test_no_tool_accepts_credentials(settings):
    srv = build_server(_ctx(settings))
    for tool in await srv.list_tools():
        props = json.dumps(getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", {})).lower()
        for bad in ("password", "passwd", "username", "token\"", "cookie", "secret"):
            assert bad not in props, f"{tool.name} exposes {bad}"


async def test_tools_end_to_end_and_no_leaks(settings):
    srv = build_server(_ctx(settings))
    out = _payload(await srv.call_tool("list_courses", {"include_past": True}))
    assert "FINA 5210" in out and "MAFS 5030" in out
    out = _payload(await srv.call_tool("list_files", {"course_id": "101"}))
    assert "Lectures" in out and "SECRETVERIFIER" not in out
    out = _payload(await srv.call_tool("read_file", {"file_id": "55"}))
    assert "Black-Scholes" in out
    for o in (out,):
        assert TOKEN not in o and "SECRETVERIFIER" not in o and "X-Amz-Signature" not in o
    out = _payload(await srv.call_tool("download_file", {"file_id": "55"}))
    assert "saved_to" in out
    saved = settings.download_dir / "misc" / "lec1.docx"
    assert saved.read_bytes() == DOCX


async def test_auth_required_message_never_asks_for_password_in_chat(settings):
    store = SecretStore(settings.data_dir, use_keyring=False)
    srv = build_server(Ctx(settings, store))  # no session stored
    out = _payload(await srv.call_tool("list_courses", {}))
    assert "auth_required" in out and "NEVER ask the user to type their password" in out
