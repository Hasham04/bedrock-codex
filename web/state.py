"""
Shared mutable state for the web server.

All global variables that are accessed across multiple route modules live here.
Import from web.state to read/write them.
"""

import atexit
import asyncio
import logging
import os
import time
from typing import Optional, Dict, Any, Tuple

from agent import CodingAgent
from backend import Backend

logger = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")

# ============================================================
# Globals
# ============================================================

_working_directory: str = "."
_backend: Optional[Backend] = None  # Set at startup; LocalBackend or SSHBackend
_explicit_dir: bool = False  # True if --dir was explicitly set (skip welcome)
_user_opened_project: bool = False  # True once user opens a project from welcome (skip welcome on refresh)
_ssh_info: Optional[Dict[str, Any]] = None  # Saved SSH details for current connection

# Shared agent reference so REST endpoints can access snapshots
_active_agent: Optional[CodingAgent] = None

# Directories/files to always skip in the file tree
_IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    ".env", ".mypy_cache", ".pytest_cache", ".tox", ".eggs",
    "dist", "build", ".next", ".nuxt", ".cache", ".DS_Store",
    "coverage", ".coverage", "htmlcov", ".idea", ".vscode",
}
_IGNORE_EXTENSIONS = {".pyc", ".pyo", ".so", ".dylib", ".o", ".a"}

# Max chars for file content served via API
_MAX_FILE_SIZE = 2 * 1024 * 1024  # 2 MB
_MAX_IMAGE_ATTACHMENTS = 3
_MAX_IMAGE_BYTES = 2 * 1024 * 1024  # 2 MB per image
_MAX_IMAGE_TOTAL_BYTES = 5 * 1024 * 1024  # 5 MB total raw image bytes
_ALLOWED_IMAGE_MEDIA_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
}

# ============================================================
# WebSocket reference wrapper (for reconnect-safe sends)
# ============================================================

from fastapi import WebSocket


class _WSRef:
    """Mutable WebSocket reference that silently drops sends when disconnected.

    All closures inside websocket_endpoint use ``wsr.send_json()`` instead of
    ``ws.send_json()`` directly.  When the WebSocket disconnects we set
    ``wsr.ws = None``; all in-flight sends become silent no-ops.  On reconnect
    we assign the new WebSocket and sends resume transparently — no need to
    recreate any closures or background tasks.
    """
    __slots__ = ("ws",)

    def __init__(self, ws: Optional[WebSocket]):
        self.ws: Optional[WebSocket] = ws

    async def send_json(self, data: Dict[str, Any]) -> None:
        _ws = self.ws
        if _ws is None:
            return
        try:
            await _ws.send_json(data)
        except Exception:
            self.ws = None          # mark disconnected on first failure


# Sessions with a running agent waiting for client reconnect.
# Maps session_id → {"future": asyncio.Future, "done_events": [asyncio.Event]}
_reconnect_sessions: Dict[str, Dict[str, Any]] = {}

# Global registry of active session save functions so the shutdown handler
# can persist all sessions before the process exits (e.g. Ctrl+C).
# Maps session_id → callable (the save_session closure from websocket_endpoint).
_active_save_fns: Dict[str, Any] = {}


def _atexit_save_all():
    """Last-resort save: called when the Python interpreter is shutting down."""
    for sid, save_fn in list(_active_save_fns.items()):
        try:
            save_fn()
        except Exception:
            pass

atexit.register(_atexit_save_all)


# ============================================================
# Background codebase index state
# ============================================================

_project_tree_cache: Dict[str, Tuple[float, str]] = {}
_PROJECT_TREE_TTL = 60.0  # refresh every 60s

_AUTO_CONTEXT_CHAR_BUDGET = 16000  # ~4000 tokens

# Background codebase index — built once on first WS connect, then reused
_bg_index_task: Optional[asyncio.Task] = None
_bg_index_ready = asyncio.Event()
_bg_codebase_index: Optional[Any] = None  # CodebaseIndex once built

_BOOT_TS = str(int(time.time()))  # unique per server start
