"""Runtime configuration (env-overridable, no secrets here)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_BASE_URL = "https://canvas.ust.hk"


@dataclass(frozen=True)
class Settings:
    base_url: str
    data_dir: Path
    download_dir: Path
    max_text_chars: int
    max_download_bytes: int

    @property
    def host(self) -> str:
        return urlparse(self.base_url).hostname or ""


def load_settings() -> Settings:
    base = os.environ.get("CANVAS_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme != "https" or not parsed.hostname:
        # Never send session cookies / tokens over plaintext HTTP.
        if not os.environ.get("CANVAS_MCP_ALLOW_INSECURE_TEST_URL"):
            raise ValueError(f"CANVAS_BASE_URL must be an https:// URL, got {base!r}")
    data_dir = Path(os.environ.get("CANVAS_MCP_HOME", Path.home() / ".canvas-mcp")).expanduser()
    download_dir = Path(
        os.environ.get("CANVAS_MCP_DOWNLOAD_DIR", data_dir / "downloads")
    ).expanduser()
    return Settings(
        base_url=base,
        data_dir=data_dir,
        download_dir=download_dir,
        max_text_chars=int(os.environ.get("CANVAS_MCP_MAX_TEXT_CHARS", "60000")),
        max_download_bytes=int(os.environ.get("CANVAS_MCP_MAX_DOWNLOAD_MB", "100")) * 1024 * 1024,
    )
