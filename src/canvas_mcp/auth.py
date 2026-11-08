"""Login flows for HKUST Canvas (Microsoft Entra ID SSO).

How HKUST Canvas auth works (observed 2026-11-08):
    canvas.ust.hk/login
      -> 302 canvas.ust.hk/login/cas
      -> 302 cas.ust.hk/cas/login?service=...   (HKUST CAS, fronted by Entra ID)
      -> login.microsoftonline.com (HKUST tenant)
      -> username (xxx@connect.ust.hk) -> password -> MFA (Authenticator
         number match / OTP) -> "Stay signed in?"
      -> POST back to canvas.ust.hk -> Canvas sets `canvas_session`,
         `_csrf_token`, `log_session_id` cookies.
After that, /api/v1/* accepts the session cookie for GETs, which is how the
Canvas web UI itself talks to the REST API.

Three ways to log in. NONE of them passes your credentials through the MCP
protocol or the AI model:

1. browser  (default, recommended)
   A real Chromium window opens on Microsoft's own login page. You type your
   credentials there. This program never sees them; it only reads the Canvas
   cookies once Canvas says you're logged in.

2. headless
   For machines without a display. Username/password are read from the
   terminal with getpass (no echo, never in argv/env/shell history), typed
   into Microsoft's page in a headless browser, and then dropped. The MFA
   number is printed so you can approve it on your phone.

3. token
   Paste a Canvas personal access token (Account > Settings > New Access
   Token), if HKUST allows it. Read via getpass.

Optionally `--mint-token` asks Canvas (using your fresh SSO session) to issue
a personal access token that expires in N days. That survives longer than a
browser session, at the cost of a long-lived secret on disk (still encrypted).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import getpass
import logging
import sys
import time
from typing import Any
from urllib.parse import unquote, urlparse

from .client import CanvasClient, Session
from .config import Settings
from .store import SecretStore

log = logging.getLogger(__name__)

MS_LOGIN_HOSTS = ("login.microsoftonline.com", "login.microsoft.com", "login.live.com")
LOGIN_TIMEOUT_S = 300
MS_REDIRECT_TIMEOUT_MS = 60_000


def _is_trusted_idp(url: str) -> bool:
    """Only ever type credentials into Microsoft's own login hosts over HTTPS."""
    p = urlparse(url)
    return p.scheme == "https" and (p.hostname or "") in MS_LOGIN_HOSTS


def _eprint(*a: Any) -> None:
    print(*a, file=sys.stderr, flush=True)


def _canvas_cookies(cookies: list[dict[str, Any]], host: str) -> list[dict[str, Any]]:
    """Keep only cookies scoped to the Canvas host; drop Microsoft's SSO cookies."""
    keep = []
    for c in cookies:
        dom = (c.get("domain") or "").lstrip(".")
        if host == dom or host.endswith("." + dom):
            keep.append({k: c[k] for k in ("name", "value", "domain", "path", "expires", "secure", "httpOnly") if k in c})
    return keep


async def _logged_in(page, base_url: str) -> bool:
    try:
        r = await page.request.get(f"{base_url}/api/v1/users/self", headers={"Accept": "application/json"}, max_redirects=0)
        if r.status != 200:
            return False
        data = await r.json()
        return isinstance(data, dict) and "id" in data
    except Exception:
        return False


async def _mint_token(page, base_url: str, days: int) -> str | None:
    """Ask Canvas to create a personal access token using the SSO session.

    This is the same call the "New Access Token" button in Canvas makes.
    Returns None if HKUST has disabled user-generated tokens.
    """
    cookies = await page.context.cookies(base_url)
    csrf = next((c["value"] for c in cookies if c["name"] == "_csrf_token"), None)
    if not csrf:
        return None
    expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = await page.request.post(
        f"{base_url}/api/v1/users/self/tokens",
        headers={"X-CSRF-Token": unquote(csrf), "Accept": "application/json"},
        form={"token[purpose]": "canvas-mcp (read-only use)", "token[expires_at]": expires},
    )
    if r.status not in (200, 201):
        _eprint(f"  Canvas refused to mint a token (HTTP {r.status}); falling back to session cookies.")
        return None
    data = await r.json()
    return data.get("visible_token") or data.get("token")


async def _ms_headless_drive(page, username: str, password: str) -> None:
    """Drive Microsoft's login page. Selectors are Entra ID's long-stable ids."""
    await page.wait_for_selector('input[name="loginfmt"]', timeout=60_000)
    await page.fill('input[name="loginfmt"]', username)
    await page.click('input[type="submit"]:visible')

    await page.wait_for_selector('input[name="passwd"]:visible, #usernameError', timeout=60_000)
    if await page.locator("#usernameError").count():
        raise RuntimeError("Microsoft rejected the username.")
    await page.fill('input[name="passwd"]', password)
    await page.click('input[type="submit"]:visible')
    # Do NOT clear the field afterwards: Microsoft's page reads it asynchronously
    # (an early clear can post an empty password), and once the page has moved on
    # to MFA the field is gone, so fill() would stall 30 s while the MFA number
    # is on screen. The incognito context is destroyed right after login anyway.


async def _ms_headless_followups(page, base_url: str, deadline: float) -> None:
    """Handle MFA / KMSI screens until Canvas reports a session."""
    shown_number = None
    while time.time() < deadline:
        if urlparse(page.url).hostname == urlparse(base_url).hostname and await _logged_in(page, base_url):
            return
        if _is_trusted_idp(page.url):
            if await page.locator("#passwordError").count():
                raise RuntimeError("Microsoft rejected the password.")
            num = page.locator("#idRichContext_DisplaySign")
            if await num.count() and await num.is_visible():
                n = (await num.inner_text()).strip()
                if n != shown_number:
                    _eprint(f"\n  >>> Open Microsoft Authenticator and enter the number: {n}\n")
                    shown_number = n
            otc = page.locator('input[name="otc"]')
            if await otc.count() and await otc.is_visible():
                code = await asyncio.to_thread(getpass.getpass, "  One-time code (SMS/Authenticator): ")
                await otc.fill(code.strip())
                await page.click('input[type="submit"]:visible')
                del code
            # "Stay signed in?" -> No (don't create long-lived MS cookies)
            kmsi = page.locator("#KmsiCheckboxField, #idBtn_Back")
            if await kmsi.count() and await page.locator("#idBtn_Back").is_visible():
                await page.click("#idBtn_Back")
        await asyncio.sleep(1.5)
    raise TimeoutError("Timed out waiting for login to complete.")


BROWSER_MISSING_HINT = (
    "No browser available for login. Install Playwright's Chromium in the SAME environment "
    "as canvas-mcp:  uv run playwright install chromium   (or, inside an activated venv: "
    "python -m playwright install chromium). Alternatively install Google Chrome or Microsoft Edge."
)


async def _launch_browser(pw, headless: bool):
    """Playwright's bundled Chromium first; fall back to an installed Chrome / Edge.

    The bundled browser is a separate download (`playwright install chromium`)
    that is easy to skip, or to run in a different environment.
    """
    attempts = [None, "chrome", "msedge"]
    last: Exception | None = None
    for channel in attempts:
        try:
            if channel is None:
                return await pw.chromium.launch(headless=headless)
            browser = await pw.chromium.launch(headless=headless, channel=channel)
            _eprint(f"(Playwright's Chromium not installed; using your installed {channel}.)")
            return browser
        except Exception as exc:  # executable missing for this channel -> try next
            msg = str(exc)
            if "Executable doesn't exist" not in msg and "is not found" not in msg and "not installed" not in msg.lower():
                raise
            last = exc
    raise RuntimeError(BROWSER_MISSING_HINT) from last


async def browser_login(settings: Settings, *, headless: bool = False, mint_token_days: int | None = None) -> dict[str, Any]:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Playwright missing: pip install playwright && playwright install chromium") from exc

    username = password = None
    if headless:
        username = input("HKUST email (e.g. abc@connect.ust.hk): ").strip()
        password = getpass.getpass("Password (input hidden, not stored): ")

    async with async_playwright() as pw:
        # Ephemeral (incognito) context: nothing about the MS login persists on disk.
        browser = await _launch_browser(pw, headless)
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto(f"{settings.base_url}/login", wait_until="domcontentloaded")
            deadline = time.time() + LOGIN_TIMEOUT_S
            if headless:
                try:
                    try:
                        await page.wait_for_url(lambda u: _is_trusted_idp(str(u)), timeout=MS_REDIRECT_TIMEOUT_MS)
                    except Exception as exc:
                        raise RuntimeError(
                            f"Login did not reach a Microsoft sign-in page (stuck at {urlparse(page.url).hostname}); "
                            "refusing to enter credentials."
                        ) from exc
                    # Phishing guard (re-checked after the wait): only type into Microsoft's own host.
                    if not _is_trusted_idp(page.url):
                        raise RuntimeError(f"Unexpected login host {urlparse(page.url).hostname}; refusing to enter credentials.")
                    await _ms_headless_drive(page, username, password)
                finally:
                    # Drop our references ASAP (CPython strings can't be zeroed, but
                    # this shortens their lifetime and keeps them out of tracebacks).
                    password = None
                    username = None
                await _ms_headless_followups(page, settings.base_url, deadline)
            else:
                _eprint("A browser window has opened. Sign in with your HKUST Microsoft account (incl. MFA).")
                _eprint("This program never sees your password; it waits for Canvas to confirm the login.")
                while time.time() < deadline:
                    if urlparse(page.url).hostname == settings.host and await _logged_in(page, settings.base_url):
                        break
                    await asyncio.sleep(1.5)
                else:
                    raise TimeoutError("Timed out waiting for you to finish signing in.")

            if mint_token_days:
                tok = await _mint_token(page, settings.base_url, mint_token_days)
                if tok:
                    return {"kind": "token", "token": tok, "minted": True, "expires_in_days": mint_token_days}

            cookies = _canvas_cookies(await context.cookies(), settings.host)
            if not cookies:
                raise RuntimeError("Login appeared to succeed but no Canvas cookies were found.")
            return {"kind": "cookies", "cookies": cookies}
        finally:
            await context.close()
            await browser.close()


async def verify_and_save(settings: Settings, store: SecretStore, payload: dict[str, Any]) -> dict[str, Any]:
    client = CanvasClient(settings.base_url, Session.from_payload(payload))
    try:
        me = await client.whoami()
    finally:
        await client.aclose()
    store.save(payload)
    return {"name": me.get("name"), "id": me.get("id")}


async def token_login(settings: Settings) -> dict[str, Any]:
    tok = getpass.getpass("Canvas access token (input hidden): ").strip()
    if not tok:
        raise RuntimeError("Empty token.")
    return {"kind": "token", "token": tok}
