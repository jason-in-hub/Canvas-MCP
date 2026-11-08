"""Encrypted-at-rest storage for the Canvas session (cookies or API token).

What is stored: ONLY Canvas-side session material (canvas.ust.hk cookies or a
Canvas access token). Your HKUST/Microsoft username and password are never
stored, by design.

How: the payload is encrypted with Fernet (AES-128-CBC + HMAC-SHA256). The
Fernet key lives in the OS keychain (macOS Keychain / Windows Credential
Manager / Secret Service) via `keyring`. Only if no keychain backend exists do
we fall back to a 0600 key file, with a loud warning.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import time
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)

KEYRING_SERVICE = "canvas-mcp"
KEYRING_USER = "session-encryption-key"


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:  # pragma: no cover - e.g. Windows
        pass


def _write_private(path: Path, data: bytes) -> None:
    """Atomic write with 0600 perms from the first byte."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


class SecretStore:
    def __init__(self, data_dir: Path, use_keyring: bool | None = None):
        self.data_dir = data_dir
        self.blob_path = data_dir / "session.enc"
        self.keyfile_path = data_dir / ".key"
        if use_keyring is None:
            use_keyring = os.environ.get("CANVAS_MCP_NO_KEYRING") != "1"
        self._use_keyring = use_keyring

    # ---- key management -------------------------------------------------
    def _keyring(self):
        if not self._use_keyring:
            return None
        try:
            import keyring
            from keyring.backends import fail

            kr = keyring.get_keyring()
            if isinstance(kr, fail.Keyring) or "fail" in type(kr).__module__:
                return None
            return keyring
        except Exception:
            return None

    def _get_key(self, create: bool) -> bytes | None:
        kr = self._keyring()
        if kr is not None:
            try:
                existing = kr.get_password(KEYRING_SERVICE, KEYRING_USER)
                if existing:
                    return existing.encode()
                if create:
                    key = Fernet.generate_key()
                    kr.set_password(KEYRING_SERVICE, KEYRING_USER, key.decode())
                    return key
                return None
            except Exception as exc:  # backend present but broken (locked, headless)
                log.warning("OS keychain unavailable (%s); falling back to key file", type(exc).__name__)

        if self.keyfile_path.exists():
            self._check_perms(self.keyfile_path)
            return self.keyfile_path.read_bytes().strip()
        if not create:
            return None
        log.warning(
            "No OS keychain backend found. Storing the encryption key in %s (mode 0600). "
            "Anyone with read access to your user account can decrypt the session.",
            self.keyfile_path,
        )
        _ensure_private_dir(self.data_dir)
        key = Fernet.generate_key()
        _write_private(self.keyfile_path, key)
        return key

    @staticmethod
    def _check_perms(path: Path) -> None:
        if os.name != "posix":
            return
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise PermissionError(
                f"{path} is accessible by other users (mode {oct(mode)}). "
                f"Run: chmod 600 {path}"
            )

    # ---- public API -----------------------------------------------------
    def save(self, payload: dict[str, Any]) -> None:
        _ensure_private_dir(self.data_dir)
        key = self._get_key(create=True)
        assert key is not None
        payload = {**payload, "saved_at": int(time.time())}
        token = Fernet(key).encrypt(json.dumps(payload).encode())
        _write_private(self.blob_path, token)

    def load(self) -> dict[str, Any] | None:
        if not self.blob_path.exists():
            return None
        self._check_perms(self.blob_path)
        key = self._get_key(create=False)
        if key is None:
            log.warning("Encrypted session exists but its key is missing; please log in again.")
            return None
        try:
            return json.loads(Fernet(key).decrypt(self.blob_path.read_bytes()))
        except (InvalidToken, ValueError):
            log.warning("Stored session could not be decrypted; please log in again.")
            return None

    def clear(self) -> None:
        self.blob_path.unlink(missing_ok=True)
        self.keyfile_path.unlink(missing_ok=True)
        kr = self._keyring()
        if kr is not None:
            try:
                kr.delete_password(KEYRING_SERVICE, KEYRING_USER)
            except Exception:
                pass
