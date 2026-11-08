# Canvas-MCP: notes for Claude

- **Python env: use uv, never `python -m venv` / bare `pip`.**
  - Setup: `uv sync`, then `uv run playwright install chromium`
  - Run: `uv run canvas-mcp ...`
  - Tests: `xvfb-run -a uv run pytest -q` (headful login tests skip without a display)
  - Add deps with `uv add <pkg>` (dev: `uv add --dev <pkg>`) and commit `uv.lock`.
- Security invariants (enforced by tests; don't weaken):
  - No MCP tool may accept usernames, passwords, tokens or cookies.
  - The Canvas client stays GET-only. Auth goes only to the Canvas host.
  - Tool output is scrubbed of capability URLs.
  - In `serve`, stdout belongs to JSON-RPC (see `stdio_guard.py`).
- Extraction split: PDF uses pymupdf4llm (non-GNN by default); office formats use anydoc. See README "How files are read" for why.
