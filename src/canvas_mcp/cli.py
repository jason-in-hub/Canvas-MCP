"""`canvas-mcp` command line.

    canvas-mcp login [--headless | --token] [--mint-token DAYS]
    canvas-mcp status
    canvas-mcp logout
    canvas-mcp serve            # what your MCP client runs (stdio)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .auth import browser_login, token_login, verify_and_save
from .client import AuthRequired, CanvasClient, CanvasError, Session
from .config import load_settings
from .redact import configure_logging, scrub_text
from .store import SecretStore


def _login(args) -> int:
    settings = load_settings()
    store = SecretStore(settings.data_dir)

    async def run():
        if args.token:
            payload = await token_login(settings)
        else:
            payload = await browser_login(settings, headless=args.headless, mint_token_days=args.mint_token)
        return await verify_and_save(settings, store, payload), payload["kind"]

    try:
        who, kind = asyncio.run(run())
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.", file=sys.stderr)
        return 130
    except Exception as exc:  # never print tracebacks that could include locals
        print(f"Login failed: {type(exc).__name__}: {scrub_text(str(exc))}", file=sys.stderr)
        return 1
    print(f"Logged in to {settings.base_url} as {who.get('name')} (auth: {kind}). Session stored encrypted in {store.blob_path}.", file=sys.stderr)
    return 0


def _status(_args) -> int:
    settings = load_settings()
    sess = Session.from_payload(SecretStore(settings.data_dir).load())
    if sess is None:
        print("Not logged in. Run: canvas-mcp login", file=sys.stderr)
        return 1

    async def run():
        c = CanvasClient(settings.base_url, sess)
        try:
            return await c.whoami()
        finally:
            await c.aclose()

    try:
        me = asyncio.run(run())
    except (AuthRequired, CanvasError) as exc:
        print(f"Session invalid: {scrub_text(str(exc))}. Run: canvas-mcp login", file=sys.stderr)
        return 1
    print(f"OK: {me.get('name')} on {settings.base_url} (auth: {sess.kind})", file=sys.stderr)
    return 0


def _logout(_args) -> int:
    settings = load_settings()
    SecretStore(settings.data_dir).clear()
    print("Local session and encryption key removed. (If you minted a token, revoke it in Canvas > Account > Settings.)", file=sys.stderr)
    return 0


def _serve(_args) -> int:
    from .server import build_server

    build_server().run()
    return 0


def main(argv: list[str] | None = None) -> None:
    configure_logging(logging.INFO)
    p = argparse.ArgumentParser(prog="canvas-mcp", description="Read-only MCP server for HKUST Canvas")
    sub = p.add_subparsers(dest="cmd")

    lg = sub.add_parser("login", help="Sign in via HKUST Microsoft SSO and store an encrypted session")
    mode = lg.add_mutually_exclusive_group()
    mode.add_argument("--headless", action="store_true", help="No browser window: prompt for credentials in this terminal (hidden input)")
    mode.add_argument("--token", action="store_true", help="Paste an existing Canvas access token instead")
    lg.add_argument("--mint-token", type=int, metavar="DAYS", default=None,
                    help="After SSO, have Canvas issue a personal access token valid for DAYS (if HKUST permits)")
    lg.set_defaults(func=_login)
    sub.add_parser("status", help="Check the stored session").set_defaults(func=_status)
    sub.add_parser("logout", help="Delete the stored session and key").set_defaults(func=_logout)
    sub.add_parser("serve", help="Run the MCP server on stdio (default)").set_defaults(func=_serve)

    args = p.parse_args(argv)
    func = getattr(args, "func", _serve)
    sys.exit(func(args))
