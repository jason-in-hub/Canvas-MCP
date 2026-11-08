# canvas-mcp: HKUST Canvas → AI (read-only MCP server)

Lets Claude, or any other MCP client, read your HKUST Canvas courses, modules, lecture files (PDF/PPTX/DOCX/IPYNB), pages, announcements, assignments and calendar. You sign in with your normal HKUST Microsoft account and MFA. **Your password never goes through the AI, the MCP protocol, or onto disk.**

## How it works (the "reverse-engineering" part)

Canvas is open source and its REST API (`/api/v1/...`) is documented. The only part that isn't documented is how HKUST's login is wired up:

```
canvas.ust.hk/login
  └─302→ canvas.ust.hk/login/cas
  └─302→ cas.ust.hk/cas/login?service=…   (HKUST CAS, merged into Microsoft SSO since 2022)
  └─→ login.microsoftonline.com   (HKUST Entra ID tenant: email → password → MFA → "Stay signed in?")
  └─POST→ canvas.ust.hk               sets canvas_session, _csrf_token, log_session_id cookies
```

Once those cookies exist, Canvas's own web UI calls `/api/v1/*` with them. This server does the same thing for **GET** requests. Optionally, it can call `POST /api/v1/users/self/tokens` once, the same call the "New Access Token" button makes, to get a longer-lived personal access token.

## Install

```bash
git clone <this repo> && cd Canvas-MCP
# needs uv: https://docs.astral.sh/uv/  (curl -LsSf https://astral.sh/uv/install.sh | sh)
uv sync                              # creates .venv from uv.lock (project + dev deps)
uv run playwright install chromium
# optional, for whole-slide images in view_page:
#   macOS: brew install --cask libreoffice   Windows: winget install TheDocumentFoundation.LibreOffice
```

## Log in (in a terminal, not in the chat)

```bash
# inside the repo; or activate .venv and drop `uv run`
uv run canvas-mcp login                 # opens a real browser at Microsoft's sign-in page (recommended)
uv run canvas-mcp login --headless      # no display: hidden terminal prompt + Authenticator number shown
uv run canvas-mcp login --token         # paste a Canvas access token (*)
uv run canvas-mcp login --mint-token 30 # after SSO, ask Canvas for a 30-day token (*)
uv run canvas-mcp status
uv run canvas-mcp logout
```

(*) **HKUST has disabled user-generated access tokens** (confirmed 2026-11: "Your Canvas administrators have chosen to limit your ability to generate your own access token"). On HKUST use the default browser login or `--headless`; the token modes remain for other Canvas instances or an admin-issued token.

Use your full email (`abc@connect.ust.hk`), as [HKUST SSO](https://itso.hkust.edu.hk/node/1199) requires. Browser sessions usually last hours to days. When one expires, tools return `auth_required` and you log in again.

## Connect to Claude Desktop / Claude Code

`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "hkust-canvas": { "command": "uv", "args": ["--directory", "/ABS/PATH/Canvas-MCP", "run", "canvas-mcp", "serve"] }
  }
}
```

Claude Code: `claude mcp add hkust-canvas -- uv --directory /ABS/PATH/Canvas-MCP run canvas-mcp serve`

(Claude Desktop may not inherit your shell `PATH`; if `uv` isn't found, use its absolute path from `which uv`.)

## Tools

| Tool | Purpose |
|---|---|
| `auth_status` | Who am I / is the session valid |
| `open_login_window` | Opens Microsoft's sign-in page in a browser **for you** to type in; credentials never enter the chat |
| `list_courses`, `get_course` | Courses, syllabus, front page |
| `list_modules` | Module structure (usually where lecture notes live) |
| `list_files`, `read_file` | Files → Markdown with `--- page N ---` / `--- slide N ---` markers, paged with `offset`. Also returns `visual_pages`, the scanned or diagram-heavy pages the AI should look at |
| `view_page` | Image of one PDF page or slide (≤1400 px, ≈1.5k tokens), for diagrams, charts, typeset formulas and scans |
| `download_file` | Save to `~/.canvas-mcp/downloads` |
| `list_pages`, `get_page` | Wiki pages |
| `list_announcements`, `list_discussions` | Course communications |
| `list_assignments`, `get_assignment`, `upcoming` | Coursework and deadlines |
| `search_course` | Title search across files, pages and module items |

## How files are read

| Format | Engine | Why |
|---|---|---|
| PDF | [pymupdf4llm](https://github.com/pymupdf/pymupdf4llm) | Markdown tables and headings come out right. Firecrawl's anydoc 0.2.4 was benchmarked here and **merged adjacent table rows**, which put Gamma's formula on Delta's row without any error. |
| PPT/PPTX, DOC/DOCX, XLS/XLSX, ODF, RTF, EPUB, CSV | [Firecrawl anydoc](https://github.com/firecrawl/anydoc) (local, MIT) | Clean Markdown in milliseconds. For slides, `--- slide N ---` markers are rebuilt from the slide titles. |
| IPYNB, HTML, text | built-in | |

- Everything runs locally. anydoc's hosted OCR is never turned on.
- **Visual pages:** pages with no text layer (scans), or with charts or diagrams (solid vector shapes, plotted curves or large images), are listed in `visual_pages`. Bordered tables are not flagged.
- **`view_page`** renders PDFs with PyMuPDF. Slides and office files go through LibreOffice when it is installed; without it, `view_page` returns the pictures embedded in that slide.
- **Speed:** a 60-page PDF takes about 0.7 s. pymupdf4llm's GNN layout engine is about 12× slower, and `CANVAS_MCP_PDF_LAYOUT=1` turns it on. Downloaded files and extractions are cached in memory (last 4 files), so paging and `view_page` don't download again.
- **stdout safety:** pymupdf4llm prints to stdout at the C level, which corrupts the MCP stdio channel. `serve` moves fd 1 to stderr and gives the protocol its own descriptor (`stdio_guard.py`).
- **License:** PyMuPDF is AGPL-3.0. That is fine for personal use. Swap it out before redistributing this as a closed product.

## Security model: how credentials are kept from leaking

| Risk | Mitigation |
|---|---|
| Password typed into the AI chat → sent to the model provider and kept in transcripts | **No tool takes a username, password or token** (enforced by a test). Login happens only in the browser or in a hidden `getpass` prompt. |
| This server holding your password | Default browser mode: the server never sees it. Headless mode: the password is held only in memory, typed into the page, then dereferenced. It is never written to disk, argv, env or logs. |
| Phishing / wrong host | Headless mode types credentials only when the page is `https://login.microsoftonline.com` (or another Microsoft login host). Otherwise it aborts. |
| Session theft from disk | Only Canvas cookies (or a token) are kept, encrypted with Fernet. The key is in the OS keychain (Keychain / Credential Manager / Secret Service). Files are `0600`, the directory is `0700`, and loading refuses world-readable files. Microsoft SSO cookies are discarded and the browser context is incognito. |
| Token sent to the wrong server | Auth headers are attached only when host == `canvas.ust.hk`. Redirects (file downloads → S3/inst-fs) are followed by hand with auth stripped. HTTPS is required. |
| Capability URLs leaking to the AI | `verifier=`, `X-Amz-Signature`, `access_token` and similar are stripped from every tool output. |
| Logs | stderr only, with a redacting filter (Bearer tokens, cookies, `password=`, Canvas token pattern). httpx and Playwright debug logging are suppressed. |
| AI modifying your Canvas | The client is GET-only. There is no submit, post or delete path. |

Residual risks you should know about:

- Anyone who runs code as your OS user can read the keychain item and the session. That is true of any locally stored login.
- Course content you read goes to your AI provider. That is the whole point, but check your course's policy on uploading materials to AI.
- A minted token is long-lived. `canvas-mcp logout` deletes it locally, but you must also revoke it in Canvas → Account → Settings.
- Automated access may fall under HKUST's acceptable-use policy. Keep it personal and low-volume.

## Config (env vars)

`CANVAS_BASE_URL` (default `https://canvas.ust.hk`), `CANVAS_MCP_HOME` (default `~/.canvas-mcp`), `CANVAS_MCP_DOWNLOAD_DIR`, `CANVAS_MCP_MAX_TEXT_CHARS` (60000), `CANVAS_MCP_MAX_DOWNLOAD_MB` (100), `CANVAS_MCP_NO_KEYRING=1` (use the key-file fallback), `CANVAS_MCP_PDF_LAYOUT=1` (slower GNN PDF layout), `CANVAS_MCP_SOFFICE` (path to LibreOffice).

## Tests

```bash
uv sync && uv run playwright install chromium
xvfb-run -a uv run pytest -q        # headful login tests need a display; without one they skip
```

What the tests cover:

- **Mock Canvas API** (httpx): pagination, credentials stripped on off-host redirects, expiry detection, encryption and file permissions, redaction, end-to-end MCP tool calls, and the check that no tool takes credentials.
- **Extraction**:
  - The table-row regression is checked on a real Chromium-printed lecture PDF.
  - Scanned, chart and line-plot pages are flagged; bordered tables are not.
  - PPTX via anydoc keeps slide markers, tables and notes, handles untitled slides, and falls back to python-pptx.
  - DOCX and CSV go through anydoc.
  - A 60-page PDF finishes in under 5 s.
  - `read_file` paging reassembles the text exactly, and each file is downloaded only once.
- **`view_page`**: PDF image size bounds, slide rendering through LibreOffice, the embedded-picture fallback, and out-of-range pages.
- **stdio guard**: a real subprocess shows that C-level and `print()` noise go to stderr while the protocol bytes stay clean.
- **Real Playwright login against a fake Canvas → CAS → Microsoft chain** (two hostnames, so cookie scoping is real):
  - Default mode keeps only the Canvas cookies, and they then authenticate the API.
  - Microsoft cookies are dropped.
  - HKUST's token refusal falls back to cookies, and the CSRF header is checked.
  - Login times out cleanly.
  - Headless mode refuses to type a password on a non-Microsoft host.
  - Headless MFA number-match and the "Stay signed in? → No" screen work.
  - A wrong password is reported without echoing it.

Not covered: the real HKUST/Microsoft pages. Run `canvas-mcp login` once to confirm them.
