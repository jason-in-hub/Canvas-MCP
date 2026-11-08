"""MCP server exposing your HKUST Canvas content to an AI client (read-only).

Security model:
* No tool accepts a username, password or token. Credentials typed into a
  chat would be sent to the model provider and stored in transcripts, so
  login is done out-of-band (`canvas-mcp login`, or the `open_login_window`
  tool, which opens Microsoft's page in a real browser for YOU to type in).
* All tool output is passed through URL scrubbing (Canvas file URLs embed a
  `verifier` capability token) and secret redaction.
* GET-only client: the AI can read but never modify your Canvas.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any

from .client import AuthRequired, CanvasClient, CanvasError, Session
from .config import Settings, load_settings
from .extract import Extracted, extract, html_to_text, kind_of
from .render import RenderError, office_to_pdf, pptx_slide_pictures, render_pdf_page, soffice_path
from .redact import scrub_text, scrub_url
from .store import SecretStore, _ensure_private_dir

log = logging.getLogger(__name__)

try:  # mcp >= 2.0
    from mcp.server.mcpserver import Image, MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server, Image  # type: ignore

LOGIN_HINT = (
    "Not logged in to Canvas (or the session expired). Ask the user to run "
    "`canvas-mcp login` in a terminal, or call the `open_login_window` tool, "
    "which opens the official Microsoft sign-in page in a browser window. "
    "NEVER ask the user to type their password or token into the chat."
)

INSTRUCTIONS = (
    "Read-only access to the user's HKUST Canvas: courses, modules, files (lecture notes), "
    "pages, announcements, assignments, discussions, calendar. Typical flow: list_courses -> "
    "list_modules / list_files -> read_file. read_file returns Markdown plus `visual_pages` "
    "(scanned or diagram-heavy pages): call view_page on those, or whenever a formula, chart "
    "or figure matters, to see the actual page image. Never request credentials in chat; use "
    "open_login_window if auth is needed."
)


class Ctx:
    CACHE_FILES = 4

    def __init__(self, settings: Settings, store: SecretStore, client: CanvasClient | None = None):
        self.settings = settings
        self.store = store
        self._client = client
        # file_id -> {"updated_at", "meta", "data", "ctype", "extracted", "pdf"}; small, in-memory only
        self.files: OrderedDict[str, dict] = OrderedDict()

    def cache_put(self, file_id: str, entry: dict) -> None:
        self.files[file_id] = entry
        self.files.move_to_end(file_id)
        while len(self.files) > self.CACHE_FILES:
            self.files.popitem(last=False)

    def client(self) -> CanvasClient:
        if self._client is None:
            sess = Session.from_payload(self.store.load())
            if sess is None:
                raise AuthRequired(LOGIN_HINT)
            self._client = CanvasClient(self.settings.base_url, sess)
        return self._client

    async def reset(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        self._client = None
        self.files.clear()


# ---------------------------------------------------------------- helpers
def _pick(d: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: d.get(k) for k in keys if d.get(k) not in (None, "", [])}


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + f"\n\n[... truncated, {len(text) - n} more chars; use offset to read more]"


def _clean(obj: Any) -> Any:
    """Recursively scrub URLs/secrets from tool output."""
    if isinstance(obj, dict):
        return {k: (_clean(scrub_url(v)) if isinstance(v, str) and ("url" in k.lower() or v.startswith("http")) else _clean(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(x) for x in obj]
    if isinstance(obj, str):
        return scrub_text(obj)
    return obj


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._\- ()\[\]]+")


def build_server(ctx: Ctx | None = None) -> Any:
    ctx = ctx or Ctx(load_settings(), SecretStore(load_settings().data_dir))
    mcp = _Server("hkust-canvas", instructions=INSTRUCTIONS)

    async def guarded(coro_fn, *a, **kw):
        try:
            return _clean(await coro_fn(*a, **kw))
        except AuthRequired as exc:
            await ctx.reset()
            return {"error": "auth_required", "detail": str(exc), "hint": LOGIN_HINT}
        except CanvasError as exc:
            return {"error": "canvas_error", "detail": scrub_text(str(exc))}

    # ------------------------------------------------------------ auth
    @mcp.tool()
    async def auth_status() -> dict:
        """Check whether a valid Canvas session exists and who it belongs to."""
        async def run():
            me = await ctx.client().whoami()
            return {"logged_in": True, "user": _pick(me, "id", "name", "short_name", "primary_email", "login_id"), "auth": ctx.client().session.kind, "canvas": ctx.settings.base_url}
        return await guarded(run)

    @mcp.tool()
    async def open_login_window(mint_token_days: int = 0) -> dict:
        """Open the official HKUST Microsoft sign-in page in a visible browser window on
        the user's machine. The USER types username/password/MFA there; nothing is sent
        through this chat. Waits up to 5 minutes. Set mint_token_days>0 to have Canvas
        issue a personal access token with that expiry (longer-lived than a session)."""
        from .auth import browser_login, verify_and_save

        try:
            payload = await browser_login(ctx.settings, headless=False, mint_token_days=mint_token_days or None)
            who = await verify_and_save(ctx.settings, ctx.store, payload)
            await ctx.reset()
            return {"logged_in": True, "user": who, "auth": payload["kind"]}
        except Exception as exc:
            return {"logged_in": False, "error": type(exc).__name__, "detail": scrub_text(str(exc)), "hint": "If no display is available, run `canvas-mcp login --headless` in a terminal."}

    # ------------------------------------------------------------ courses
    @mcp.tool()
    async def list_courses(include_past: bool = False) -> list | dict:
        """List the user's Canvas courses (active by default; include_past for concluded ones)."""
        async def run():
            params: dict[str, Any] = {"include[]": ["term", "teachers", "total_students"]}
            if not include_past:
                params["enrollment_state"] = "active"
            data = await ctx.client().paginate("/api/v1/courses", params)
            out = []
            for c in data:
                if c.get("access_restricted_by_date"):
                    continue
                out.append({**_pick(c, "id", "name", "course_code", "workflow_state", "start_at", "end_at"),
                            "term": (c.get("term") or {}).get("name"),
                            "teachers": [t.get("display_name") for t in c.get("teachers") or []]})
            return out
        return await guarded(run)

    @mcp.tool()
    async def get_course(course_id: str) -> dict:
        """Course details incl. syllabus text and front page (if any)."""
        async def run():
            c = await ctx.client().get(f"/api/v1/courses/{course_id}", {"include[]": ["syllabus_body", "term", "teachers"]})
            out = _pick(c, "id", "name", "course_code", "start_at", "end_at", "default_view")
            out["term"] = (c.get("term") or {}).get("name")
            out["syllabus"] = _clip(html_to_text(c.get("syllabus_body")), ctx.settings.max_text_chars)
            if c.get("default_view") == "wiki":
                try:
                    fp = await ctx.client().get(f"/api/v1/courses/{course_id}/front_page")
                    out["front_page"] = _clip(html_to_text(fp.get("body")), 8000)
                except CanvasError:
                    pass
            return out
        return await guarded(run)

    @mcp.tool()
    async def list_modules(course_id: str) -> list | dict:
        """Modules and their items (files, pages, links, assignments) - usually how lecture notes are organised."""
        async def run():
            mods = await ctx.client().paginate(f"/api/v1/courses/{course_id}/modules", {"include[]": ["items"]})
            out = []
            for m in mods:
                items = m.get("items")
                if items is None:
                    items = await ctx.client().paginate(f"/api/v1/courses/{course_id}/modules/{m['id']}/items")
                out.append({**_pick(m, "id", "name", "position", "unlock_at", "state"),
                            "items": [_pick(i, "id", "title", "type", "content_id", "page_url", "external_url") for i in items]})
            return out
        return await guarded(run)

    # ------------------------------------------------------------ files
    @mcp.tool()
    async def list_files(course_id: str, search: str = "", content_type: str = "") -> list | dict:
        """List files in a course. Optional `search` (name substring, >=2 chars) and
        `content_type` (e.g. 'application/pdf'). Returns ids for read_file/download_file."""
        async def run():
            params: dict[str, Any] = {"sort": "updated_at", "order": "desc"}
            if len(search) >= 2:
                params["search_term"] = search
            if content_type:
                params["content_types[]"] = content_type
            files = await ctx.client().paginate(f"/api/v1/courses/{course_id}/files", params)
            folders = {}
            try:
                for f in await ctx.client().paginate(f"/api/v1/courses/{course_id}/folders"):
                    folders[f["id"]] = f.get("full_name")
            except CanvasError:
                pass
            return [{**_pick(f, "id", "display_name", "content-type", "size", "updated_at", "locked_for_user"),
                     "folder": folders.get(f.get("folder_id"))} for f in files]
        return await guarded(run)

    async def _fetch_file(file_id: str) -> dict:
        """Metadata is always re-fetched (cheap, and enforces locks); bytes are cached by updated_at."""
        meta = await ctx.client().get(f"/api/v1/files/{file_id}")
        if meta.get("locked_for_user"):
            raise CanvasError(f"File is locked: {meta.get('lock_explanation') or 'not yet available'}")
        cached = ctx.files.get(str(file_id))
        if cached and cached["updated_at"] == meta.get("updated_at"):
            ctx.files.move_to_end(str(file_id))
            cached["meta"] = meta
            return cached
        if (meta.get("size") or 0) > ctx.settings.max_download_bytes:
            raise CanvasError("File too large for the configured download cap.")
        data, ctype = await ctx.client().stream_download(meta["url"], ctx.settings.max_download_bytes)
        entry = {"updated_at": meta.get("updated_at"), "meta": meta, "data": data,
                 "ctype": meta.get("content-type") or ctype, "extracted": None, "pdf": None}
        ctx.cache_put(str(file_id), entry)
        return entry

    def _name(meta: dict) -> str:
        return meta.get("display_name") or meta.get("filename") or ""

    @mcp.tool()
    async def read_file(file_id: str, offset: int = 0, max_chars: int = 0) -> dict:
        """Download a course file and return it as Markdown. PDFs and slides are split by
        `--- page N ---` / `--- slide N ---`. Formats: PDF, PPT/PPTX, DOC/DOCX, XLS/XLSX,
        ODF, RTF, EPUB, CSV, IPYNB, HTML, text. `visual_pages` lists pages whose content is
        mainly visual (scans, charts, diagrams): use view_page for those. Page through long
        documents with offset/max_chars."""
        async def run():
            entry = await _fetch_file(file_id)
            meta = entry["meta"]
            if entry["extracted"] is None:
                entry["extracted"] = await asyncio.to_thread(extract, _name(meta), entry["ctype"], entry["data"])
            ex: Extracted = entry["extracted"]
            base = {**_pick(meta, "id", "display_name", "content-type", "size", "updated_at"),
                    "kind": ex.kind, "engine": ex.engine}
            if ex.page_count:
                base["page_count"] = ex.page_count
            if ex.visual_pages:
                base["visual_pages"] = ex.visual_pages
                base["hint"] = "Call view_page(file_id, page) for the pages in visual_pages before relying on their text."
            if ex.note:
                base["note"] = ex.note
            if ex.text is None:
                return {**base, "text": None,
                        "note": ex.note or "Unsupported format for text extraction; use download_file to save it locally."}
            text = ex.text
            limit = max_chars or ctx.settings.max_text_chars
            chunk = text[offset: offset + limit]
            return {**base, "total_chars": len(text), "offset": offset, "text": chunk,
                    "has_more": offset + limit < len(text)}
        return await guarded(run)

    @mcp.tool(structured_output=False)  # returns image content, not JSON
    async def view_page(file_id: str, page: int = 1):
        """Return an image of one page (PDF) or slide (PPT/PPTX, also DOCX etc.) so you can SEE
        diagrams, charts, handwriting, scanned notes and typeset formulas. Costs ~1.5k tokens
        per image, so use it on pages that matter (e.g. those in read_file's visual_pages)."""
        async def run():
            entry = await _fetch_file(file_id)
            meta, name = entry["meta"], _name(entry["meta"])
            kind = kind_of(name, entry["ctype"])
            note = None
            if kind == "pdf":
                r = await asyncio.to_thread(render_pdf_page, entry["data"], page)
            elif kind in ("slides", "office"):
                if entry["pdf"] is None and soffice_path():
                    entry["pdf"] = await asyncio.to_thread(office_to_pdf, entry["data"], name)
                if entry["pdf"] is not None:
                    r = await asyncio.to_thread(render_pdf_page, entry["pdf"], page)
                elif name.lower().endswith((".pptx", ".pptm", ".ppsx")):
                    r = await asyncio.to_thread(pptx_slide_pictures, entry["data"], page)
                else:
                    return {"error": "cannot_render", "detail": "Install LibreOffice to render this format."}
            else:
                return {"error": "cannot_render", "detail": f"view_page supports PDF and office files, not {kind}."}
            note = r.note
            label = "slide" if kind == "slides" else "page"
            header = f"{meta.get('display_name')}: {label} {page} of {r.page_count}" + (f". {note}" if note else "")
            if not r.images:
                return {"error": "no_image", "detail": header}
            return [header, *[Image(data=d, format=fmt) for d, fmt in r.images]]

        try:
            return await run()
        except AuthRequired as exc:
            await ctx.reset()
            return {"error": "auth_required", "detail": str(exc), "hint": LOGIN_HINT}
        except (CanvasError, RenderError) as exc:
            return {"error": "cannot_render" if isinstance(exc, RenderError) else "canvas_error", "detail": scrub_text(str(exc))}

    @mcp.tool()
    async def download_file(file_id: str) -> dict:
        """Save a course file to the local download folder (~/.canvas-mcp/downloads/<course>/)."""
        async def run():
            entry = await _fetch_file(file_id)
            meta, data = entry["meta"], entry["data"]
            name = _SAFE_NAME.sub("_", meta.get("display_name") or f"file_{file_id}").strip(" .") or f"file_{file_id}"
            ctx_folder = _SAFE_NAME.sub("_", str(meta.get("folder_id") or "misc"))
            dest_dir: Path = ctx.settings.download_dir / ctx_folder
            _ensure_private_dir(dest_dir)
            dest = (dest_dir / name).resolve()
            if ctx.settings.download_dir.resolve() not in dest.parents:
                raise CanvasError("Refusing to write outside the download directory.")
            dest.write_bytes(data)
            return {"saved_to": str(dest), "bytes": len(data)}
        return await guarded(run)

    # ------------------------------------------------------------ pages
    @mcp.tool()
    async def list_pages(course_id: str, search: str = "") -> list | dict:
        """List wiki pages in a course."""
        async def run():
            params: dict[str, Any] = {"sort": "updated_at", "order": "desc"}
            if search:
                params["search_term"] = search
            pages = await ctx.client().paginate(f"/api/v1/courses/{course_id}/pages", params)
            return [_pick(p, "page_id", "url", "title", "updated_at", "front_page") for p in pages]
        return await guarded(run)

    @mcp.tool()
    async def get_page(course_id: str, page_url: str) -> dict:
        """Read a wiki page (page_url is the slug from list_pages / module items)."""
        async def run():
            p = await ctx.client().get(f"/api/v1/courses/{course_id}/pages/{page_url}")
            return {**_pick(p, "title", "url", "updated_at"), "text": _clip(html_to_text(p.get("body")), ctx.settings.max_text_chars)}
        return await guarded(run)

    # ------------------------------------------------------------ comms
    @mcp.tool()
    async def list_announcements(course_ids: list[str], days: int = 60) -> list | dict:
        """Recent announcements for the given courses (last `days` days)."""
        import datetime as dt

        async def run():
            start = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%d")
            params = {"context_codes[]": [f"course_{c}" for c in course_ids], "start_date": start}
            items = await ctx.client().paginate("/api/v1/announcements", params, limit=200)
            return [{**_pick(a, "id", "title", "posted_at", "context_code", "author"),
                     "author": (a.get("author") or {}).get("display_name"),
                     "text": _clip(html_to_text(a.get("message")), 4000)} for a in items]
        return await guarded(run)

    @mcp.tool()
    async def list_discussions(course_id: str) -> list | dict:
        """Discussion topics in a course (titles + opening post)."""
        async def run():
            items = await ctx.client().paginate(f"/api/v1/courses/{course_id}/discussion_topics", limit=200)
            return [{**_pick(d, "id", "title", "posted_at", "discussion_subentry_count"),
                     "text": _clip(html_to_text(d.get("message")), 2000)} for d in items]
        return await guarded(run)

    # ------------------------------------------------------------ work
    @mcp.tool()
    async def list_assignments(course_id: str, bucket: str = "") -> list | dict:
        """Assignments in a course. bucket: '', 'upcoming', 'past', 'overdue', 'undated', 'unsubmitted'."""
        async def run():
            params: dict[str, Any] = {"order_by": "due_at", "include[]": ["submission"]}
            if bucket:
                params["bucket"] = bucket
            items = await ctx.client().paginate(f"/api/v1/courses/{course_id}/assignments", params)
            return [{**_pick(a, "id", "name", "due_at", "points_possible", "submission_types", "html_url"),
                     "submitted": ((a.get("submission") or {}).get("workflow_state")),
                     "score": ((a.get("submission") or {}).get("score"))} for a in items]
        return await guarded(run)

    @mcp.tool()
    async def get_assignment(course_id: str, assignment_id: str) -> dict:
        """Full assignment description."""
        async def run():
            a = await ctx.client().get(f"/api/v1/courses/{course_id}/assignments/{assignment_id}")
            return {**_pick(a, "id", "name", "due_at", "lock_at", "points_possible", "submission_types", "allowed_extensions"),
                    "description": _clip(html_to_text(a.get("description")), ctx.settings.max_text_chars)}
        return await guarded(run)

    @mcp.tool()
    async def upcoming() -> list | dict:
        """User's upcoming events and to-dos across all courses."""
        async def run():
            ev = await ctx.client().get("/api/v1/users/self/upcoming_events")
            return [{**_pick(e, "id", "title", "start_at", "end_at", "context_code", "type"),
                     "due_at": (e.get("assignment") or {}).get("due_at")} for e in ev]
        return await guarded(run)

    @mcp.tool()
    async def search_course(course_id: str, query: str) -> dict:
        """Find files, pages and module items whose title matches `query` (case-insensitive)."""
        async def run():
            q = query.lower()
            res: dict[str, list] = {"files": [], "pages": [], "module_items": []}
            for f in await ctx.client().paginate(f"/api/v1/courses/{course_id}/files", {"search_term": query} if len(query) >= 2 else None):
                res["files"].append(_pick(f, "id", "display_name"))
            try:
                for p in await ctx.client().paginate(f"/api/v1/courses/{course_id}/pages", {"search_term": query}):
                    res["pages"].append(_pick(p, "url", "title"))
            except CanvasError:
                pass
            for m in await ctx.client().paginate(f"/api/v1/courses/{course_id}/modules", {"include[]": ["items"], "search_term": query}):
                for i in m.get("items") or []:
                    if q in (i.get("title") or "").lower():
                        res["module_items"].append({"module": m.get("name"), **_pick(i, "title", "type", "content_id", "page_url")})
            return res
        return await guarded(run)

    return mcp


def main() -> None:
    build_server().run()
