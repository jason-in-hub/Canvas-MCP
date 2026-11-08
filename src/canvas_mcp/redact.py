"""Scrubbing helpers so secrets never reach logs or the LLM.

Anything that leaves this process (log lines on stderr, tool results that go
into the model's context) passes through these functions.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Query params that act as bearer capabilities on Canvas / inst-fs / S3 URLs.
_SENSITIVE_QS = {
    "verifier",
    "access_token",
    "token",
    "sf_verifier",
    "signature",
    "x-amz-signature",
    "x-amz-credential",
    "x-amz-security-token",
    "sig",
    "jwt",
    "code",
    "session_token",
}

_PATTERNS = [
    # Authorization: Bearer xxx
    (re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?)(bearer\s+)?[^\s,\"'}]+"), r"\1\2[REDACTED]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9~._\-]{8,}"), r"\1[REDACTED]"),
    # Cookie headers / cookie values
    (re.compile(r"(?i)((?:set-)?cookie[\"']?\s*[:=]\s*[\"']?)[^\n\"']+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:canvas_session|_csrf_token|_legacy_normandy_session|log_session_id|pseudonym_credentials|ESTSAUTH\w*)=)[^;\s\"']+"), r"\1[REDACTED]"),
    # password=... / passwd=... / "password": "..."
    (re.compile(r"(?i)((?:password|passwd|pwd)[\"']?\s*[:=]\s*[\"']?)[^\s&\"',}]+"), r"\1[REDACTED]"),
    # Canvas access tokens look like "<shard>~<64 chars>"
    (re.compile(r"\b\d{1,6}~[A-Za-z0-9]{40,}\b"), "[REDACTED_TOKEN]"),
]


def scrub_text(text: str) -> str:
    if not text:
        return text
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text


def scrub_url(url: str | None) -> str | None:
    """Drop capability-bearing query params from a URL."""
    if not url:
        return url
    try:
        p = urlparse(url)
    except ValueError:
        return "[unparseable-url]"
    if not p.query:
        return url
    kept = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in _SENSITIVE_QS]
    return urlunparse(p._replace(query=urlencode(kept)))


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        record.msg = scrub_text(msg)
        record.args = ()
        return True


def configure_logging(level: int = logging.INFO) -> None:
    """Log to stderr only (stdout is the MCP stdio channel), always redacted."""
    handler = logging.StreamHandler()  # stderr
    handler.addFilter(RedactingFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Third-party libs that may log request headers / URLs at DEBUG.
    for noisy in ("httpx", "httpcore", "playwright", "asyncio", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
