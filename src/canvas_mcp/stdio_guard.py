"""Keep stray output off the MCP stdio channel.

In stdio mode, stdout carries JSON-RPC. Any library that prints to stdout breaks
the protocol, and the client disconnects. For example, pymupdf4llm prints
"Document parser messages" there, at the C level, so redirect_stdout can't
catch it.

isolate_stdout() is called once before the server starts:
* fd 1 is dup2'd onto stderr, so C-level writes to fd 1 go to stderr.
* sys.stdout becomes a shim. Its .write() (print) goes to stderr. Its .buffer
  is the original stdout pipe, which is what mcp's stdio_server wraps when it
  starts.
"""

from __future__ import annotations

import io
import os
import sys


class _StdoutShim(io.TextIOBase):
    def __init__(self, protocol_binary, err):
        self.buffer = protocol_binary  # used by mcp.server.stdio.stdio_server
        self._err = err

    def write(self, s: str) -> int:  # print() from any library -> stderr
        return self._err.write(s)

    def flush(self) -> None:
        self._err.flush()

    def fileno(self) -> int:
        return self._err.fileno()

    def isatty(self) -> bool:
        return False

    @property
    def encoding(self) -> str:
        return "utf-8"


def isolate_stdout() -> None:
    if isinstance(sys.stdout, _StdoutShim):
        return
    sys.stdout.flush()
    protocol_fd = os.dup(1)
    os.dup2(2, 1)  # C-level writes to fd 1 now land on stderr
    protocol = os.fdopen(protocol_fd, "wb", buffering=0)
    sys.stdout = _StdoutShim(protocol, sys.stderr)
