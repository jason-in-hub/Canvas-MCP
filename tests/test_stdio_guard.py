"""stdout is the MCP JSON-RPC channel: stray library output must never reach it."""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")


def test_isolate_stdout_routes_stray_output_to_stderr():
    code = textwrap.dedent("""
        import os, sys
        from canvas_mcp.stdio_guard import isolate_stdout
        isolate_stdout()
        os.write(1, b"C-LEVEL-NOISE\\n")          # like pymupdf4llm's parser messages
        print("PYTHON-PRINT-NOISE")               # like any chatty library
        sys.stdout.buffer.write(b'{"jsonrpc":"2.0"}\\n')  # what mcp's stdio_server writes
        sys.stdout.buffer.flush()
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, env={**os.environ, "PYTHONPATH": SRC}, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout == b'{"jsonrpc":"2.0"}\n'
    assert b"C-LEVEL-NOISE" in r.stderr and b"PYTHON-PRINT-NOISE" in r.stderr


def test_real_pdf_extraction_does_not_touch_stdout_when_guarded():
    fixture = str(Path(__file__).parent / "fixtures" / "lec04.pdf")
    code = textwrap.dedent(f"""
        import sys
        from canvas_mcp.stdio_guard import isolate_stdout
        isolate_stdout()
        from canvas_mcp.extract import extract
        r = extract("lec04.pdf", "application/pdf", open({fixture!r}, "rb").read())
        sys.stdout.buffer.write(("OK %d" % r.page_count).encode())
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, env={**os.environ, "PYTHONPATH": SRC}, timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout == b"OK 2", r.stdout


def test_unguarded_pymupdf4llm_does_pollute_stdout():
    """Documents WHY the guard exists (if this starts failing, upstream got quieter)."""
    fixture = str(Path(__file__).parent / "fixtures" / "lec04.pdf")
    code = f"import pymupdf, pymupdf4llm; pymupdf4llm.to_markdown(pymupdf.open({fixture!r}))"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=120)
    assert r.stdout.strip() != b""
