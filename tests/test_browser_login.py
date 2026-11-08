"""Drive the real Playwright login code against a fake Canvas + fake IdP.

Two different hostnames on one local server:
    127.0.0.1  -> plays canvas.ust.hk
    localhost  -> plays cas.ust.hk / login.microsoftonline.com
so cookie scoping (keep Canvas cookies, drop IdP cookies) is exercised for real.

Headful tests need a display: run with `xvfb-run -a pytest`. They skip otherwise.
"""

from __future__ import annotations

import json
import os
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("playwright.async_api")

from canvas_mcp import auth  # noqa: E402
from canvas_mcp.client import CanvasClient, Session  # noqa: E402
from canvas_mcp.config import Settings  # noqa: E402

USER = "student@connect.ust.hk"
PASSWORD = "Corr3ct-Horse-Battery!"
SESSION = "sess-abc123"
CSRF = "csrf%2Btoken%3D%3D"  # Canvas URL-encodes this cookie
HKUST_TOKEN_DENIED = "Your Canvas administrators have chosen to limit your ability to generate your own access token."


class FakeWorld:
    def __init__(self):
        self.autosubmit = True   # simulate the human filling the IdP form (headful mode)
        self.mfa = False         # show number-match + "Stay signed in?" screens
        self.events: list[tuple] = []
        self.port = 0

    @property
    def canvas(self):
        return f"http://127.0.0.1:{self.port}"

    @property
    def idp(self):
        return f"http://localhost:{self.port}"


def make_handler(w: FakeWorld):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep test output quiet
            pass

        def _cookies(self):
            c = SimpleCookie(self.headers.get("Cookie", ""))
            return {k: v.value for k, v in c.items()}

        def _send(self, code, body=b"", ctype="text/html", headers=()):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, loc, cookies=()):
            self._send(302, headers=[("Location", loc), *[("Set-Cookie", c) for c in cookies]])

        def do_GET(self):
            host = self.headers["Host"].split(":")[0]
            u = urlparse(self.path)
            w.events.append(("GET", host, u.path))
            if host == "127.0.0.1":  # ---- Canvas
                if u.path == "/login":
                    return self._redirect(f"{w.idp}/cas/login?service={w.canvas}/login/cas")
                if u.path == "/login/cas":
                    if parse_qs(u.query).get("ticket") != ["ST-1"]:
                        return self._send(403, b"bad ticket")
                    return self._redirect("/?login_success=1", [
                        f"canvas_session={SESSION}; Path=/; HttpOnly",
                        f"_csrf_token={CSRF}; Path=/",
                    ])
                if u.path == "/":
                    return self._send(200, b"<h1>Dashboard</h1>")
                if u.path == "/api/v1/users/self":
                    if self._cookies().get("canvas_session") == SESSION:
                        return self._send(200, json.dumps({"id": 42, "name": "Fake Student"}).encode(), "application/json")
                    return self._send(401, b'{"errors":[{"message":"user authorization required"}]}', "application/json")
                return self._send(404)
            # ---- IdP (localhost)
            if u.path == "/cas/login":
                return self._redirect(f"{w.idp}/ms/login")
            if u.path == "/ms/login":
                auto = """<script>setTimeout(()=>{document.querySelector('[name=loginfmt]').value='%s';
                    document.querySelector('[name=passwd]').value='%s';document.forms[0].submit();},800)</script>""" % (USER, PASSWORD)
                page = f"""<html><body><form method=post action="/ms/submit">
                    <div id=s1><input name=loginfmt><input type=submit id=next value=Next
                        onclick="event.preventDefault();s1.style.display='none';s2.style.display='block'"></div>
                    <div id=s2 style="display:none"><input name=passwd type=password><input type=submit value="Sign in"></div>
                    </form>{auto if w.autosubmit else ''}</body></html>"""
                return self._send(200, page.encode())
            if u.path == "/ms/mfa":
                page = f"""<html><body><div id=mfa>Approve sign in: <div id=idRichContext_DisplaySign>42</div></div>
                    <div id=kmsi style="display:none">Stay signed in? <input type=checkbox id=KmsiCheckboxField>
                    <button id=idBtn_Back onclick="location='{w.canvas}/login/cas?ticket=ST-1'">No</button></div>
                    <script>setTimeout(()=>{{mfa.style.display='none';kmsi.style.display='block'}},2500)</script></body></html>"""
                return self._send(200, page.encode())
            return self._send(404)

        def do_POST(self):
            host = self.headers["Host"].split(":")[0]
            u = urlparse(self.path)
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            form = {k: v[0] for k, v in parse_qs(body).items()}
            w.events.append(("POST", host, u.path, form, {k.lower(): v for k, v in self.headers.items()}))
            if host == "localhost" and u.path == "/ms/submit":
                if form.get("loginfmt") != USER or form.get("passwd") != PASSWORD:
                    return self._send(200, b"<div id=passwordError>Your account or password is incorrect.</div>")
                idp_cookie = "ESTSAUTH=idp-secret-should-not-be-kept; Path=/; HttpOnly"
                nxt = f"{w.idp}/ms/mfa" if w.mfa else f"{w.canvas}/login/cas?ticket=ST-1"
                return self._redirect(nxt, [idp_cookie])
            if host == "127.0.0.1" and u.path == "/api/v1/users/self/tokens":
                return self._send(401, json.dumps({"errors": [{"message": HKUST_TOKEN_DENIED}]}).encode(), "application/json")
            return self._send(404)

    return H


@pytest.fixture
def world(monkeypatch):
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    w = FakeWorld()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(w))
    w.port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield w
    srv.shutdown()


@pytest.fixture
def settings(world, tmp_path):
    return Settings(base_url=world.canvas, data_dir=tmp_path, download_dir=tmp_path / "dl",
                    max_text_chars=10_000, max_download_bytes=10**7)


needs_display = pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="headful browser needs a display (use xvfb-run)")


def _no_secret_anywhere(payload, captured: str):
    blob = json.dumps(payload)
    assert PASSWORD not in blob and USER not in blob
    assert PASSWORD not in captured


# ------------------------------------------------------------- headful (default mode)
@needs_display
async def test_headful_login_captures_only_canvas_cookies_and_they_work(world, settings, capsys):
    payload = await auth.browser_login(settings, headless=False)
    assert payload["kind"] == "cookies"
    names = {c["name"]: c for c in payload["cookies"]}
    assert names["canvas_session"]["value"] == SESSION and "_csrf_token" in names
    assert "ESTSAUTH" not in names, "IdP (Microsoft) cookies must be dropped"
    assert all(c["domain"].lstrip(".") == "127.0.0.1" for c in payload["cookies"])
    # the full chain was walked: Canvas -> CAS -> MS -> Canvas
    paths = [e[2] for e in world.events]
    assert paths.index("/login") < paths.index("/cas/login") < paths.index("/ms/login") < paths.index("/login/cas")
    # captured cookies authenticate the read-only API client
    c = CanvasClient(settings.base_url, Session.from_payload(payload))
    try:
        assert (await c.whoami())["name"] == "Fake Student"
    finally:
        await c.aclose()
    _no_secret_anywhere(payload, capsys.readouterr().err)


@needs_display
async def test_mint_token_refused_by_hkust_falls_back_to_cookies(world, settings, capsys):
    payload = await auth.browser_login(settings, headless=False, mint_token_days=7)
    assert payload["kind"] == "cookies"
    post = next(e for e in world.events if e[0] == "POST" and e[2] == "/api/v1/users/self/tokens")
    assert post[4].get("x-csrf-token") == "csrf+token==", "CSRF header must be the URL-decoded cookie"
    assert "Canvas refused to mint a token (HTTP 401)" in capsys.readouterr().err


@needs_display
async def test_headful_times_out_if_user_never_signs_in(world, settings, monkeypatch):
    world.autosubmit = False
    monkeypatch.setattr(auth, "LOGIN_TIMEOUT_S", 4)
    with pytest.raises(TimeoutError):
        await auth.browser_login(settings, headless=False)
    assert not any(e[0] == "POST" for e in world.events)


# ------------------------------------------------------------- headless (terminal creds)
def _fake_prompts(monkeypatch, password=PASSWORD):
    monkeypatch.setattr("builtins.input", lambda prompt="": USER)
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt="": password)


async def test_headless_refuses_to_type_password_on_non_microsoft_host(world, settings, monkeypatch):
    """Phishing guard: the IdP here is NOT https://login.microsoftonline.com -> never fill the form."""
    world.autosubmit = False
    _fake_prompts(monkeypatch)
    monkeypatch.setattr(auth, "MS_REDIRECT_TIMEOUT_MS", 3000)
    with pytest.raises(RuntimeError, match="refusing to enter credentials"):
        await auth.browser_login(settings, headless=True)
    assert not any(e[0] == "POST" for e in world.events), "credentials must never be submitted"


def _trust_fake_idp(monkeypatch, world):
    monkeypatch.setattr(auth, "_is_trusted_idp", lambda url: urlparse(str(url)).hostname == "localhost")


async def test_headless_full_flow_with_mfa_number_and_kmsi(world, settings, monkeypatch, capsys):
    world.autosubmit, world.mfa = False, True
    _fake_prompts(monkeypatch)
    _trust_fake_idp(monkeypatch, world)
    payload = await auth.browser_login(settings, headless=True)
    assert {c["name"] for c in payload["cookies"]} >= {"canvas_session", "_csrf_token"}
    assert "ESTSAUTH" not in {c["name"] for c in payload["cookies"]}
    err = capsys.readouterr().err
    assert "enter the number: 42" in err
    submit = next(e for e in world.events if e[0] == "POST" and e[2] == "/ms/submit")
    assert submit[1] == "localhost", "password only ever sent to the IdP host"
    assert not any(e[0] == "POST" and e[1] == "127.0.0.1" and "passwd" in e[3] for e in world.events)
    _no_secret_anywhere(payload, err)


async def test_headless_wrong_password_reports_without_echoing_it(world, settings, monkeypatch, capsys):
    world.autosubmit = False
    _fake_prompts(monkeypatch, password="wrong-Pa55word")
    _trust_fake_idp(monkeypatch, world)
    monkeypatch.setattr(auth, "LOGIN_TIMEOUT_S", 15)
    with pytest.raises(RuntimeError, match="rejected the password") as ei:
        await auth.browser_login(settings, headless=True)
    assert "wrong-Pa55word" not in str(ei.value) and "wrong-Pa55word" not in capsys.readouterr().err
