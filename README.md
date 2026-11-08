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
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .
playwright install chromium
```

## Log in (in a terminal, not in the chat)

```bash
canvas-mcp login                 # opens a real browser at Microsoft's sign-in page (recommended)
canvas-mcp login --headless      # no display: hidden terminal prompt + Authenticator number shown
canvas-mcp login --token         # paste a Canvas access token (Account → Settings → New Access Token)
canvas-mcp login --mint-token 30 # after SSO, ask Canvas for a 30-day token (if HKUST allows it)
canvas-mcp status
canvas-mcp logout
```

Use your full email (`abc@connect.ust.hk`), as [HKUST SSO](https://itso.hkust.edu.hk/node/1199) requires. Browser sessions usually last hours to days. When one expires, tools return `auth_required` and you log in again.

## Connect to Claude Desktop / Claude Code

`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "hkust-canvas": { "command": "/ABS/PATH/Canvas-MCP/.venv/bin/canvas-mcp", "args": ["serve"] }
  }
}
```

Claude Code: `claude mcp add hkust-canvas -- /ABS/PATH/.venv/bin/canvas-mcp serve`

## Tools

| Tool | Purpose |
|---|---|
| `auth_status` | Who am I / is the session valid |
| `open_login_window` | Opens Microsoft's sign-in page in a browser **for you** to type in; credentials never enter the chat |
| `list_courses`, `get_course` | Courses, syllabus, front page |
| `list_modules` | Module structure (usually where lecture notes live) |
| `list_files`, `read_file`, `download_file` | Files → extracted text (paged with `offset`), or saved to `~/.canvas-mcp/downloads` |
| `list_pages`, `get_page` | Wiki pages |
| `list_announcements`, `list_discussions` | Course communications |
| `list_assignments`, `get_assignment`, `upcoming` | Coursework and deadlines |
| `search_course` | Title search across files, pages and module items |

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

`CANVAS_BASE_URL` (default `https://canvas.ust.hk`), `CANVAS_MCP_HOME` (default `~/.canvas-mcp`), `CANVAS_MCP_DOWNLOAD_DIR`, `CANVAS_MCP_MAX_TEXT_CHARS` (60000), `CANVAS_MCP_MAX_DOWNLOAD_MB` (100), `CANVAS_MCP_NO_KEYRING=1` (use the key-file fallback).

## Tests

```bash
pip install -e '.[dev]' && pytest -q
```

The tests use a mock Canvas. They cover pagination, off-host credential stripping, expiry detection, encryption and permissions, redaction, end-to-end MCP tool calls, and the check that no tool takes credentials.
