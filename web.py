"""
Bedrock Codex — Web GUI server.
FastAPI + WebSocket bridge to the CodingAgent.

Run:  python web.py [--port 8765] [--dir /path/to/project]
Open: http://localhost:8765
"""

import argparse
import asyncio
import atexit
import base64
import errno
import difflib
import json
import logging
import mimetypes
import os
import posixpath
import re
import shlex
import struct
import sys
import termios
import threading
import time
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from bedrock_service import BedrockService, BedrockError
from agent import CodingAgent, AgentEvent, classify_intent
from backend import Backend, LocalBackend, SSHBackend
from sessions import SessionStore, Session
from config import (
    get_model_name,
    get_model_config,
    get_context_window,
    model_config,
    app_config,
    supports_thinking,
    supports_caching,
)

logger = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(title="Bedrock Codex")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("shutdown")
async def _on_shutdown():
    """Save all active sessions before the server exits (e.g. Ctrl+C).

    Without this, a hard server kill loses any state that changed since the
    last periodic auto-save (every 30s), including pending keep/revert bars,
    plans awaiting approval, file snapshots, etc.
    """
    for sid, save_fn in list(_active_save_fns.items()):
        try:
            save_fn()
            logger.info("Shutdown: saved session %s", sid)
        except Exception as exc:
            logger.error("Shutdown: failed to save session %s: %s", sid, exc)
    _active_save_fns.clear()


@app.middleware("http")
async def no_cache_static(request, call_next):
    """Prevent browser caching of static assets during development."""
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


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
# Auto-context assembly (Cursor-style automatic context injection)
# ============================================================

_project_tree_cache: Dict[str, Tuple[float, str]] = {}
_PROJECT_TREE_TTL = 60.0  # refresh every 60s

_AUTO_CONTEXT_CHAR_BUDGET = 16000  # ~4000 tokens

# Background codebase index — built once on first WS connect, then reused
_bg_index_task: Optional[asyncio.Task] = None
_bg_index_ready = asyncio.Event()
_bg_codebase_index: Optional[Any] = None  # CodebaseIndex once built


async def _build_index_background(backend: Backend, working_directory: str):
    """Build codebase index in background thread on first WS connect."""
    global _bg_codebase_index
    try:
        from codebase_index import get_index, set_embed_fn
        from config import app_config

        if not getattr(app_config, "codebase_index_enabled", True):
            _bg_index_ready.set()
            return

        svc = BedrockService()
        if hasattr(svc, "embed_texts"):
            set_embed_fn(svc.embed_texts)

        idx = await asyncio.to_thread(
            lambda: get_index(working_directory, embed_fn=svc.embed_texts if hasattr(svc, "embed_texts") else None, backend=backend)
        )

        if not idx.chunks:
            await asyncio.to_thread(lambda: idx.build(backend))

        _bg_codebase_index = idx
        logger.info("Background index ready: %d chunks", len(idx.chunks))
    except Exception as e:
        logger.warning("Background index build failed: %s", e)
    finally:
        _bg_index_ready.set()


def _assemble_auto_context(
    working_directory: str,
    editor_context: Optional[Dict[str, Any]] = None,
    modified_files: Optional[set] = None,
    backend: Optional[Backend] = None,
    user_query: Optional[str] = None,
) -> str:
    """Assemble auto-context that gets injected into the agent conversation.

    Works with both local and SSH backends. Uses the Backend abstraction for
    all file reads and command execution.

    Priority order (each section is added only if budget remains):
    1. Active file (window around cursor, or first 200 lines)
    2. Selected text (if any)
    3. Recently modified files by agent (last 3, first 50 lines each)
    3.5. Dependency-aware context (1-hop imports)
    4. Git diff summary
    5. Project tree (cached)
    6. Open files list

    Returns empty string if no useful context available.
    """
    from backend import LocalBackend as _LB
    b = backend or _LB(os.path.abspath(working_directory))
    sections: List[str] = []
    budget = _AUTO_CONTEXT_CHAR_BUDGET
    editor_context = editor_context or {}

    def add_section(label: str, content: str) -> bool:
        nonlocal budget
        section = f"<{label}>\n{content.rstrip()}\n</{label}>"
        if len(section) <= budget:
            sections.append(section)
            budget -= len(section)
            return True
        elif budget > 200:
            truncated = content[:budget - 100].rstrip()
            section = f"<{label}>\n{truncated}\n… (truncated)\n</{label}>"
            sections.append(section)
            budget -= len(section)
            return True
        return False

    abs_wd = b.working_directory if hasattr(b, 'working_directory') else os.path.abspath(working_directory)

    def _read_file_safe(path: str) -> Optional[str]:
        """Read a file via Backend (works for both local and SSH)."""
        try:
            return b.read_file(path)
        except Exception:
            return None

    # 1. Active file
    active = editor_context.get("activeFile", {})
    active_path = active.get("path", "")
    if active_path:
        cursor_line = active.get("cursorLine")
        try:
            content = _read_file_safe(active_path)
            if content is not None:
                all_lines = content.splitlines(keepends=True)
                total = len(all_lines)
                if cursor_line and total > 200:
                    start = max(0, cursor_line - 100)
                    end = min(total, cursor_line + 100)
                    window = all_lines[start:end]
                    header = f"# {active_path} (lines {start+1}-{end} of {total}, cursor at line {cursor_line})\n"
                else:
                    window = all_lines[:200]
                    header = f"# {active_path} ({total} lines total" + (f", cursor at line {cursor_line}" if cursor_line else "") + ")\n"
                    if total > 200:
                        header = f"# {active_path} (showing first 200 of {total} lines)\n"
                numbered = "".join(f"{start + i + 1 if cursor_line and total > 200 else i + 1:6}|{l}" for i, l in enumerate(window))
                add_section("active_file", header + numbered)
        except Exception:
            pass

    # 2. Selected text
    selected = editor_context.get("selectedText", "")
    if selected:
        add_section("selected_text", f"# Selected in {active_path}\n{selected}")

    # 3. Recently modified files (by agent)
    if modified_files:
        recent_mods = list(modified_files)[-3:]
        for mp in recent_mods:
            if mp == active_path:
                continue
            try:
                content = _read_file_safe(mp)
                if content is not None:
                    lines = content.splitlines(keepends=True)[:50]
                    text = f"# {mp} (recently modified, first {len(lines)} lines)\n" + "".join(lines)
                    add_section("modified_file", text)
            except Exception:
                pass

    # 3.5. Dependency-aware context (1-hop imports of active file)
    if active_path and budget > 500:
        try:
            from codebase_index import get_index, get_dependency_neighborhood
            idx = get_index(abs_wd)
            if idx.file_imports:
                neighbors = get_dependency_neighborhood(
                    active_path, idx.file_imports, idx.reverse_imports, max_neighbors=5
                )
                if neighbors:
                    dep_lines = [f"# Related files (1-hop imports of {active_path})"]
                    for np_ in neighbors:
                        try:
                            dep_content = _read_file_safe(np_)
                            if dep_content is not None:
                                first_lines = dep_content.splitlines(keepends=True)[:20]
                                dep_lines.append(f"\n## {np_} (first 20 lines)")
                                dep_lines.append("".join(first_lines))
                        except Exception:
                            pass
                    if len(dep_lines) > 1:
                        add_section("dependency_context", "\n".join(dep_lines))
        except Exception:
            pass

    # 3.7. Semantic search — use background index to find relevant chunks for the query
    if user_query and budget > 1000:
        try:
            idx = _bg_codebase_index
            if idx and idx.chunks:
                results = idx.retrieve(user_query, top_k=5)
                if results:
                    sem_lines = ["# Relevant code (semantic search)"]
                    for chunk in results:
                        snippet = chunk.to_search_snippet(max_lines=20)
                        sem_lines.append(snippet)
                    add_section("semantic_context", "\n\n".join(sem_lines))
        except Exception:
            pass

    # 4. Git diff summary (via Backend.run_command for SSH support)
    try:
        stdout, _, rc = b.run_command("git diff --stat", ".", timeout=5)
        if rc == 0 and stdout and stdout.strip():
            diff_stat = stdout.strip()
            diff_content = f"# git diff --stat\n{diff_stat}"
            if len(diff_stat) < 2000:
                stdout2, _, rc2 = b.run_command("git diff --no-color", ".", timeout=5)
                if rc2 == 0 and stdout2:
                    diff_lines = stdout2.split("\n")[:50]
                    diff_content += "\n\n# git diff (first 50 lines)\n" + "\n".join(diff_lines)
            add_section("git_diff", diff_content)
    except Exception:
        pass

    # 5. Project tree (cached)
    cache_key = abs_wd
    now = time.time()
    cached = _project_tree_cache.get(cache_key)
    if cached and (now - cached[0]) < _PROJECT_TREE_TTL:
        tree_text = cached[1]
    else:
        try:
            from tools import project_tree
            result = project_tree(backend=b, working_directory=abs_wd)
            tree_text = result.output if result.success else ""
            _project_tree_cache[cache_key] = (now, tree_text)
        except Exception:
            tree_text = ""
    if tree_text:
        add_section("project_structure", tree_text)

    # 6. Linter errors on recently modified and active files
    if budget > 300:
        lint_targets = set()
        if active_path:
            lint_targets.add(active_path)
        if modified_files:
            lint_targets.update(list(modified_files)[-3:])
        if lint_targets:
            try:
                from tools import lint_file as _lint_file
                lint_errors = []
                for lp in lint_targets:
                    try:
                        lr = _lint_file(path=lp, backend=b, working_directory=abs_wd)
                        if lr.success and lr.output and "no issues" not in lr.output.lower():
                            lint_errors.append(f"## {lp}\n{lr.output[:500]}")
                    except Exception:
                        pass
                if lint_errors:
                    add_section("linter_errors", "# Linter errors on active/modified files\n" + "\n\n".join(lint_errors))
            except Exception:
                pass

    # 7. Open files list
    open_files = editor_context.get("openFiles", [])
    if open_files and len(open_files) > 1:
        file_list = "\n".join(f"  {f}" for f in open_files if f != active_path)
        if file_list:
            add_section("open_files", f"# Other open files in editor\n{file_list}")

    if not sections:
        return ""

    return "<auto_context>\n" + "\n\n".join(sections) + "\n</auto_context>"


def active_file_in_context(auto_ctx: str) -> bool:
    """Check if auto-context contains an active file section."""
    return "<active_file>" in auto_ctx


# ============================================================
# @ Mention resolution
# ============================================================

_MENTION_RE = re.compile(r"@([\w./_-]+)")
_MENTION_TOKEN_CAP = 6000  # ~1500 tokens per mention, cap total


def _resolve_mentions(task_text: str, working_directory: str, backend: Optional[Backend] = None) -> str:
    """Resolve @file and @special mentions in the task text.

    Works with both local and SSH backends.
    Returns modified task text with mentions replaced by inline references.
    """
    from backend import LocalBackend as _LB
    b = backend or _LB(os.path.abspath(working_directory))
    abs_wd = b.working_directory if hasattr(b, 'working_directory') else os.path.abspath(working_directory)
    mentions = list(_MENTION_RE.finditer(task_text))
    if not mentions:
        return task_text

    resolved_parts: List[str] = []
    budget = _MENTION_TOKEN_CAP

    for m in mentions:
        ref = m.group(1)

        if ref == "codebase":
            try:
                from tools import project_tree
                result = project_tree(backend=b, working_directory=abs_wd)
                if result.success and budget > len(result.output):
                    tag = f"\n<mentioned_codebase>\n{result.output}\n</mentioned_codebase>\n"
                    resolved_parts.append(tag)
                    budget -= len(tag)
            except Exception:
                pass
            continue

        if ref == "git":
            try:
                stdout, _, rc = b.run_command("git diff --no-color", ".", timeout=10)
                if rc == 0 and stdout and stdout.strip():
                    content = stdout[:3000]
                    if budget > len(content):
                        tag = f"\n<mentioned_git_diff>\n{content}\n</mentioned_git_diff>\n"
                        resolved_parts.append(tag)
                        budget -= len(tag)
            except Exception:
                pass
            continue

        if ref == "terminal":
            resolved_parts.append("\n<mentioned_terminal>(terminal context not available in this environment)</mentioned_terminal>\n")
            continue

        try:
            if b.is_file(ref):
                content = b.read_file(ref)
                if len(content) > 4000:
                    content = content[:4000] + "\n… (truncated)"
                if budget > len(content):
                    tag = f"\n<mentioned_file path=\"{ref}\">\n{content}\n</mentioned_file>\n"
                    resolved_parts.append(tag)
                    budget -= len(tag)
        except Exception:
            pass

    if resolved_parts:
        return task_text + "\n" + "".join(resolved_parts)
    return task_text


# ============================================================
# REST API
# ============================================================

_BOOT_TS = str(int(time.time()))  # unique per server start

@app.get("/")
async def index():
    """Serve index.html with a dynamic cache-buster so the browser
    always picks up the latest JS/CSS after a server restart."""
    html_path = os.path.join(STATIC_DIR, "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    # Replace static version tags with the boot timestamp
    html = html.replace("style.css?v=", f"style.css?v={_BOOT_TS}&_v=")
    html = html.replace("app.js?v=", f"app.js?v={_BOOT_TS}&_v=")
    resp = HTMLResponse(html)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.get("/api/info")
async def info():
    """Return model and config info for the frontend."""
    mcfg = get_model_config(model_config.model_id)
    return {
        "model_name": get_model_name(model_config.model_id),
        "model_id": model_config.model_id,
        "context_window": mcfg.get("context_window", 0),
        "max_output_tokens": mcfg.get("max_output_tokens", 0),
        "thinking": supports_thinking(model_config.model_id),
        "caching": supports_caching(model_config.model_id),
        "working_directory": os.path.abspath(_working_directory),
        "plan_phase_enabled": app_config.plan_phase_enabled,
        "show_welcome": not _explicit_dir and not _user_opened_project,
    }


@app.get("/api/sessions")
async def list_sessions():
    def _session_wd_key() -> str:
        if _ssh_info is not None:
            return _working_directory
        return os.path.abspath(_working_directory)

    store = SessionStore()
    sessions = store.list_sessions(_session_wd_key())
    return [
        {
            "session_id": s.session_id,
            "name": s.name,
            "message_count": s.message_count,
            "total_tokens": s.total_tokens,
            "updated_at": s.updated_at,
        }
        for s in sessions
    ]


@app.post("/api/sessions/new")
async def create_session(request: Request):
    def _session_wd_key() -> str:
        if _ssh_info is not None:
            return _working_directory
        return os.path.abspath(_working_directory)

    def _unique_name(store: SessionStore, wd_key: str, raw_name: str) -> str:
        base = (raw_name or "agent").strip() or "agent"
        existing = {s.name.strip().lower() for s in store.list_sessions(wd_key)}
        if base.lower() not in existing:
            return base
        idx = 2
        while f"{base} {idx}".lower() in existing:
            idx += 1
        return f"{base} {idx}"

    try:
        body = await request.json()
    except Exception:
        body = {}

    store = SessionStore()
    wd_key = _session_wd_key()
    desired_name = str(body.get("name", "") or "").strip()
    session_name = _unique_name(store, wd_key, desired_name)
    session = store.create_session(wd_key, model_config.model_id, name=session_name)
    store.save(session)
    return {
        "ok": True,
        "session_id": session.session_id,
        "name": session.name,
        "updated_at": session.updated_at,
    }


@app.get("/api/projects")
async def list_projects():
    """List all known projects from session history — used by the welcome screen."""
    store = SessionStore()
    return store.list_all_projects()


@app.post("/api/projects/remove")
async def remove_project(request: Request):
    """Remove a project from recents (deletes all its session files). Body: { \"path\": \"...\" }."""
    try:
        body = await request.json() if request.body else {}
    except Exception:
        body = {}
    path = (body.get("path") or "").strip()
    if not path:
        return JSONResponse({"ok": False, "error": "path required"}, status_code=400)
    store = SessionStore()
    try:
        n = store.delete_all_sessions_for_project(path)
        return {"ok": True, "deleted": n}
    except Exception as e:
        logger.exception("Remove project failed")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/ssh-list-dir")
async def ssh_list_dir(request: Request):
    """List a directory on a remote host via a one-off SSH connection.
    Used by the SSH connect flow to let the user browse and pick a folder.
    Body: host, user, port?, key_path?, directory? (default '~').
    Returns: { ok, path, parent, entries: [ { name, type, ... } ] }."""
    body = await request.json() if request.body else {}
    host = str(body.get("host", "") or "").strip()
    user = str(body.get("user", "") or "").strip()
    port = body.get("port", 22)
    key_path = (body.get("key_path") or "").strip() or None
    directory = str(body.get("directory", "") or "").strip() or "~"

    if host.startswith("ssh://"):
        host = host[len("ssh://"):].strip()
    if "@" in host and not user:
        parts = host.split("@", 1)
        if parts[0].strip() and parts[1].strip():
            user, host = parts[0].strip(), parts[1].strip()
    try:
        port = int(port)
    except Exception:
        port = 22
    if not host or not user:
        return JSONResponse({"ok": False, "error": "host and user are required"}, status_code=400)

    try:
        from backend import SSHBackend

        def _do_list():
            backend = SSHBackend(
                host=host,
                working_directory=directory,
                user=user,
                key_path=key_path,
                port=port,
            )
            try:
                # Resolve absolute path (e.g. expand ~) using ${1/#\~/$HOME} so it works when dir doesn't exist
                stdout, stderr, rc = backend._exec(
                    "bash -c 'echo \"${1/#\\~/$HOME}\"' _ " + shlex.quote(backend.working_directory),
                    timeout=10,
                )
                resolved = stdout.strip() if rc == 0 and stdout.strip() else directory
                entries = backend.list_dir(".")
                parent = None
                if resolved and resolved != "/":
                    parent = posixpath.dirname(resolved)
                    if parent == resolved:
                        parent = None
                return {"path": resolved, "parent": parent, "entries": entries}
            finally:
                backend.close()

        result = await asyncio.to_thread(_do_list)
        return {"ok": True, **result}
    except Exception as e:
        logger.exception("SSH list-dir failed")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/ssh-connect")
async def ssh_connect(request: Request):
    """Connect to a remote host via SSH at runtime."""
    global _backend, _working_directory, _ssh_info, _user_opened_project
    body = await request.json()
    host = str(body.get("host", "") or "").strip()
    user = str(body.get("user", "") or "").strip()
    port = body.get("port", 22)
    key_path = body.get("key_path", "").strip() or None
    directory = str(body.get("directory", "") or "").strip()

    # Normalize lenient host input from recents/manual entry:
    # - ssh://host
    # - user@host when user is omitted
    # - host:port when port is omitted
    if host.startswith("ssh://"):
        host = host[len("ssh://"):].strip()
    if "@" in host and not user:
        maybe_user, maybe_host = host.split("@", 1)
        if maybe_user and maybe_host:
            user = maybe_user.strip()
            host = maybe_host.strip()
    if ":" in host and host.count(":") == 1:
        maybe_host, maybe_port = host.rsplit(":", 1)
        if maybe_host and maybe_port.isdigit():
            host = maybe_host.strip()
            if not body.get("port"):
                port = int(maybe_port)
    host = host.strip("[] ").strip()

    if not host or not user or not directory:
        return JSONResponse({"ok": False, "error": "host, user, and directory are required"}, status_code=400)
    try:
        port = int(port)
    except Exception:
        port = 22

    try:
        from backend import SSHBackend

        def _do_connect():
            return SSHBackend(
                host=host,
                working_directory=directory,
                user=user,
                key_path=key_path,
                port=port,
            )

        _backend = await asyncio.to_thread(_do_connect)
        # Resolve directory to absolute path so terminal cd .. / cd back works
        try:
            out, err, rc = await asyncio.to_thread(_backend.run_command, "pwd", ".", 10)
            if rc == 0 and out and out.strip():
                directory = out.strip()
                _backend._working_directory = directory
        except Exception:
            pass
        # Composite working directory: user@host:port:directory — unique per SSH target
        _working_directory = f"{user}@{host}:{port}:{directory}"
        _ssh_info = {
            "host": host,
            "user": user,
            "port": port,
            "key_path": key_path or "",
            "directory": directory,
        }
        display = f"{user}@{host}:{directory}"
        logger.info(f"SSH connected to {display}")
        _user_opened_project = True
        return {"ok": True, "path": _working_directory}
    except Exception as e:
        logger.error(f"SSH connection failed: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/set-directory")
async def set_directory(body: dict):
    """Change the working directory at runtime."""
    global _working_directory, _backend, _ssh_info, _user_opened_project
    raw = body.get("path", "").strip()
    if not raw:
        return JSONResponse({"ok": False, "error": "No path provided"}, status_code=400)

    expanded = os.path.expanduser(raw)
    resolved = os.path.abspath(expanded)

    if not os.path.isdir(resolved):
        return JSONResponse({"ok": False, "error": f"Directory not found: {resolved}"})

    _working_directory = resolved
    _backend = LocalBackend(resolved)
    _ssh_info = None  # Clear SSH info when switching to local
    _user_opened_project = True
    return {"ok": True, "path": resolved}


@app.get("/api/terminal-cwd")
async def terminal_cwd():
    """Return current working directory for the integrated terminal (project root)."""
    if _backend is None:
        return {"ok": False, "cwd": None}
    return {"ok": True, "cwd": _backend.working_directory}


def _terminal_cwd_ok(backend, requested_cwd: str) -> Tuple[bool, str]:
    """Validate requested cwd is project root, a subdir, or an ancestor (so cd .. works).
    Returns (ok, resolved_cwd)."""
    root = backend.working_directory
    if not requested_cwd or requested_cwd == ".":
        return True, root
    # SSH backend: allow root, subdirs, or ancestors of project root
    if getattr(backend, "_host", None) is not None:
        root_norm = root.rstrip("/")
        req_norm = requested_cwd.rstrip("/")
        if req_norm == root_norm or req_norm.startswith(root_norm + "/"):
            return True, requested_cwd
        if root_norm.startswith(req_norm + "/"):
            return True, requested_cwd
        return False, root
    # Local: allow under project root or ancestor of project root
    try:
        req_abs = os.path.abspath(os.path.join(root, requested_cwd)) if not os.path.isabs(requested_cwd) else requested_cwd
        req_real = os.path.realpath(req_abs)
        root_real = os.path.realpath(root)
        sep = os.sep
        root_prefix = root_real.rstrip(sep) + sep
        req_prefix = req_real.rstrip(sep) + sep
        if req_real == root_real or req_real.startswith(root_prefix):
            return True, req_real
        if root_real.startswith(req_prefix) or root_real == req_real:
            return True, req_real
    except Exception:
        pass
    return False, root


@app.post("/api/terminal-run")
async def terminal_run(request: Request):
    """Run a shell command in the given cwd (default project root). Returns stdout, stderr, returncode, cwd."""
    global _backend
    if _backend is None:
        return JSONResponse({"ok": False, "error": "No project open. Open a local folder or connect via SSH first."}, status_code=400)
    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Invalid request: {e!s}"}, status_code=400)
    command = (body.get("command") or "").strip()
    if not command:
        return JSONResponse({"ok": False, "error": "No command"}, status_code=400)
    requested_cwd = (body.get("cwd") or "").strip() or "."
    ok, cwd = _terminal_cwd_ok(_backend, requested_cwd)
    if not ok:
        return JSONResponse({"ok": False, "error": "Directory not under project root"}, status_code=400)
    timeout = min(int(body.get("timeout", 60)), 300)
    try:
        stdout, stderr, returncode = await asyncio.to_thread(
            _backend.run_command, command, cwd, timeout
        )
        return {
            "ok": True,
            "stdout": stdout or "",
            "stderr": stderr or "",
            "returncode": returncode,
            "cwd": cwd,
        }
    except Exception as e:
        err_msg = str(e).strip() or "Command failed"
        logger.exception("Terminal run failed")
        return JSONResponse({"ok": False, "error": err_msg}, status_code=500)


@app.post("/api/terminal-complete")
async def terminal_complete(request: Request):
    """Return tab-completion candidates for the terminal. Body: prefix, cwd, type ('path'|'command')."""
    global _backend
    if _backend is None:
        return JSONResponse({"ok": False, "error": "No project open."}, status_code=400)
    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    prefix = (body.get("prefix") or "").strip()
    complete_type = (body.get("type") or "path").strip().lower() or "path"
    if complete_type not in ("path", "command"):
        complete_type = "path"
    requested_cwd = (body.get("cwd") or "").strip() or "."
    ok, cwd = _terminal_cwd_ok(_backend, requested_cwd)
    if not ok:
        return JSONResponse({"ok": False, "error": "Directory not under project root"}, status_code=400)

    try:
        if complete_type == "command":
            out, err, rc = await asyncio.to_thread(
                _backend.run_command, "bash -c 'compgen -c'", cwd, 5
            )
            candidates = [line for line in (out or "").splitlines() if line.strip()]
            if prefix:
                prefix_lower = prefix.lower()
                candidates = [c for c in candidates if c.lower().startswith(prefix_lower)]
            candidates = sorted(set(candidates))[:100]
            return {"ok": True, "completions": candidates, "prefix": prefix}
        else:
            # Path completion: list directory (cwd or parent of prefix), filter by prefix
            if "/" in prefix:
                dir_part = prefix.rsplit("/", 1)[0]
                filter_prefix = prefix.rsplit("/", 1)[1]
                list_path = posixpath.normpath(cwd.rstrip("/") + "/" + dir_part) if cwd else dir_part
            else:
                list_path = cwd
                filter_prefix = prefix
            try:
                entries = await asyncio.to_thread(_backend.list_dir, list_path)
            except Exception:
                entries = []
            completions = []
            for e in entries:
                name = e.get("name") or ""
                if not name or (filter_prefix and not name.startswith(filter_prefix)):
                    continue
                if e.get("type") == "directory":
                    completions.append(name + "/")
                else:
                    completions.append(name)
            completions = sorted(completions)[:100]
            return {"ok": True, "completions": completions, "prefix": prefix}
    except Exception as e:
        logger.exception("Terminal complete failed")
        return JSONResponse({"ok": False, "error": str(e).strip() or "Completion failed"}, status_code=500)


# ------------------------------------------------------------------
# Full terminal (PTY) WebSocket — local backend only
# ------------------------------------------------------------------

def _pty_shell(cwd: str) -> None:
    """Run in the child after pty.fork(): stdio is already the slave; chdir and exec shell."""
    os.setsid()
    os.chdir(cwd)
    shell = os.environ.get("SHELL", "/bin/bash")
    if not os.path.exists(shell):
        shell = "/bin/bash"
    os.execlp(shell, os.path.basename(shell), "-l")


def _pty_read_loop(master_fd: int, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop) -> None:
    """Thread: read from PTY master and put bytes into asyncio queue."""
    try:
        while True:
            try:
                data = os.read(master_fd, 4096)
            except (OSError, AttributeError):
                break
            if not data:
                break
            loop.call_soon_threadsafe(queue.put_nowait, data)
    except Exception:
        pass
    try:
        loop.call_soon_threadsafe(queue.put_nowait, None)
    except Exception:
        pass


def _ssh_channel_read_loop(channel: Any, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop) -> None:
    """Thread: read from paramiko Channel and put bytes into asyncio queue."""
    try:
        while channel.active:
            try:
                data = channel.recv(4096)
            except Exception:
                break
            if not data:
                break
            loop.call_soon_threadsafe(queue.put_nowait, data)
    except Exception:
        pass
    try:
        loop.call_soon_threadsafe(queue.put_nowait, None)
    except Exception:
        pass


@app.websocket("/ws/terminal")
async def websocket_terminal(ws: WebSocket):
    """Full PTY terminal. Local: pty.fork(); SSH: invoke_shell. Binary = I/O; text JSON = resize [rows, cols]."""
    global _backend, _working_directory
    await ws.accept()

    async def _send_error_and_close(message: str) -> None:
        await ws.send_json({"type": "error", "message": message})
        await asyncio.sleep(0.05)
        await ws.close()

    if _backend is None:
        logger.warning("terminal ws: rejected (no project open)")
        await _send_error_and_close("No project open. Open a project first.")
        return

    is_local = isinstance(_backend, LocalBackend)
    is_ssh = isinstance(_backend, SSHBackend)
    if not is_local and not is_ssh:
        logger.warning("terminal ws: rejected (backend type %s)", type(_backend).__name__)
        await _send_error_and_close("Full terminal is not available for this backend.")
        return

    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_event_loop()
    master_fd: Optional[int] = None
    pid: Optional[int] = None
    ssh_channel: Optional[Any] = None

    if is_local:
        cwd = os.path.abspath(os.path.expanduser(str(_working_directory)))
        if not os.path.isdir(cwd):
            logger.warning("terminal ws: local project dir not found: %s", cwd)
            await _send_error_and_close(f"Project directory not found: {cwd}")
            return
        pty_available = False
        try:
            import pty
            pty_available = True
        except ImportError:
            pass
        if not pty_available or sys.platform == "win32":
            logger.warning("terminal ws: PTY not available (platform=%s)", sys.platform)
            await _send_error_and_close("PTY not available on this system (required for local terminal).")
            return
        import pty
        try:
            pid, master_fd = pty.fork()
        except OSError as e:
            logger.exception("terminal ws: pty.fork failed")
            await _send_error_and_close(f"Terminal failed to start: {e!s}")
            return
        if pid == 0:
            try:
                _pty_shell(cwd)
            except Exception:
                os._exit(1)
            os._exit(0)
        reader_thread = threading.Thread(
            target=_pty_read_loop,
            args=(master_fd, queue, loop),
            daemon=True,
        )
        reader_thread.start()
    else:
        # SSH: open interactive shell with PTY
        backend = _backend
        cwd = (backend.working_directory or "").rstrip("/") or "/"
        with backend._lock:
            backend._reconnect_if_needed()
            try:
                ssh_channel = backend._client.invoke_shell(term="xterm", width=80, height=24)
            except Exception as e:
                await _send_error_and_close(f"SSH shell failed: {e!s}")
                return
        ssh_channel.settimeout(0.5)
        # Start in project directory
        if cwd and cwd != "~":
            try:
                ssh_channel.send(f"cd {shlex.quote(cwd)}\r\n")
            except Exception:
                pass
        reader_thread = threading.Thread(
            target=_ssh_channel_read_loop,
            args=(ssh_channel, queue, loop),
            daemon=True,
        )
        reader_thread.start()

    async def send_output():
        """Batch PTY output: drain queue for a short window and send one combined message to reduce WebSocket chatter."""
        batch: list = []
        batch_size = 0
        max_batch_bytes = 8192
        flush_interval = 0.008  # 8ms

        async def flush():
            nonlocal batch, batch_size
            if not batch:
                return
            try:
                await ws.send_bytes(b"".join(batch))
            except Exception:
                pass
            batch = []
            batch_size = 0

        while True:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=flush_interval)
            except asyncio.TimeoutError:
                await flush()
                continue
            if data is None:
                await flush()
                break
            batch.append(data)
            batch_size += len(data)
            while batch_size >= max_batch_bytes:
                await flush()
            # Drain any more available without waiting
            while True:
                try:
                    data = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if data is None:
                    await flush()
                    return
                batch.append(data)
                batch_size += len(data)
                if batch_size >= max_batch_bytes:
                    await flush()
                    break
        await flush()

    send_task = asyncio.create_task(send_output())

    def do_resize(rows: int, cols: int) -> None:
        if rows <= 0 or cols <= 0:
            return
        if is_local and master_fd is not None:
            try:
                if hasattr(termios, "tcsetwinsize"):
                    termios.tcsetwinsize(master_fd, (rows, cols))
                else:
                    import fcntl
                    buf = struct.pack("HHHH", rows, cols, 0, 0)
                    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, buf)
            except (OSError, AttributeError, NameError):
                pass
        elif is_ssh and ssh_channel is not None:
            try:
                ssh_channel.resize_pty(width=cols, height=rows)
            except Exception:
                pass

    try:
        logger.info("terminal ws: connected (backend=%s)", "local" if is_local else "ssh")
        await ws.send_json({"type": "ready"})
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=3600.0)
            except asyncio.TimeoutError:
                continue
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("type") == "websocket.receive":
                text = msg.get("text")
                data = msg.get("bytes")
                if text is not None:
                    logger.info("terminal ws: received text len=%s (first 50 repr=%s)", len(text), repr(text[:50]))
                    try:
                        obj = json.loads(text)
                        if not isinstance(obj, dict):
                            logger.warning(
                                "terminal ws: received JSON text that is not a dict (type=%s, repr=%s, raw_len=%s)",
                                type(obj).__name__, repr(obj)[:200], len(text),
                            )
                        elif isinstance(obj.get("resize"), (list, tuple)) and len(obj["resize"]) >= 2:
                            rows, cols = int(obj["resize"][0]), int(obj["resize"][1])
                            logger.info("terminal ws: resize rows=%s cols=%s", rows, cols)
                            do_resize(rows, cols)
                            continue
                    except (json.JSONDecodeError, ValueError, TypeError) as e:
                        logger.debug("terminal ws: JSON parse failed for text (len=%s): %s", len(text), e)
                    try:
                        if is_local and master_fd is not None:
                            os.write(master_fd, text.encode("utf-8"))
                        elif is_ssh and ssh_channel is not None:
                            ssh_channel.send(text)
                    except (OSError, BrokenPipeError, Exception):
                        break
                elif data is not None:
                    logger.debug("terminal ws: received binary payload len=%s", len(data))
                    try:
                        if is_local and master_fd is not None:
                            os.write(master_fd, data)
                        elif is_ssh and ssh_channel is not None:
                            ssh_channel.send(data)
                    except (OSError, BrokenPipeError, Exception):
                        break
    except WebSocketDisconnect:
        logger.debug("terminal ws: client disconnected")
    finally:
        logger.info("terminal ws: closing")
        send_task.cancel()
        try:
            send_task.exception()
        except (asyncio.CancelledError, Exception):
            pass
        if is_local and master_fd is not None:
            try:
                os.close(master_fd)
            except OSError:
                pass
            if pid is not None:
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass
                try:
                    os.waitpid(pid, 0)
                except OSError:
                    pass
        if is_ssh and ssh_channel is not None:
            try:
                ssh_channel.close()
            except Exception:
                pass


# ------------------------------------------------------------------
# File tree
# ------------------------------------------------------------------

def _build_file_tree(root: str, rel: str = "") -> List[Dict[str, Any]]:
    """Recursively build a file tree, skipping ignored dirs/files. Uses shared gitignore helper."""
    from tools import _load_gitignore, _is_ignored
    abs_dir = os.path.join(root, rel) if rel else root
    gi = _load_gitignore(root)
    entries: List[Dict[str, Any]] = []

    try:
        items = sorted(os.listdir(abs_dir))
    except PermissionError:
        return entries

    dirs_list = []
    files_list = []

    for name in items:
        full = os.path.join(abs_dir, name)
        child_rel = os.path.join(rel, name) if rel else name
        is_dir = os.path.isdir(full)

        if _is_ignored(child_rel, name, is_dir, gi):
            continue
        if name in _IGNORE_DIRS:
            continue

        if is_dir:
            dirs_list.append({
                "name": name,
                "path": child_rel,
                "type": "directory",
                "children": None,
            })
        elif os.path.isfile(full):
            _, ext = os.path.splitext(name)
            if ext in _IGNORE_EXTENSIONS:
                continue
            files_list.append({
                "name": name,
                "path": child_rel,
                "type": "file",
                "ext": ext.lstrip("."),
            })

    return dirs_list + files_list


@app.get("/api/files")
async def list_files(path: str = "", recursive: bool = False):
    """Return file tree entries for a directory.

    - Default (recursive=false): lazy — one level at a time.
    - recursive=true: flat list of ALL files (for fuzzy file search in explorer).
    """
    import posixpath
    b = _backend or LocalBackend(os.path.abspath(_working_directory))
    is_ssh = hasattr(b, '_client')  # SSHBackend has _client attr

    if recursive:
        return await asyncio.to_thread(_list_all_files_recursive, os.path.abspath(_working_directory), is_ssh, backend=b)

    try:
        entries = await asyncio.to_thread(b.list_dir, path or ".")
        result = []
        for e in entries:
            name = e["name"]
            if name.startswith(".") and name in _IGNORE_DIRS:
                continue
            if name in _IGNORE_DIRS:
                continue
            if is_ssh:
                child_rel = posixpath.join(path, name) if path else name
            else:
                child_rel = (path + "/" + name) if path else name
            if e["type"] == "directory":
                result.append({"name": name, "path": child_rel, "type": "directory", "children": None})
            else:
                ext = e.get("ext", "")
                if f".{ext}" in _IGNORE_EXTENSIONS:
                    continue
                result.append({"name": name, "path": child_rel, "type": "file", "ext": ext})
        dirs = [x for x in result if x["type"] == "directory"]
        files = [x for x in result if x["type"] == "file"]
        return dirs + files
    except Exception as ex:
        logger.error(f"list_files error for path={path!r}: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


def _list_all_files_recursive(root: str, is_ssh: bool = False, backend: Optional[Backend] = None) -> List[Dict[str, Any]]:
    """Collect all files recursively, respecting _IGNORE_DIRS and _IGNORE_EXTENSIONS.

    Works with both local and SSH backends. Uses BFS via Backend.list_dir() for SSH,
    os.walk() for local (faster).

    Returns a flat list of {name, path, type, ext, dir} for fuzzy file search.
    Capped at 10,000 files to protect against huge repos.
    """
    from tools import _load_gitignore, _is_ignored, _ALWAYS_SKIP_DIRS, _ALWAYS_SKIP_EXTENSIONS

    b = backend
    wd = b.working_directory if (b and hasattr(b, 'working_directory')) else root
    gi = _load_gitignore(wd, backend=b)
    result: List[Dict[str, Any]] = []
    cap = 10_000

    if b is not None and getattr(b, "_host", None) is not None:
        # SSH: BFS via Backend.list_dir()
        queue = [""]
        while queue and len(result) < cap:
            rel_dir = queue.pop(0)
            target = rel_dir if rel_dir else "."
            try:
                entries = b.list_dir(target)
            except Exception:
                continue
            for e in sorted(entries, key=lambda x: x.get("name", "")):
                if len(result) >= cap:
                    break
                name = e.get("name", "")
                if not name or name.startswith("."):
                    continue
                is_dir = e.get("type") == "directory"
                child_rel = (rel_dir + "/" + name) if rel_dir else name

                if name in _IGNORE_DIRS or name in _ALWAYS_SKIP_DIRS:
                    continue
                if _is_ignored(child_rel, name, is_dir, gi):
                    continue

                if is_dir:
                    queue.append(child_rel)
                else:
                    _, ext = os.path.splitext(name)
                    if ext in _IGNORE_EXTENSIONS or ext in _ALWAYS_SKIP_EXTENSIONS:
                        continue
                    result.append({
                        "name": name,
                        "path": child_rel,
                        "type": "file",
                        "ext": ext.lstrip("."),
                        "dir": rel_dir if rel_dir else "",
                    })
    else:
        # Local: use os.walk (faster)
        for dirpath, dirnames, filenames in os.walk(root, topdown=True):
            rel_dir = os.path.relpath(dirpath, root)
            if rel_dir == ".":
                rel_dir = ""

            dirnames[:] = [
                d for d in dirnames
                if d not in _IGNORE_DIRS and d not in _ALWAYS_SKIP_DIRS
                and not _is_ignored(os.path.join(rel_dir, d) if rel_dir else d, d, True, gi)
            ]

            for fname in sorted(filenames):
                if len(result) >= cap:
                    return result
                _, ext = os.path.splitext(fname)
                if ext in _IGNORE_EXTENSIONS or ext in _ALWAYS_SKIP_EXTENSIONS:
                    continue
                child_rel = os.path.join(rel_dir, fname) if rel_dir else fname
                child_rel = child_rel.replace("\\", "/")
                if gi and gi.match_file(child_rel):
                    continue
                result.append({
                    "name": fname,
                    "path": child_rel,
                    "type": "file",
                    "ext": ext.lstrip("."),
                    "dir": rel_dir.replace("\\", "/") if rel_dir else "",
                })

    return result


# ------------------------------------------------------------------
# File read / write
# ------------------------------------------------------------------

def _safe_path(wd: str, rel: str) -> Optional[str]:
    """Resolve a relative path and ensure it stays inside the working directory."""
    if not rel:
        return wd
    resolved = os.path.normpath(os.path.join(wd, rel))
    if not resolved.startswith(wd):
        return None
    return resolved


def _normalize_user_images(raw_images: Any) -> List[Dict[str, Any]]:
    """Validate image attachments and convert them into Anthropic image blocks."""
    if not raw_images:
        return []
    if not isinstance(raw_images, list):
        raise ValueError("images must be a list")
    if len(raw_images) > _MAX_IMAGE_ATTACHMENTS:
        raise ValueError(f"Too many images (max {_MAX_IMAGE_ATTACHMENTS})")

    blocks: List[Dict[str, Any]] = []
    total_bytes = 0
    for idx, item in enumerate(raw_images, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Invalid image payload at index {idx}")

        media_type = str(item.get("media_type", "")).strip().lower()
        if media_type == "image/jpg":
            media_type = "image/jpeg"
        if media_type not in _ALLOWED_IMAGE_MEDIA_TYPES:
            raise ValueError(f"Unsupported image media type: {media_type or 'unknown'}")

        data_b64 = str(item.get("data", "")).strip()
        if not data_b64:
            raise ValueError(f"Missing image data at index {idx}")

        # Accept data URLs and plain base64; keep only the payload.
        if data_b64.startswith("data:"):
            comma = data_b64.find(",")
            if comma == -1:
                raise ValueError(f"Invalid data URL for image {idx}")
            data_b64 = data_b64[comma + 1:].strip()

        try:
            raw = base64.b64decode(data_b64, validate=True)
        except Exception:
            raise ValueError(f"Invalid base64 payload for image {idx}")

        size = len(raw)
        if size <= 0:
            raise ValueError(f"Empty image payload at index {idx}")
        if size > _MAX_IMAGE_BYTES:
            raise ValueError(f"Image {idx} exceeds {_MAX_IMAGE_BYTES // (1024 * 1024)}MB limit")

        total_bytes += size
        if total_bytes > _MAX_IMAGE_TOTAL_BYTES:
            raise ValueError("Total image payload exceeds size limit")

        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.b64encode(raw).decode("ascii"),
            },
        })
    return blocks


@app.get("/api/find-symbol")
async def api_find_symbol(symbol: str = Query(...), kind: str = Query("definition")):
    """Find symbol definitions for Go to Definition in the editor."""
    from tools import find_symbol as _find_symbol
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                _find_symbol, symbol, kind=kind, working_directory=_working_directory
            ),
            timeout=10.0,
        )
        if not result.success:
            return JSONResponse({"results": [], "error": result.error})
        locations = []
        for line in (result.output or "").split("\n"):
            line = line.strip()
            if not line or line.startswith("Found") or line.startswith("No "):
                continue
            if ":" in line:
                parts = line.split(":", 2)
                if len(parts) >= 2:
                    fpath = parts[0].strip()
                    try:
                        linenum = int(parts[1].strip())
                    except ValueError:
                        linenum = 1
                    text = parts[2].strip() if len(parts) > 2 else ""
                    locations.append({"path": fpath, "line": linenum, "text": text})
        return {"results": locations}
    except Exception as e:
        return JSONResponse({"results": [], "error": str(e)}, status_code=500)


@app.get("/api/file")
async def read_file(path: str = Query(...)):
    """Return the contents of a file as plain text."""
    path = (path or "").strip().replace("\\", "/")
    if not path or path.endswith("/") or ".." in path or path.startswith("/"):
        return JSONResponse({"error": "Invalid path or directory"}, status_code=400)
    b = _backend or LocalBackend(os.path.abspath(_working_directory))
    try:
        if await asyncio.to_thread(b.is_dir, path):
            return JSONResponse({"error": "Cannot read a directory"}, status_code=400)
        content = await asyncio.to_thread(b.read_file, path)
        if len(content) > _MAX_FILE_SIZE:
            return JSONResponse({"error": f"File too large"}, status_code=413)
        return PlainTextResponse(content)
    except FileNotFoundError:
        return JSONResponse({"error": "File not found"}, status_code=404)
    except OSError as e:
        if getattr(e, "errno", None) == errno.ENOENT:
            return JSONResponse({"error": "File not found"}, status_code=404)
        logger.error(f"read_file error for path={path!r}: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
    except Exception as e:
        logger.error(f"read_file error for path={path!r}: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.put("/api/file")
async def write_file(request: Request):
    """Save file content from the editor."""
    b = _backend or LocalBackend(os.path.abspath(_working_directory))
    body = await request.json()
    rel_path = (body.get("path", "") or "").strip().replace("\\", "/")
    content = body.get("content", "")

    if not rel_path or ".." in rel_path or rel_path.startswith("/"):
        return JSONResponse({"ok": False, "error": "Invalid path"}, status_code=400)
    try:
        await asyncio.to_thread(b.write_file, rel_path, content)
        return {"ok": True, "path": rel_path}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ------------------------------------------------------------------
# File operations: delete, rename, mkdir
# ------------------------------------------------------------------

def _validate_rel_path(p: str) -> Optional[str]:
    """Normalise and validate a relative path. Returns cleaned path or None if invalid."""
    p = (p or "").strip().replace("\\", "/")
    if not p or ".." in p or p.startswith("/"):
        return None
    return p


@app.post("/api/file/delete")
async def delete_file(request: Request):
    """Delete a file or directory (recursively). Works over SSH."""
    body = await request.json()
    rel_path = _validate_rel_path(body.get("path", ""))
    if not rel_path:
        return JSONResponse({"ok": False, "error": "Invalid path"}, status_code=400)
    b = _backend or LocalBackend(os.path.abspath(_working_directory))
    try:
        is_dir = await asyncio.to_thread(b.is_dir, rel_path)
        exists = is_dir or await asyncio.to_thread(b.file_exists, rel_path)
        if not exists:
            return JSONResponse({"ok": False, "error": "Path not found"}, status_code=404)
        if is_dir:
            import shlex
            stdout, stderr, rc = await asyncio.to_thread(
                b.run_command, f"rm -rf {shlex.quote(rel_path)}", cwd=".", timeout=30
            )
            if rc != 0:
                return JSONResponse({"ok": False, "error": stderr or "rm failed"}, status_code=500)
        else:
            await asyncio.to_thread(b.remove_file, rel_path)
        return {"ok": True, "path": rel_path}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/file/rename")
async def rename_file(request: Request):
    """Rename (move) a file or directory. Works over SSH."""
    body = await request.json()
    old_path = _validate_rel_path(body.get("old_path", ""))
    new_path = _validate_rel_path(body.get("new_path", ""))
    if not old_path or not new_path:
        return JSONResponse({"ok": False, "error": "Invalid path"}, status_code=400)
    b = _backend or LocalBackend(os.path.abspath(_working_directory))
    try:
        src_exists = await asyncio.to_thread(b.file_exists, old_path)
        if not src_exists:
            src_exists = await asyncio.to_thread(b.is_dir, old_path)
        if not src_exists:
            return JSONResponse({"ok": False, "error": "Source not found"}, status_code=404)
        dst_exists = await asyncio.to_thread(b.file_exists, new_path)
        if not dst_exists:
            dst_exists = await asyncio.to_thread(b.is_dir, new_path)
        if dst_exists:
            return JSONResponse({"ok": False, "error": "Destination already exists"}, status_code=409)
        import shlex, posixpath
        parent = posixpath.dirname(new_path) if "/" in new_path else ""
        if parent:
            await asyncio.to_thread(
                b.run_command, f"mkdir -p {shlex.quote(parent)}", cwd=".", timeout=15
            )
        stdout, stderr, rc = await asyncio.to_thread(
            b.run_command,
            f"mv {shlex.quote(old_path)} {shlex.quote(new_path)}",
            cwd=".", timeout=15,
        )
        if rc != 0:
            return JSONResponse({"ok": False, "error": stderr or "mv failed"}, status_code=500)
        return {"ok": True, "old_path": old_path, "new_path": new_path}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/file/mkdir")
async def mkdir_file(request: Request):
    """Create a new directory (including intermediate parents). Works over SSH."""
    body = await request.json()
    rel_path = _validate_rel_path(body.get("path", ""))
    if not rel_path:
        return JSONResponse({"ok": False, "error": "Invalid path"}, status_code=400)
    b = _backend or LocalBackend(os.path.abspath(_working_directory))
    try:
        import shlex
        stdout, stderr, rc = await asyncio.to_thread(
            b.run_command, f"mkdir -p {shlex.quote(rel_path)}", cwd=".", timeout=15
        )
        if rc != 0:
            return JSONResponse({"ok": False, "error": stderr or "mkdir failed"}, status_code=500)
        return {"ok": True, "path": rel_path}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ------------------------------------------------------------------
# Git status and diff (for explorer badges and inline diffs)
# ------------------------------------------------------------------

def _parse_git_status_porcelain(stdout: str) -> Dict[str, str]:
    """Parse 'git status --porcelain' output. Returns dict path -> 'M'|'A'|'D'|'U'.
    Paths are normalized to forward slashes. Skips directory-only entries (e.g. gradle, submodules)."""
    result = {}
    for line in (stdout or "").strip().splitlines():
        line = line.strip()
        if len(line) < 3:
            continue
        # First two chars: index and work tree. Then path starts after any spaces (robust: use lstrip so we never drop 's' from "src")
        idx, wt = line[0], line[1]
        path = line[2:].lstrip()
        # Strip double quotes (git uses these for paths with spaces)
        if path.startswith('"') and path.endswith('"') and len(path) >= 2:
            path = path[1:-1].replace('\\"', '"')
        # Handle rename: "R  from -> to"
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
            if path.startswith('"') and path.endswith('"') and len(path) >= 2:
                path = path[1:-1].replace('\\"', '"')
        path = path.replace("\\", "/").strip().rstrip("/")
        if not path:
            continue
        # Skip directory-only entries that git can list (e.g. submodules like gradle); avoids "can't read a directory"
        _skip_dir_names = ("gradle", "build", "node_modules", ".git")
        if path in _skip_dir_names or any(path == d or path.endswith("/" + d) for d in _skip_dir_names):
            continue
        if wt == "M" or idx == "M" or (wt == " " and idx == "M"):
            result[path] = "M"  # modified
        elif wt == "?" and idx == "?":
            result[path] = "U"  # untracked
        elif wt == "D" or idx == "D":
            result[path] = "D"  # deleted
        elif wt == "A" or idx == "A":
            result[path] = "A"  # added
        else:
            result[path] = "M"
    return result


@app.get("/api/git-status")
async def api_git_status():
    """Return git status for the project. Path -> 'M'|'A'|'D'|'U'. Empty if not a git repo or SSH."""
    global _backend
    if _backend is None:
        return {"status": {}}
    if getattr(_backend, "_host", None) is not None:
        # SSH: support multiple repos — find all .git, run git status in each, merge with workspace-relative paths
        try:
            wd = _backend.working_directory.rstrip("/")
            # 1) If workspace is inside a repo, use that repo first
            out_root, err_root, rc_root = await asyncio.to_thread(
                _backend.run_command, "git rev-parse --show-toplevel 2>/dev/null", ".", 5
            )
            repo_roots = []  # list of (repo_root_abs, repo_rel_to_wd)
            if rc_root == 0 and out_root and out_root.strip():
                repo_root = out_root.strip().rstrip("/")
                if repo_root == wd or wd.startswith(repo_root + "/"):
                    repo_roots.append((repo_root, None))  # None = workspace is inside this repo
                elif repo_root.startswith(wd + "/"):
                    repo_roots.append((repo_root, posixpath.relpath(repo_root, wd).replace("\\", "/")))
            # 2) Find all .git in subdirs (multiple repos under workspace)
            find_out, _, _ = await asyncio.to_thread(
                _backend.run_command, "find . -maxdepth 5 -type d -name .git 2>/dev/null", ".", 5
            )
            seen_rel = set()
            for line in (find_out or "").strip().splitlines():
                git_dir = line.strip().rstrip("/")
                if not git_dir or "/.git" not in git_dir and git_dir != ".git":
                    continue
                if git_dir.endswith("/.git"):
                    repo_root_rel = git_dir[:-5].lstrip("./")
                else:
                    repo_root_rel = (os.path.dirname(git_dir) if "/" in git_dir else ".").lstrip("./")
                if not repo_root_rel or repo_root_rel in seen_rel:
                    continue
                seen_rel.add(repo_root_rel)
                out_abs, _, rc_abs = await asyncio.to_thread(
                    _backend.run_command, "cd " + shlex.quote(repo_root_rel) + " && pwd", ".", 5
                )
                if rc_abs == 0 and out_abs and out_abs.strip():
                    repo_abs = out_abs.strip().rstrip("/")
                    if (repo_abs, repo_root_rel) not in [(r[0], r[1]) for r in repo_roots]:
                        repo_roots.append((repo_abs, repo_root_rel))
            # If we only have workspace-in-repo (one repo, workspace inside it), run status once
            if len(repo_roots) == 1 and repo_roots[0][1] is None:
                repo_root = repo_roots[0][0]
                out, err, rc = await asyncio.to_thread(
                    _backend.run_command, "git status --porcelain", repo_root, 10
                )
                if rc != 0:
                    return {"status": {}, "error": (err or "").strip() or f"exit {rc}"}
                status_map = _parse_git_status_porcelain(out)
                if wd != repo_root and wd.startswith(repo_root + "/"):
                    prefix = wd[len(repo_root) :].lstrip("/") + "/"
                    status_map = {path[len(prefix) :]: s for path, s in status_map.items() if path.startswith(prefix)}
                return {"status": status_map}
            # 3) No repos found: try git status from workspace (e.g. workspace is repo root)
            if not repo_roots:
                out, err, rc = await asyncio.to_thread(
                    _backend.run_command, "git status --porcelain", ".", 10
                )
                if rc == 0 and out:
                    return {"status": _parse_git_status_porcelain(out)}
                return {"status": {}, "error": (err_root or err or "").strip() or "not a git repository"}
            # 4) Multiple repos or workspace contains repos: run git status in each and merge
            merged = {}
            for repo_abs, repo_rel in repo_roots:
                out, err, rc = await asyncio.to_thread(
                    _backend.run_command, "git status --porcelain", repo_abs, 10
                )
                if rc != 0:
                    continue
                status_map = _parse_git_status_porcelain(out)
                if repo_rel is None:
                    # workspace inside this repo: strip prefix
                    if wd != repo_abs and wd.startswith(repo_abs + "/"):
                        prefix = wd[len(repo_abs) :].lstrip("/") + "/"
                        status_map = {path[len(prefix) :]: s for path, s in status_map.items() if path.startswith(prefix)}
                    else:
                        pass  # wd == repo_abs, paths already workspace-relative
                else:
                    status_map = {repo_rel + "/" + path: s for path, s in status_map.items()}
                merged.update(status_map)
            return {"status": merged}
        except Exception as e:
            logger.warning("git status SSH failed: %s", e)
            return {"status": {}, "error": str(e)}
    # Local: support multiple repos — find all .git under workspace, run git status in each, merge
    try:
        wd = os.path.realpath(_backend.working_directory.rstrip(os.sep))
        out_root, _, rc_root = await asyncio.to_thread(
            _backend.run_command, "git rev-parse --show-toplevel 2>/dev/null", ".", 5
        )
        repo_roots = []  # list of (repo_abs, repo_rel_to_wd or None if wd inside repo)
        if rc_root == 0 and out_root and out_root.strip():
            repo_abs = os.path.realpath(out_root.strip().rstrip(os.sep))
            if wd == repo_abs or (wd + os.sep).startswith(repo_abs.rstrip(os.sep) + os.sep):
                repo_roots.append((repo_abs, None))
            elif (repo_abs + os.sep).startswith(wd.rstrip(os.sep) + os.sep):
                repo_roots.append((repo_abs, os.path.relpath(repo_abs, wd).replace("\\", "/")))
        # Find all .git dirs under workspace (multiple repos)
        skip_dirs = {"node_modules", "venv", ".venv", "__pycache__", ".git"}
        for root, dirs, _ in os.walk(wd):
            dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
            if os.path.relpath(root, wd).count(os.sep) >= 4:
                dirs.clear()
                continue
            if ".git" in dirs:
                repo_abs = os.path.realpath(root)
                rel = os.path.relpath(repo_abs, wd).replace("\\", "/")
                if not any(r[0] == repo_abs for r in repo_roots):
                    repo_roots.append((repo_abs, rel if rel != "." else None))
        if not repo_roots:
            out, err, rc = await asyncio.to_thread(
                _backend.run_command, "git status --porcelain", ".", 10
            )
            if rc == 0 and out:
                return {"status": _parse_git_status_porcelain(out)}
            return {"status": {}}
        merged = {}
        for repo_abs, repo_rel in repo_roots:
            out, err, rc = await asyncio.to_thread(
                _backend.run_command, "git status --porcelain", repo_abs, 10
            )
            if rc != 0:
                continue
            status_map = _parse_git_status_porcelain(out)
            if repo_rel is None or repo_rel == ".":
                # workspace is this repo or inside it
                if wd != repo_abs and (wd + os.sep).startswith(repo_abs.rstrip(os.sep) + os.sep):
                    prefix = os.path.relpath(wd, repo_abs).replace("\\", "/") + "/"
                    status_map = {path[len(prefix):]: s for path, s in status_map.items() if path.startswith(prefix)}
            else:
                status_map = {repo_rel + "/" + path: s for path, s in status_map.items()}
            merged.update(status_map)
        return {"status": merged}
    except Exception:
        return {"status": {}}


@app.get("/api/git-file-diff")
async def api_git_file_diff(path: str = Query(...)):
    """Return original (HEAD) and current (working tree) content for a file. For inline git diffs."""
    global _backend
    if _backend is None:
        return JSONResponse({"error": "No project"}, status_code=400)
    path = (path or "").strip()
    if ".." in path or path.startswith("/"):
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    wd = _backend.working_directory
    try:
        current = await asyncio.to_thread(_backend.read_file, path)
    except FileNotFoundError:
        current = ""
    original = ""
    try:
        if getattr(_backend, "_host", None) is not None:
            # SSH: run git show; path is workspace-relative (may be in nested repo)
            cmd = "git show HEAD:" + shlex.quote(path)
            out, err, rc = await asyncio.to_thread(
                _backend.run_command, cmd, ".", 5
            )
            if rc != 0 or out is None:
                # Find which repo contains this path (multiple repos: use innermost match)
                find_out, _, _ = await asyncio.to_thread(
                    _backend.run_command, "find . -maxdepth 5 -type d -name .git 2>/dev/null", ".", 5
                )
                candidates = []  # (repo_rel, path_in_repo)
                for line in (find_out or "").strip().splitlines():
                    git_dir = line.strip().rstrip("/")
                    if not git_dir or not git_dir.endswith("/.git"):
                        continue
                    repo_rel = git_dir[:-5].lstrip("./")
                    if not repo_rel:
                        repo_rel = "."
                    if path == repo_rel or path.startswith(repo_rel + "/"):
                        path_in_repo = path[len(repo_rel) + 1:] if path.startswith(repo_rel + "/") else path
                        candidates.append((len(repo_rel), repo_rel, path_in_repo))
                # Prefer innermost repo (longest repo_rel)
                candidates.sort(key=lambda x: -x[0])
                for _, repo_rel, path_in_repo in candidates:
                    cmd2 = "git show HEAD:" + shlex.quote(path_in_repo)
                    out2, _, rc2 = await asyncio.to_thread(
                        _backend.run_command, cmd2, repo_rel if repo_rel != "." else ".", 5
                    )
                    if rc2 == 0 and out2 is not None:
                        original = out2
                        break
            elif rc == 0 and out is not None:
                original = out
        else:
            import subprocess
            r = subprocess.run(
                ["git", "show", f"HEAD:{path}"],
                cwd=wd,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if r.returncode == 0 and r.stdout is not None:
                original = r.stdout
            else:
                # Multiple repos: collect all repo roots, pick innermost that contains path
                wd_abs = os.path.realpath(wd)
                skip_dirs = {"node_modules", "venv", ".venv", "__pycache__", ".git"}
                candidates = []
                for root, dirs, _ in os.walk(wd_abs):
                    dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
                    if os.path.relpath(root, wd_abs).count(os.sep) >= 4:
                        dirs.clear()
                        continue
                    if ".git" in dirs:
                        repo_root = os.path.realpath(root)
                        rel = os.path.relpath(repo_root, wd_abs).replace("\\", "/")
                        if rel == ".":
                            rel = ""
                        if path == rel or (rel and path.startswith(rel + "/")) or (not rel and True):
                            path_in_repo = path[len(rel) + 1:] if rel and path.startswith(rel + "/") else path
                            candidates.append((len(rel or "."), repo_root, path_in_repo))
                candidates.sort(key=lambda x: -x[0])
                for _, repo_root, path_in_repo in candidates:
                    r2 = subprocess.run(
                        ["git", "show", f"HEAD:{path_in_repo}"],
                        cwd=repo_root,
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if r2.returncode == 0 and r2.stdout:
                        original = r2.stdout
                        break
    except Exception:
        pass
    return {"path": path, "original": original, "current": current}


def _parse_git_diff_numstat(stdout: str) -> List[Dict[str, Any]]:
    """Parse 'git diff --numstat' output. Returns list of {path, additions, deletions}."""
    rows = []
    for line in (stdout or "").strip().splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        add_s, del_s, path = parts[0], parts[1], parts[2].strip()
        path = path.replace("\\", "/").strip().rstrip("/")
        if not path:
            continue
        try:
            additions = int(add_s) if add_s != "-" else 0
            deletions = int(del_s) if del_s != "-" else 0
        except ValueError:
            additions = deletions = 0
        rows.append({"path": path, "additions": additions, "deletions": deletions})
    return rows


@app.get("/api/git-diff-stats")
async def api_git_diff_stats():
    """Return per-file and total diff stats (additions/deletions) for working tree vs HEAD.
    Used by the Cursor-style modified files dropdown."""
    global _backend
    if _backend is None:
        return {"files": [], "total_additions": 0, "total_deletions": 0}
    wd = _backend.working_directory
    files: List[Dict[str, Any]] = []
    total_additions = 0
    total_deletions = 0
    try:
        if getattr(_backend, "_host", None) is not None:
            out, err, rc = await asyncio.to_thread(
                _backend.run_command, "git diff --numstat", ".", 15
            )
            if rc != 0 or not out:
                # Include untracked (e.g. new files) via status + diff
                out2, _, _ = await asyncio.to_thread(
                    _backend.run_command, "git status --porcelain", ".", 10
                )
                for line in (out2 or "").strip().splitlines():
                    path = line[2:].lstrip().replace("\\", "/").strip()
                    if path and " -> " not in path:
                        files.append({"path": path, "additions": 0, "deletions": 0})
            else:
                files = _parse_git_diff_numstat(out)
        else:
            import subprocess
            r = subprocess.run(
                ["git", "diff", "--numstat"],
                cwd=wd,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if r.returncode == 0 and r.stdout:
                files = _parse_git_diff_numstat(r.stdout)
            else:
                # Fallback: list from status so we at least show modified paths
                r2 = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=wd,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if r2.returncode == 0 and r2.stdout:
                    for line in r2.stdout.strip().splitlines():
                        if len(line) >= 3:
                            path = line[2:].lstrip().split(" -> ")[-1].strip().replace("\\", "/")
                            if path:
                                files.append({"path": path, "additions": 0, "deletions": 0})
        for f in files:
            total_additions += f.get("additions", 0)
            total_deletions += f.get("deletions", 0)
    except Exception:
        pass
    return {"files": files[:100], "total_additions": total_additions, "total_deletions": total_deletions}


# ------------------------------------------------------------------
# File diff (agent snapshots)
# ------------------------------------------------------------------

def _get_effective_wd() -> str:
    """Get the effective working directory (remote dir for SSH, absolute path for local)."""
    if _ssh_info:
        return _ssh_info["directory"]
    return os.path.abspath(_working_directory)


@app.get("/api/file-diff")
async def file_diff(path: str = Query(...)):
    """Return original and current content for a file the agent modified."""
    wd = _get_effective_wd()
    safe = _safe_path(wd, path)
    if safe is None:
        return JSONResponse({"error": "Invalid path"}, status_code=400)

    agent = _active_agent
    if not agent:
        return JSONResponse({"error": "No active agent"}, status_code=404)

    snapshots = agent.modified_files
    if safe not in snapshots:
        return JSONResponse({"error": "File not modified by agent"}, status_code=404)

    snap_val = snapshots[safe]
    if snap_val is None or (isinstance(snap_val, dict) and snap_val.get("created")):
        original = ""  # file was created by agent — no prior content
    elif isinstance(snap_val, str):
        original = snap_val
    else:
        original = ""
    b = _backend or LocalBackend(wd)
    try:
        current = await asyncio.to_thread(b.read_file, safe)
    except FileNotFoundError:
        current = ""

    return {
        "path": path,
        "original": original,
        "current": current,
    }


# ------------------------------------------------------------------
# Project-wide search and replace
# ------------------------------------------------------------------

@app.get("/api/search")
async def api_search(
    pattern: str = Query(...),
    path: str = Query(""),
    include: str = Query(""),
):
    """Search for a regex pattern across the project. Returns structured results."""
    b = _backend or LocalBackend(os.path.abspath(_working_directory))
    try:
        raw = await asyncio.to_thread(b.search, pattern, path or ".", include or None, ".")
        if not raw:
            return {"results": [], "count": 0}

        results = []
        for line in raw.split("\n"):
            if not line.strip():
                continue
            # Parse ripgrep/grep output:  file:line:text
            parts = line.split(":", 2)
            if len(parts) >= 3:
                file_path = parts[0]
                try:
                    line_num = int(parts[1])
                except ValueError:
                    continue
                text = parts[2]
                # Make path relative to working directory
                wd = b.working_directory
                if file_path.startswith(wd):
                    file_path = file_path[len(wd):].lstrip(os.sep).lstrip("/")
                results.append({
                    "file": file_path,
                    "line": line_num,
                    "text": text.rstrip(),
                    "match": pattern,
                })

        return {"results": results[:500], "count": len(results)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/replace")
async def api_replace(request: Request):
    """Search-and-replace across specified files."""
    b = _backend or LocalBackend(os.path.abspath(_working_directory))
    body = await request.json()
    pattern = body.get("pattern", "")
    replacement = body.get("replacement", "")
    files = body.get("files", [])
    use_regex = body.get("regex", False)

    if not pattern or not files:
        return JSONResponse({"error": "pattern and files required"}, status_code=400)

    import re
    changed = []
    errors = []
    for file_path in files:
        try:
            content = await asyncio.to_thread(b.read_file, file_path)
            if use_regex:
                new_content, count = re.subn(pattern, replacement, content)
            else:
                count = content.count(pattern)
                new_content = content.replace(pattern, replacement)
            if count > 0:
                await asyncio.to_thread(b.write_file, file_path, new_content)
                changed.append({"file": file_path, "replacements": count})
        except Exception as e:
            errors.append({"file": file_path, "error": str(e)})

    return {"changed": changed, "errors": errors}


# ============================================================
# WebSocket handler — one agent per connection
# ============================================================


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, session_id: Optional[str] = None):
    global _active_agent
    await ws.accept()

    # ── Reconnect hand-off ──────────────────────────────────────
    # If a previous handler is waiting for a reconnect on this session,
    # hand the new WS over and keep *this* handler alive (so the WS
    # stays open) until the old handler finishes using it.
    _req_sid = (ws.query_params.get("session_id") or "").strip()
    if not _req_sid and session_id:
        _req_sid = str(session_id).strip()

    if _req_sid and _req_sid in _reconnect_sessions:
        entry = _reconnect_sessions[_req_sid]
        done_event = asyncio.Event()
        try:
            entry["future"].set_result((ws, done_event))
        except asyncio.InvalidStateError:
            pass  # future already resolved (race)
        else:
            # Park this handler — the OLD handler now owns the WS.
            try:
                await done_event.wait()
            except (asyncio.CancelledError, Exception):
                pass
            return

    # ── Normal / fresh connection ───────────────────────────────
    wsr = _WSRef(ws)

    is_ssh = _ssh_info is not None
    # For SSH projects, _working_directory is composite (user@host:port:dir)
    # For local, normalize to absolute path
    if is_ssh:
        wd = _working_directory
        # The actual filesystem directory for the agent is the remote dir
        agent_wd = _ssh_info["directory"]
    else:
        wd = os.path.abspath(_working_directory)
        agent_wd = wd

    # Initialise services
    try:
        bedrock_service = BedrockService()
        backend = _backend or LocalBackend(agent_wd)
        agent = CodingAgent(
            bedrock_service,
            working_directory=agent_wd,
            max_iterations=int(os.getenv("MAX_TOOL_ITERATIONS", "50")),
            backend=backend,
        )
        _active_agent = agent
    except Exception as e:
        await wsr.send_json({"type": "error", "content": f"Init failed: {e}"})
        await ws.close()
        return

    # Session management — wd is the composite key (unique per project + SSH target)
    requested_session_id = (ws.query_params.get("session_id") or "").strip()
    if not requested_session_id and session_id:
        requested_session_id = str(session_id).strip()

    store = SessionStore()
    session: Optional[Session] = None
    if requested_session_id:
        loaded = store.load(requested_session_id)
        if loaded and loaded.working_directory == wd:
            session = loaded
        elif loaded:
            # Session exists but is for a different workspace (e.g. different path or SSH resolved path changed)
            logger.debug("Session %s is for another workspace (wd mismatch), using latest for this workspace", requested_session_id)
        else:
            logger.debug("Session %s not found, using latest for this workspace", requested_session_id)
    if session is None:
        session = store.get_latest(wd)
    # Keys that are web.py UI state, not agent state
    _ui_state_keys = {
        "ssh_info",
        "awaiting_build",
        "awaiting_keep_revert",
        "pending_task",
        "pending_plan",
        "pending_images",
        "current_thinking_text",
        "current_text_buffer",
    }
    _restored_ui_state: Dict[str, Any] = {}
    if session is None:
        session = store.create_session(wd, model_config.model_id)
    else:
        # Restore agent/session state even for empty-history sessions.
        # Otherwise, switching to a newly created agent session would
        # incorrectly fall back to a fresh default session.
        restore_data = {
            "history": session.history or [],
            "token_usage": session.token_usage or {},
        }
        # Restore extra state (running_summary, current_plan, approved_commands, etc.)
        if session.extra_state:
            # Separate UI state from agent state
            for k, v in session.extra_state.items():
                if k in _ui_state_keys:
                    _restored_ui_state[k] = v
                else:
                    restore_data[k] = v
        agent.from_dict(restore_data)

    def save_session():
        if session and agent:
            state = agent.to_dict()
            session.history = state["history"]
            session.token_usage = state["token_usage"]
            session.extra_state = {
                "approved_commands": state.get("approved_commands", []),
                "running_summary": state.get("running_summary", ""),
                "current_plan": state.get("current_plan"),
                "plan_file_path": state.get("plan_file_path"),
                "plan_text": state.get("plan_text", ""),
                "scout_context": state.get("scout_context"),
                "file_snapshots": state.get("file_snapshots", {}),
                "plan_step_index": state.get("plan_step_index", 0),
                "todos": state.get("todos", []),
                "memory": state.get("memory", {}),
                # UI state for reconnection
                "awaiting_build": awaiting_build,
                "awaiting_keep_revert": awaiting_keep_revert,
                "pending_task": pending_task,
                "pending_plan": pending_plan,
                "pending_images": pending_images,
                # In-progress stream buffers (survive server restart)
                "current_thinking_text": _current_thinking_text,
                "current_text_buffer": _current_text_buffer,
            }
            # Persist SSH connection info so it can be reused on reopen
            if _ssh_info:
                session.extra_state["ssh_info"] = _ssh_info
            session.working_directory = wd
            try:
                store.save(session)
            except Exception as exc:
                logger.error(f"Session save failed: {exc}")

    # Register this save function globally so shutdown handler can call it
    if session and session.session_id:
        _active_save_fns[session.session_id] = save_session

    # State machine — restore from session if reconnecting
    pending_task: Optional[str] = _restored_ui_state.get("pending_task")
    pending_plan: Optional[List[str]] = _restored_ui_state.get("pending_plan")
    pending_images: List[Dict[str, Any]] = list(_restored_ui_state.get("pending_images") or [])
    awaiting_build: bool = bool(_restored_ui_state.get("awaiting_build"))
    # Only show Keep/Revert if we have pending snapshots and user hasn't already resolved (Keep/Revert).
    # If we saved awaiting_keep_revert=False (user clicked Keep/Revert), never show the bar again.
    awaiting_keep_revert: bool = (
        bool(agent._file_snapshots)
        and _restored_ui_state.get("awaiting_keep_revert") is not False
    )
    task_start: Optional[float] = None
    _last_save_time: float = time.time()
    _agent_task: Optional[asyncio.Task] = None
    _cancel_ack_sent: bool = False

    # ------------------------------------------------------------------
    # History replay — rebuild chat from persisted history
    # ------------------------------------------------------------------

    # Known internal XML tags injected by the agent or auto-context into user messages.
    _INTERNAL_XML_TAGS = [
        "codebase_context", "approved_plan", "plan_decomposition",
        "manager_worker_insights", "project_context", "current_plan",
        "updated_plan", "scout_context", "verification_context",
        # Auto-context tags from build_auto_context()
        "auto_context", "active_file", "selected_text", "modified_file",
        "dependency_context", "semantic_context", "git_diff", "project_structure",
        "linter_errors", "open_files", "recent_files",
    ]
    _STRIP_XML_RE = re.compile(
        r"<(" + "|".join(_INTERNAL_XML_TAGS) + r")>[\s\S]*?</\1>",
        re.IGNORECASE,
    )
    _INSTRUCTION_SUFFIXES = [
        "Execute this plan step by step.",
        "State which step you are working on.",
        "Work through them in order; set each to completed and the next to in_progress as you go.",
    ]

    def _strip_internal_replay_content(text: str) -> Optional[str]:
        """Strip internal agent context from a user message for replay.
        Returns the cleaned user-facing text, or None if the message
        should be completely hidden."""
        if not text or not text.strip():
            return None
        # Skip system-injected messages (any case)
        if text.upper().startswith("[SYSTEM]"):
            return None
        # Skip compressed / trimmed markers
        if "(earlier context compressed)" in text or "(earlier work trimmed)" in text:
            return None
        # Skip verification nudges
        if text.startswith("Verification pass") or text.startswith("Quick check"):
            return None
        if text.startswith("You have completed all plan steps"):
            return None
        # If the user said "User's message: " (follow-up with plan context), extract it
        if "<current_plan>" in text and "User's message: " in text:
            return text.split("User's message: ", 1)[-1].strip() or None
        # Strip all known internal XML blocks
        cleaned = _STRIP_XML_RE.sub("", text).strip()
        # Strip known instruction suffixes
        for suffix in _INSTRUCTION_SUFFIXES:
            cleaned = cleaned.replace(suffix, "").strip()
        # Remove any remaining instructional block that starts with "Before touching files"
        # or "For each step:" — these are agent instructions, not user text
        for marker in [
            "Before touching files, call TodoWrite",
            "For each step:\n",
            "If you discover something the plan missed",
        ]:
            idx = cleaned.find(marker)
            if idx >= 0:
                cleaned = cleaned[:idx].strip()
        return cleaned if cleaned else None

    async def replay_history():
        """Walk through saved history and emit replay events so the
        frontend can rebuild the full conversation on reconnect.

        Improvements over basic replay:
        - Pairs tool_call with tool_result by tracking tool_use IDs
        - Skips system-injected messages (verification nudges, summaries)
        - Filters out compressed/trimmed content markers
        """
        if not agent.history:
            return

        # Build a map of tool_use_id -> tool_result for pairing
        tool_results_map: Dict[str, Dict[str, Any]] = {}
        for msg in agent.history:
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") == "tool_result":
                    tid = block.get("tool_use_id", "")
                    if tid:
                        result_content = block.get("content", "")
                        text = ""
                        if isinstance(result_content, str):
                            text = result_content
                        elif isinstance(result_content, list):
                            text = " ".join(
                                b.get("text", "") for b in result_content
                                if b.get("type") == "text"
                            )
                        tool_results_map[tid] = {
                            "content": text[:1000],
                            "success": not block.get("is_error", False),
                        }

        emitted_tool_results: set = set()

        for msg in agent.history:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "user":
                if isinstance(content, str):
                    cleaned = _strip_internal_replay_content(content)
                    if cleaned:
                        await wsr.send_json({"type": "replay_user", "content": cleaned})
                elif isinstance(content, list):
                    image_count = 0
                    for block in content:
                        if block.get("type") == "text":
                            cleaned = _strip_internal_replay_content(block.get("text", ""))
                            if cleaned:
                                await wsr.send_json({"type": "replay_user", "content": cleaned})
                        elif block.get("type") == "image":
                            image_count += 1
                    if image_count > 0:
                        await wsr.send_json({
                            "type": "replay_user",
                            "content": f"📷 {image_count} image attachment{'s' if image_count != 1 else ''}",
                        })

            elif role == "assistant":
                if isinstance(content, str):
                    _display = _STRIP_XML_RE.sub("", content).strip()
                    if _display:
                        await wsr.send_json({"type": "replay_text", "content": _display})
                elif isinstance(content, list):
                    for block in content:
                        btype = block.get("type", "")
                        if btype == "thinking":
                            thinking_text = block.get("thinking", "")
                            if thinking_text and thinking_text != "...":
                                await wsr.send_json({"type": "replay_thinking", "content": thinking_text})
                        elif btype == "text":
                            text = _STRIP_XML_RE.sub("", block.get("text", "")).strip()
                            if text:
                                await wsr.send_json({"type": "replay_text", "content": text})
                        elif btype == "tool_use":
                            tool_id = block.get("id", "")
                            await wsr.send_json({
                                "type": "replay_tool_call",
                                "data": {
                                    "name": block.get("name", ""),
                                    "input": block.get("input", {}),
                                    "id": tool_id,
                                },
                            })
                            # Immediately pair with its result if available
                            if tool_id in tool_results_map:
                                tr = tool_results_map[tool_id]
                                await wsr.send_json({
                                    "type": "replay_tool_result",
                                    "content": tr["content"],
                                    "data": {"tool_use_id": tool_id, "success": tr["success"]},
                                })
                                emitted_tool_results.add(tool_id)

        await wsr.send_json({"type": "replay_done"})

        # Send UI state so frontend can restore interactive elements
        # (plan buttons, keep/revert bar, etc.)
        state_msg: Dict[str, Any] = {
            "type": "replay_state",
            "awaiting_build": awaiting_build,
            "awaiting_keep_revert": awaiting_keep_revert,
        }
        todos = getattr(agent, "_todos", None) or []
        # Always send todos (even if empty) to properly restore checklist state
        state_msg["todos"] = todos
        if awaiting_build and pending_plan:
            state_msg["pending_plan"] = pending_plan
            state_msg["plan_step_index"] = agent._plan_step_index
            # Restore plan file / text so "Open in Editor" and full plan doc survive reload
            plan_file = getattr(agent, "_plan_file_path", None) or None
            plan_text = getattr(agent, "_plan_text", "") or ""
            # Fallback: if session has no plan_file (e.g. old session or two plans on disk),
            # use the latest plan file from .bedrock-codex/plans/ so the button always shows
            if not plan_file and agent.backend:
                try:
                    entries = agent.backend.list_dir(".bedrock-codex/plans")
                    md_files = [e["name"] for e in (entries or []) if e.get("type") == "file" and (e.get("name") or "").endswith(".md")]
                    if md_files:
                        # Newest by name (plan-YYYYMMDD-HHMMSS-slug.md sorts chronologically)
                        latest_name = max(md_files)
                        plan_file = ".bedrock-codex/plans/" + latest_name
                        if not plan_text:
                            try:
                                plan_text = agent.backend.read_file(plan_file)
                            except Exception:
                                pass
                except Exception:
                    pass
            state_msg["plan_file"] = plan_file
            state_msg["plan_text"] = plan_text or ""
        if awaiting_keep_revert and agent._file_snapshots:
            # Generate and send actual diffs for the keep/revert bar
            diffs = generate_diffs()
            if diffs:
                state_msg["has_diffs"] = True
                state_msg["diffs"] = diffs
        await wsr.send_json(state_msg)

    # ------------------------------------------------------------------
    # Event bridge: AgentEvent → WebSocket JSON
    # ------------------------------------------------------------------

    async def on_event(event: AgentEvent):
        nonlocal awaiting_keep_revert, _last_save_time, _cancel_ack_sent
        nonlocal _current_thinking_text, _current_text_buffer
        # Skip agent's own cancelled event if we already sent one from the cancel handler
        if event.type == "cancelled" and _cancel_ack_sent:
            return

        # Track in-progress thinking/text so reconnects can resume blocks
        if event.type == "thinking_start":
            _current_thinking_text = ""
        elif event.type == "thinking":
            _current_thinking_text += (event.content or "")
        elif event.type == "thinking_end":
            _current_thinking_text = ""
        elif event.type == "text_start":
            _current_text_buffer = ""
        elif event.type == "text":
            _current_text_buffer += (event.content or "")
        elif event.type in ("text_end", "done", "error", "cancelled"):
            _current_text_buffer = ""

        msg: Dict[str, Any] = {"type": event.type}

        if event.content:
            msg["content"] = event.content
        if event.data:
            msg["data"] = event.data

        # So frontend can use evt.todos without digging into evt.data
        if event.type == "todos_updated" and event.data and "todos" in event.data:
            msg["todos"] = event.data["todos"]

        # Special handling for plan phase
        if event.type == "phase_plan":
            steps = event.data.get("steps", []) if event.data else []
            if not steps and event.content:
                steps = [l.strip() for l in event.content.strip().split("\n") if l.strip()]
            plan_file = event.data.get("plan_file") if event.data else None
            plan_text = event.data.get("plan_text", "") if event.data else ""
            msg = {
                "type": "plan",
                "steps": steps,
                "plan_file": plan_file,
                "plan_text": plan_text,
            }

        await wsr.send_json(msg)

        # ── Periodic auto-save: save after key events or every 5s during streaming ──
        now = time.time()
        should_save = False
        if event.type in ("tool_result", "thinking_end", "text_end", "done"):
            should_save = True  # natural save points
        elif event.type in ("thinking", "text") and now - _last_save_time >= 5:
            should_save = True  # save mid-stream every 5s so kills lose minimal content
        elif now - _last_save_time >= 10:
            should_save = True  # general time-based fallback
        if should_save:
            _last_save_time = now
            try:
                await asyncio.to_thread(save_session)
            except Exception:
                pass  # don't break streaming on save failure

    async def dummy_approval(tool_name: str, desc: str, data: Dict) -> bool:
        """Auto-approve everything — user reviews via keep/revert."""
        return True

    # ------------------------------------------------------------------
    # Diff generation (works for both local and SSH: SSH uses backend.read_file)
    # ------------------------------------------------------------------

    def _rel_from_working(abs_path: str, working_directory: str) -> str:
        """Return path relative to working_directory (POSIX-style for SSH)."""
        norm_wd = (working_directory or "").rstrip("/")
        norm_abs = (abs_path or "").replace("\\", "/")
        if norm_abs == norm_wd:
            return "."
        if norm_wd and norm_abs.startswith(norm_wd + "/"):
            return norm_abs[len(norm_wd) + 1:]
        return norm_abs.split("/")[-1] if "/" in norm_abs else norm_abs

    def generate_diffs() -> List[Dict[str, Any]]:
        modified = agent.modified_files
        if not modified:
            return []
        is_ssh = getattr(agent.backend, "_host", None) is not None
        diffs = []
        for abs_path, original in modified.items():
            if is_ssh:
                rel = _rel_from_working(abs_path, agent.working_directory)
                try:
                    new_content = agent.backend.read_file(abs_path)
                except Exception:
                    new_content = ""
            else:
                rel = os.path.relpath(abs_path, wd)
                try:
                    with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                        new_content = f.read()
                except FileNotFoundError:
                    new_content = ""
            new_lines = new_content.splitlines(keepends=True)
            # Resolve original content: dict means "created file", None means "new file"
            is_created = original is None or (isinstance(original, dict) and original.get("created"))
            old_text = "" if is_created else (original if isinstance(original, str) else "")
            old_lines = old_text.splitlines(keepends=True)

            diff_lines = list(difflib.unified_diff(
                old_lines, new_lines,
                fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm=""
            ))

            additions = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
            deletions = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))

            label = "new file" if is_created else "modified"

            diffs.append({
                "path": rel,
                "label": label,
                "additions": additions,
                "deletions": deletions,
                "diff": "\n".join(diff_lines),
            })

        return diffs

    # ------------------------------------------------------------------
    # Message loop
    # ------------------------------------------------------------------

    _watcher_task = None
    _save_interval_task: Optional[asyncio.Task] = None
    _pending_question: Dict[str, Any] = {"future": None, "tool_use_id": None}
    # Buffer in-progress thinking/text so they survive reconnects AND server restarts.
    # Populated by streaming events; cleared when the block ends.
    # Restored from session extra_state if the server was killed mid-stream.
    _current_thinking_text: str = _restored_ui_state.get("current_thinking_text", "") or ""
    _current_text_buffer: str = _restored_ui_state.get("current_text_buffer", "") or ""

    async def _periodic_save_loop():
        """Save session every 10s so kills/crashes lose minimal state."""
        while True:
            await asyncio.sleep(10)
            try:
                await asyncio.to_thread(save_session)
            except Exception:
                pass


    async def _message_loop():
        """Read and dispatch incoming WS messages. Extracted as a reusable
        closure so it can be re-entered after a reconnect."""
        nonlocal _cancel_ack_sent, _agent_task, awaiting_keep_revert
        nonlocal awaiting_build, pending_task, pending_plan, pending_images
        nonlocal session
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await wsr.send_json({"type": "error", "content": "Invalid JSON"})
                continue

            msg_type = data.get("type", "")

            # ── User answer to clarifying question (plan phase) ───
            if msg_type == "user_answer":
                if _pending_question["future"] and data.get("tool_use_id") == _pending_question["tool_use_id"]:
                    try:
                        _pending_question["future"].set_result(data.get("answer", ""))
                    except asyncio.InvalidStateError:
                        pass
                continue

            # ── Cancel ─────────────────────────────────────────────
            if msg_type == "cancel":
                _cancel_ack_sent = True
                agent.cancel()  # sets flag + kills running subprocess
                if _agent_task and not _agent_task.done():
                    # Give the agent task a moment to wind down gracefully
                    try:
                        await asyncio.wait_for(asyncio.shield(_agent_task), timeout=3.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                        _agent_task.cancel()
                _agent_task = None
                await wsr.send_json({"type": "cancelled"})
                # Show keep/revert bar if the agent modified files before being stopped
                if agent._file_snapshots:
                    awaiting_keep_revert = True
                    diffs = generate_diffs()
                    if diffs:
                        await wsr.send_json({"type": "diff", "files": diffs, "cumulative": True})
                continue

            # ── Keep / Revert ──────────────────────────────────────
            if msg_type == "keep" and awaiting_keep_revert:
                # Record which files were kept so the agent knows its changes were accepted
                kept_paths = [os.path.relpath(p, wd) for p in agent.modified_files]
                agent.clear_snapshots()
                awaiting_keep_revert = False
                feedback = "[System] The user accepted your changes."
                if kept_paths:
                    feedback += f" The following files were kept: {', '.join(kept_paths[:20])}"
                    if len(kept_paths) > 20:
                        feedback += f" (and {len(kept_paths) - 20} more)"
                agent.history.append({"role": "user", "content": feedback})
                save_session()
                await wsr.send_json({"type": "kept"})
                continue

            if msg_type == "revert" and awaiting_keep_revert:
                reverted = agent.revert_all()
                reverted_rel = [os.path.relpath(p, wd) for p in reverted]
                awaiting_keep_revert = False
                feedback = "[System] The user reverted your changes. The following files were reverted to their previous state: "
                feedback += ", ".join(reverted_rel[:20]) if reverted_rel else "(none)"
                if len(reverted_rel) > 20:
                    feedback += f" (and {len(reverted_rel) - 20} more)"
                agent.history.append({"role": "user", "content": feedback})
                save_session()
                await wsr.send_json({
                    "type": "reverted",
                    "files": reverted_rel,
                })
                continue

            # ── Revert to specific plan step ──────────────────────
            if msg_type == "revert_to_step":
                try:
                    step = int(data.get("step", 0) or 0)
                except (TypeError, ValueError):
                    step = 0
                if step < 1:
                    await wsr.send_json({
                        "type": "error",
                        "content": "Invalid step for revert (must be at least 1).",
                    })
                    continue
                had_checkpoint = step in getattr(agent, "_step_checkpoints", {})
                reverted = agent.revert_to_step(step)
                reverted_rel = [os.path.relpath(p, wd) for p in reverted]
                feedback = f"[System] The user reverted to step {step}. The following files were reverted: "
                feedback += ", ".join(reverted_rel[:20]) if reverted_rel else "(none)"
                if len(reverted_rel) > 20:
                    feedback += f" (and {len(reverted_rel) - 20} more)"
                agent.history.append({"role": "user", "content": feedback})
                save_session()
                await wsr.send_json({
                    "type": "reverted_to_step",
                    "step": step,
                    "files": reverted_rel,
                    "no_checkpoint": not had_checkpoint and len(reverted_rel) == 0,
                })
                continue

            # ── Add todo (user adds a task in real time; agent sees it on next TodoRead) ───
            if msg_type == "add_todo":
                content = (data.get("content") or "").strip()
                if content:
                    existing_ids = []
                    for t in agent._todos:
                        tid = t.get("id")
                        if isinstance(tid, int):
                            existing_ids.append(tid)
                        elif isinstance(tid, str) and tid.isdigit():
                            existing_ids.append(int(tid))
                    next_id = str(max(existing_ids, default=0) + 1)
                    agent._todos.append({"id": next_id, "content": content, "status": "pending"})
                    save_session()
                    await wsr.send_json({"type": "todos_updated", "todos": list(agent._todos)})
                continue

            # ── Remove todo (user removes a task; agent sees updated list on next TodoRead) ───
            if msg_type == "remove_todo":
                todo_id = data.get("id")
                if todo_id is not None:
                    before = len(agent._todos)
                    agent._todos = [t for t in agent._todos if str(t.get("id")) != str(todo_id)]
                    if len(agent._todos) != before:
                        save_session()
                    await wsr.send_json({"type": "todos_updated", "todos": list(agent._todos)})
                continue

            # ── Build (approve plan) ───────────────────────────────
            if msg_type == "build" and awaiting_build:
                awaiting_build = False
                edited_steps = data.get("steps") or pending_plan or []
                _cancel_ack_sent = False
                _agent_task = asyncio.create_task(
                    _run_build_bg(pending_task or "", edited_steps, pending_images or [])
                )
                continue

            # ── Reject plan ────────────────────────────────────────
            if msg_type == "reject_plan" and awaiting_build:
                awaiting_build = False
                pending_task = None
                pending_plan = None
                pending_images = []
                await wsr.send_json({"type": "plan_rejected"})
                continue

            # ── Re-plan with feedback ──────────────────────────────
            if msg_type == "replan" and awaiting_build:
                feedback = data.get("content", "")
                task = f"{pending_task}\n\nUser feedback: {feedback}" if pending_task else feedback
                awaiting_build = False
                pending_task = task
                pending_plan = None

                # Fall through to task handling below
                msg_type = "task"
                data["content"] = task
                data["images"] = pending_images

            # ── Reset ──────────────────────────────────────────────
            if msg_type == "reset":
                if _agent_task and not _agent_task.done():
                    agent.cancel()
                    _agent_task.cancel()
                    _agent_task = None
                agent.reset()
                session = store.create_session(wd, model_config.model_id)
                save_session()
                await wsr.send_json({
                    "type": "reset_done",
                    "session_id": session.session_id,
                    "session_name": session.name,
                })
                continue

            # ── New task ───────────────────────────────────────────
            if msg_type == "task":
                task_text = data.get("content", "").strip()
                try:
                    task_images = _normalize_user_images(data.get("images") or [])
                except ValueError as ve:
                    await wsr.send_json({"type": "error", "content": f"Image upload error: {ve}"})
                    continue

                if not task_text and not task_images:
                    continue
                if not task_text and task_images:
                    task_text = "Analyze the attached image(s) and help me with the request."

                # Don't start a new task while one is running
                if _agent_task and not _agent_task.done():
                    await wsr.send_json({"type": "error", "content": "Agent is already running. Cancel first."})
                    continue

                # When user has uncommitted changes and sends a new task: preserve snapshots so
                # diff/revert keep growing (cumulative). Don't clear the keep/revert UI.
                preserve_snapshots = bool(awaiting_keep_revert and agent._file_snapshots)

                # Name session from first task — use LLM for smart titles
                if session and session.name == "default" and not agent.history:
                    if task_text:
                        # Set a quick placeholder immediately so UI isn't blank
                        words = task_text.split()[:6]
                        session.name = " ".join(words) + ("..." if len(task_text.split()) > 6 else "")
                        # Fire-and-forget: generate a proper title in background
                        _title_source = task_text

                        async def _generate_session_title(source_text: str):
                            try:
                                loop = asyncio.get_event_loop()
                                title = await loop.run_in_executor(
                                    None, bedrock_service.generate_title, source_text
                                )
                                if title and session:
                                    session.name = title
                                    save_session()
                                    try:
                                        await wsr.send_json({
                                            "type": "session_name_update",
                                            "session_name": title,
                                        })
                                    except Exception:
                                        pass  # WS may have closed
                            except Exception as e:
                                logger.debug(f"Background title generation failed: {e}")

                        asyncio.create_task(_generate_session_title(_title_source))
                    else:
                        session.name = "Image prompt"

                editor_context = data.get("context")
                _cancel_ack_sent = False
                _agent_task = asyncio.create_task(_run_task_bg(task_text, task_images, preserve_snapshots, editor_context=editor_context))
                continue

    try:
        # Send initial info
        mcfg = get_model_config(model_config.model_id)
        # Display-friendly working directory
        if is_ssh and _ssh_info:
            display_wd = f"{_ssh_info['user']}@{_ssh_info['host']}:{_ssh_info['directory']}"
        else:
            display_wd = wd
        await wsr.send_json({
            "type": "init",
            "model_name": get_model_name(model_config.model_id),
            "working_directory": display_wd,
            "context_window": mcfg.get("context_window", 0),
            "thinking": supports_thinking(model_config.model_id),
            "caching": supports_caching(model_config.model_id),
            "session_id": session.session_id if session else "",
            "session_name": session.name if session else "default",
            "message_count": session.message_count if session else 0,
            "total_tokens": agent.total_tokens,
            "input_tokens": getattr(agent, "_total_input_tokens", 0),
            "output_tokens": getattr(agent, "_total_output_tokens", 0),
            "cache_read": getattr(agent, "_cache_read_tokens", 0),
            "is_ssh": is_ssh,
        })

        # Replay conversation history so frontend rebuilds the chat
        await replay_history()

        # If the server was killed mid-stream, restore the in-progress
        # thinking/text block so the user sees what was accumulated.
        if _current_thinking_text:
            await wsr.send_json({"type": "replay_thinking", "content": _current_thinking_text})
            _current_thinking_text = ""  # consumed — don't re-send
        if _current_text_buffer:
            await wsr.send_json({"type": "replay_text", "content": _current_text_buffer})
            _current_text_buffer = ""  # consumed

        # Start periodic save so SSH disconnect/kill doesn't lose conversation
        _save_interval_task = asyncio.create_task(_periodic_save_loop())

        # Kick off background codebase index build (first connect only)
        global _bg_index_task
        if _bg_index_task is None or _bg_index_task.done():
            _bg_index_ready.clear()
            _bg_index_task = asyncio.create_task(
                _build_index_background(_backend or backend, agent_wd)
            )

        # ── Background task helpers ─────────────────────────────
        async def _send_status():
            """Send token count status to frontend."""
            try:
                ctx_window = get_context_window(model_config.model_id)
                ctx_est = agent._current_token_estimate() if hasattr(agent, '_current_token_estimate') else 0
                await wsr.send_json({
                    "type": "status",
                    "tokens": agent.total_tokens,
                    "input_tokens": agent._total_input_tokens,
                    "output_tokens": agent._total_output_tokens,
                    "cache_read": agent._cache_read_tokens,
                    "cache_write": agent._cache_write_tokens,
                    "context_usage_pct": round(ctx_est / ctx_window * 100) if ctx_window else 0,
                })
            except Exception:
                pass

        # Send status so token badge and context gauge are correct after connect (not stale 0 / yellow)
        await _send_status()

        async def _request_question_answer(
            question: str,
            context: Optional[str],
            tool_use_id: str,
            *,
            options: Optional[List[str]] = None,
        ) -> str:
            _pending_question["future"] = asyncio.get_event_loop().create_future()
            _pending_question["tool_use_id"] = tool_use_id
            try:
                payload = {
                    "type": "user_question",
                    "question": question,
                    "context": context or "",
                    "tool_use_id": tool_use_id,
                }
                if options:
                    payload["options"] = options
                await wsr.send_json(payload)
                return await asyncio.wait_for(_pending_question["future"], timeout=300.0)  # 5 min max
            finally:
                _pending_question["future"] = None
                _pending_question["tool_use_id"] = None

        async def _detect_and_apply_plan_update(ag, current_plan):
            """Check agent's last response for <updated_plan> tags and update plan/checklist."""
            nonlocal pending_plan
            if not ag.history:
                return
            last_msg = ag.history[-1]
            text = ""
            if isinstance(last_msg.get("content"), str):
                text = last_msg["content"]
            elif isinstance(last_msg.get("content"), list):
                for block in last_msg["content"]:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text += block.get("text", "")
            if not text:
                return

            # Look for <updated_plan>...</updated_plan> tags
            match = re.search(r"<updated_plan>\s*(.*?)\s*</updated_plan>", text, re.DOTALL)
            if not match:
                return

            plan_body = match.group(1).strip()
            if not plan_body:
                return

            # Parse numbered steps
            new_steps = []
            for line in plan_body.split("\n"):
                line = line.strip()
                if re.match(r"^\d+[\.\)]\s+", line):
                    new_steps.append(line)

            if not new_steps:
                return

            # Update state
            pending_plan = new_steps
            ag._current_plan = new_steps
            plan_text_full = "\n".join(new_steps)
            ag._plan_text = plan_text_full

            # Emit updated plan event to frontend
            await wsr.send_json({
                "type": "updated_plan",
                "steps": new_steps,
                "plan_text": plan_text_full,
                "plan_file": getattr(ag, "_plan_file_path", None),
            })

            # Also update the checklist
            todos = [
                {"id": str(i + 1), "content": s, "status": "pending"}
                for i, s in enumerate(new_steps)
            ]
            ag._todos = todos
            await wsr.send_json({"type": "todos_updated", "todos": todos})

        async def _run_task_bg(task_text: str, task_images: Optional[List[Dict[str, Any]]] = None, preserve_snapshots: bool = False, editor_context: Optional[Dict[str, Any]] = None):
            """Run a task (plan or direct mode) in the background. preserve_snapshots=True keeps diff/revert cumulative."""
            nonlocal awaiting_build, awaiting_keep_revert, pending_task, pending_plan, pending_images
            task_start = time.time()

            # Immediate feedback so the user sees activity right away
            await wsr.send_json({"type": "scout_progress", "content": "Preparing\u2026"})

            # Run auto-context, mention resolution, and intent classification in parallel
            async def _auto_ctx_task():
                try:
                    return await asyncio.to_thread(
                        _assemble_auto_context,
                        _working_directory,
                        editor_context,
                        agent.modified_files if hasattr(agent, "modified_files") else None,
                        backend=_backend,
                        user_query=task_text,
                    )
                except Exception as e:
                    logger.debug(f"Auto-context assembly failed: {e}")
                    return ""

            async def _mentions_task():
                try:
                    return await asyncio.to_thread(
                        _resolve_mentions, task_text, _working_directory, backend=_backend
                    )
                except Exception as e:
                    logger.debug(f"Mention resolution failed: {e}")
                    return task_text

            async def _intent_task():
                try:
                    return await asyncio.wait_for(
                        asyncio.to_thread(classify_intent, task_text, agent.service),
                        timeout=3.0,
                    )
                except (asyncio.TimeoutError, Exception):
                    return {"scout": True, "plan": False, "question": False, "complexity": "simple"}

            auto_ctx, task_text, intent = await asyncio.gather(
                _auto_ctx_task(), _mentions_task(), _intent_task()
            )

            # End the "Preparing" indicator now that pre-processing is done
            await wsr.send_json({"type": "scout_end"})

            try:
                is_question = intent.get("question", False)
                if app_config.plan_phase_enabled and intent.get("plan") and not is_question:
                    # Plan phase — inject auto-context into plan task too
                    plan_task = (auto_ctx + "\n\n" + task_text) if auto_ctx else task_text
                    plan_steps = await agent.run_plan(
                        task=plan_task,
                        on_event=on_event,
                        request_question_answer=_request_question_answer,
                        user_images=task_images or [],
                    )
                    if agent._cancelled:
                        return

                    elapsed = round(time.time() - task_start, 1)
                    await wsr.send_json({"type": "phase_end", "content": "plan", "elapsed": elapsed})

                    if plan_steps:
                        pending_task = task_text
                        pending_plan = plan_steps
                        pending_images = list(task_images or [])
                        awaiting_build = True
                        try:
                            await asyncio.to_thread(save_session)
                        except Exception:
                            pass  # don't break on save failure; replay will still send plan from agent state
                    else:
                        await wsr.send_json({"type": "no_plan"})
                else:
                    # Direct mode — pass scout decision from intent classification
                    # If user has a pending plan (didn't press Build), inject plan into context so
                    # follow-up questions ("change step 2 to...") have the plan in scope.
                    task_for_run = task_text
                    _is_plan_followup = False
                    if awaiting_build and (pending_plan or getattr(agent, "_plan_text", None)):
                        _is_plan_followup = True
                        plan_text = getattr(agent, "_plan_text", "") or ""
                        if not plan_text and pending_plan:
                            plan_text = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(pending_plan))
                        if plan_text.strip():
                            task_for_run = (
                                "[Current plan (user has not run Build yet; use for context):\n<current_plan>\n"
                                + plan_text.strip()
                                + "\n</current_plan>]\n\n"
                                "IMPORTANT: If the user asks to modify, add, remove, or change any plan steps, "
                                "you MUST output the complete updated plan as a numbered list wrapped in "
                                "<updated_plan> tags. For example:\n"
                                "<updated_plan>\n1. First step\n2. Second step\n</updated_plan>\n"
                                "Always include ALL steps (not just changed ones). "
                                "If the user is just asking a question about the plan, answer normally without the tags.\n\n"
                                "User's message: "
                                + task_text
                            )

                    # Inject auto-context if available
                    if auto_ctx:
                        task_for_run = auto_ctx + "\n\n" + task_for_run

                    # Smart model routing: use fast model for trivial/simple tasks
                    complexity = intent.get("complexity", "complex")
                    original_model = agent.service.model_id
                    if complexity in ("trivial", "simple") and app_config.fast_model:
                        agent.service.model_id = app_config.fast_model
                        logger.info(f"Smart routing: using fast model for {complexity} task")

                    # Smart scout skip: if auto-context includes active file and task is targeted, skip scout
                    enable_scout = intent.get("scout", True)
                    if auto_ctx and active_file_in_context(auto_ctx) and not intent.get("explore", False):
                        enable_scout = False
                        logger.info("Smart scout skip: auto-context provides sufficient file context")

                    await wsr.send_json({"type": "phase_start", "content": "direct"})
                    try:
                        await agent.run(
                            task=task_for_run,
                            on_event=on_event,
                            request_approval=dummy_approval,
                            enable_scout=enable_scout,
                            user_images=task_images or [],
                            preserve_snapshots=preserve_snapshots,
                            request_question_answer=_request_question_answer,
                        )
                    finally:
                        # Always restore the original model
                        agent.service.model_id = original_model
                    if agent._cancelled:
                        return

                    # Check if the agent's response contains an updated plan
                    if _is_plan_followup and agent.history:
                        _detect_and_apply_plan_update(agent, pending_plan)

                    elapsed = round(time.time() - task_start, 1)
                    await wsr.send_json({"type": "phase_end", "content": "direct", "elapsed": elapsed})
                    await wsr.send_json({"type": "done"})

                    # Show cumulative diff and Keep/Revert bar for ALL accumulated changes
                    if agent.modified_files:
                        awaiting_keep_revert = True
                        diffs = generate_diffs()
                        if diffs:
                            await wsr.send_json({"type": "diff", "files": diffs, "cumulative": True})
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.exception("Task error")
                try:
                    await wsr.send_json({"type": "error", "content": str(exc)})
                except Exception:
                    pass
            finally:
                save_session()
                await _send_status()

        async def _run_build_bg(task_text: str, steps: list, task_images: Optional[List[Dict[str, Any]]] = None):
            """Run build phase in the background."""
            nonlocal awaiting_keep_revert
            task_start = time.time()
            try:
                # Auto-create todos from plan steps so checklist is populated immediately
                agent._todos = [
                    {"id": str(i + 1), "content": s, "status": "pending"}
                    for i, s in enumerate(steps)
                ]
                await wsr.send_json({"type": "todos_updated", "todos": list(agent._todos)})

                await wsr.send_json({"type": "phase_start", "content": "build"})
                await agent.run_build(
                    task=task_text,
                    plan_steps=steps,
                    on_event=on_event,
                    request_approval=dummy_approval,
                    user_images=task_images or [],
                    request_question_answer=_request_question_answer,
                )
                if agent._cancelled:
                    return

                elapsed = round(time.time() - task_start, 1)
                await wsr.send_json({"type": "phase_end", "content": "build", "elapsed": elapsed})
                await wsr.send_json({"type": "done"})

                # Show cumulative diff and Keep/Revert bar for ALL accumulated changes
                if agent.modified_files:
                    awaiting_keep_revert = True
                    diffs = generate_diffs()
                    if diffs:
                        await wsr.send_json({"type": "diff", "files": diffs, "cumulative": True})
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.exception("Build error")
                try:
                    await wsr.send_json({"type": "error", "content": str(exc)})
                except Exception:
                    pass
            finally:
                save_session()
                await _send_status()

        # ── Background file watcher ────────────────────────────
        _file_mtimes: Dict[str, float] = {}
        async def _file_watcher():
            """Lightweight polling watcher that detects external file changes."""
            POLL_INTERVAL = 3  # seconds
            IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".bedrock-codex", ".venv", "venv"}
            try:
                # Initial scan
                for root, dirs, files in os.walk(wd):
                    dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith(".")]
                    for fname in files[:200]:  # cap per directory
                        fpath = os.path.join(root, fname)
                        try:
                            _file_mtimes[fpath] = os.path.getmtime(fpath)
                        except OSError:
                            pass

                while True:
                    await asyncio.sleep(POLL_INTERVAL)
                    agent_busy = _agent_task is not None and not _agent_task.done()
                    changed = []
                    for root, dirs, files in os.walk(wd):
                        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith(".")]
                        for fname in files[:200]:
                            fpath = os.path.join(root, fname)
                            try:
                                mtime = os.path.getmtime(fpath)
                                prev = _file_mtimes.get(fpath)
                                if prev is None or mtime > prev + 0.1:
                                    _file_mtimes[fpath] = mtime
                                    if not agent_busy and prev is not None:
                                        changed.append(os.path.relpath(fpath, wd))
                            except OSError:
                                pass

                    for rel in changed[:10]:  # cap events per poll
                        try:
                            await wsr.send_json({"type": "file_changed", "path": rel})
                        except Exception:
                            return
            except asyncio.CancelledError:
                return
            except Exception:
                pass  # watcher failure is non-fatal

        # Only enable file watcher for local backends
        if isinstance(backend, LocalBackend):
            _watcher_task = asyncio.create_task(_file_watcher())

        # ── Message loop ───────────────────────────────────────
        await _message_loop()

    except WebSocketDisconnect:
        # Cancel disposable background tasks
        if _save_interval_task and not _save_interval_task.done():
            _save_interval_task.cancel()
        if _watcher_task and not _watcher_task.done():
            _watcher_task.cancel()

        if _agent_task and not _agent_task.done():
            # ── Agent still running: wait for client to reconnect ──
            wsr.ws = None  # sends become silent no-ops while disconnected
            _rc_future: asyncio.Future = asyncio.get_event_loop().create_future()
            _reconnect_sessions[session.session_id] = {"future": _rc_future}
            save_session()
            logger.info("WS disconnected — agent still running (session %s). Waiting for reconnect…", session.session_id)

            _new_ws = None
            _done_event = None
            try:
                _new_ws, _done_event = await asyncio.wait_for(_rc_future, timeout=300)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                logger.info("Reconnect timeout / cancelled (session %s) — cleaning up", session.session_id)
                agent.cancel()
                if _agent_task and not _agent_task.done():
                    _agent_task.cancel()
                save_session()
                _reconnect_sessions.pop(session.session_id, None)
                _active_agent = None
                return

            # Reconnected!
            _reconnect_sessions.pop(session.session_id, None)
            ws = _new_ws
            wsr.ws = _new_ws
            logger.info("WS reconnected (session %s)", session.session_id)

            try:
                # Re-send init + replay so the frontend rebuilds the chat
                mcfg = get_model_config(model_config.model_id)
                if is_ssh and _ssh_info:
                    display_wd = f"{_ssh_info['user']}@{_ssh_info['host']}:{_ssh_info['directory']}"
                else:
                    display_wd = wd
                await wsr.send_json({
                    "type": "init",
                    "model_name": get_model_name(model_config.model_id),
                    "working_directory": display_wd,
                    "context_window": mcfg.get("context_window", 0),
                    "thinking": supports_thinking(model_config.model_id),
                    "caching": supports_caching(model_config.model_id),
                    "session_id": session.session_id if session else "",
                    "session_name": session.name if session else "default",
                    "message_count": session.message_count if session else 0,
                    "total_tokens": agent.total_tokens,
                    "input_tokens": getattr(agent, "_total_input_tokens", 0),
                    "output_tokens": getattr(agent, "_total_output_tokens", 0),
                    "cache_read": getattr(agent, "_cache_read_tokens", 0),
                    "is_ssh": is_ssh,
                })
                await replay_history()
                _agent_running = _agent_task is not None and not _agent_task.done()

                # If the agent was mid-stream when we disconnected, replay
                # the buffered content so the frontend picks up where it left off.
                if _agent_running:
                    if _current_thinking_text:
                        await wsr.send_json({"type": "thinking_start", "content": ""})
                        await wsr.send_json({"type": "thinking", "content": _current_thinking_text})
                    if _current_text_buffer:
                        await wsr.send_json({"type": "text_start", "content": ""})
                        await wsr.send_json({"type": "text", "content": _current_text_buffer})

                await wsr.send_json({"type": "resumed", "agent_running": _agent_running})
                await _send_status()

                # Restart disposable tasks
                _save_interval_task = asyncio.create_task(_periodic_save_loop())
                if isinstance(backend, LocalBackend):
                    _watcher_task = asyncio.create_task(_file_watcher())

                # Re-enter message loop with the new WebSocket
                await _message_loop()

            except WebSocketDisconnect:
                # Second disconnect — clean up normally
                if _save_interval_task and not _save_interval_task.done():
                    _save_interval_task.cancel()
                if _watcher_task and not _watcher_task.done():
                    _watcher_task.cancel()
                if _agent_task and not _agent_task.done():
                    agent.cancel()
                    _agent_task.cancel()
                save_session()
                logger.info("WS disconnected again after reconnect")
            except Exception as exc:
                if _agent_task and not _agent_task.done():
                    agent.cancel()
                    _agent_task.cancel()
                logger.exception("WS error after reconnect: %s", exc)
                save_session()
            finally:
                if _done_event:
                    _done_event.set()
        else:
            # No running task — normal disconnect
            save_session()
            logger.info("WebSocket disconnected")
        _active_agent = None
        if session and session.session_id:
            _active_save_fns.pop(session.session_id, None)
    except Exception as e:
        if _save_interval_task and not _save_interval_task.done():
            _save_interval_task.cancel()
        if _agent_task and not _agent_task.done():
            agent.cancel()
            _agent_task.cancel()
        logger.exception(f"WebSocket error: {e}")
        save_session()
        _active_agent = None
        if session and session.session_id:
            _active_save_fns.pop(session.session_id, None)

# ============================================================
# Entry point
# ============================================================

def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="Bedrock Codex — Web GUI")
    parser.add_argument("--port", type=int, default=8765, help="Server port (default: 8765)")
    parser.add_argument("--host", default="127.0.0.1", help="Server host (default: 127.0.0.1)")
    parser.add_argument("--dir", default=".", help="Working directory for the agent")
    parser.add_argument("--ssh", default=None, help="SSH remote: user@host (e.g. deploy@192.168.1.50)")
    parser.add_argument("--key", default=None, help="SSH private key path (default: ~/.ssh/id_rsa)")
    parser.add_argument("--ssh-port", type=int, default=22, help="SSH port (default: 22)")
    args = parser.parse_args()

    global _working_directory, _backend, _explicit_dir
    _working_directory = os.path.abspath(os.path.expanduser(args.dir))

    if args.ssh:
        # SSH remote mode — always explicit
        _explicit_dir = True
        from backend import SSHBackend
        parts = args.ssh.split("@", 1)
        if len(parts) == 2:
            user, host = parts
        else:
            user, host = None, parts[0]

        remote_dir = args.dir if args.dir != "." else "/home/" + (user or "root")

        try:
            _backend = SSHBackend(
                host=host,
                working_directory=remote_dir,
                user=user,
                key_path=args.key,
                port=args.ssh_port,
            )
            _working_directory = remote_dir
            print(f"\n  Bedrock Codex — Web GUI (SSH Remote)")
            print(f"  http://{args.host}:{args.port}")
            print(f"  Remote: {args.ssh}:{remote_dir}\n")
        except Exception as e:
            print(f"\n  SSH connection failed: {e}\n")
            raise SystemExit(1)
    else:
        # Local mode
        # If --dir was explicitly passed (not default "."), skip the welcome screen
        _explicit_dir = args.dir != "."

        if _explicit_dir and not os.path.isdir(_working_directory):
            print(f"\n  Error: directory not found: {_working_directory}")
            print(f"  Hint: use the full path, e.g. --dir ~/Desktop/my-project")
            print(f"        or run from inside the project with --dir .\n")
            raise SystemExit(1)

        _backend = LocalBackend(_working_directory)
        print(f"\n  Bedrock Codex — Web GUI")
        print(f"  http://{args.host}:{args.port}")
        if _explicit_dir:
            print(f"  Working directory: {_working_directory}")
        else:
            print(f"  Welcome screen enabled — select a project in the browser")
        print()

    # Ensure our app logs (e.g. terminal ws) are visible; uvicorn's log_level only affects its own loggers
    web_log = logging.getLogger("web")
    web_log.setLevel(logging.INFO)
    if not web_log.handlers:
        h = logging.StreamHandler()
        h.setLevel(logging.INFO)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [web] %(message)s"))
        web_log.addHandler(h)

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
