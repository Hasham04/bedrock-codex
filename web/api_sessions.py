"""
Session and project management REST API endpoints.
"""

import asyncio
import json
import logging
import os
import posixpath
import shlex
from typing import Optional, Dict, Any, List

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

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
from web.state import (
    STATIC_DIR, _BOOT_TS, _LAST_PROJECT_FILE,
)
import web.state as _state

logger = logging.getLogger(__name__)

router = APIRouter()


def _persist_last_project(path: str) -> None:
    """Write last-opened project path to disk."""
    try:
        os.makedirs(os.path.dirname(_LAST_PROJECT_FILE), exist_ok=True)
        with open(_LAST_PROJECT_FILE, "w") as f:
            json.dump({"path": path}, f)
    except Exception:
        pass


def _load_last_project() -> Optional[str]:
    """Read last-opened project path from disk."""
    try:
        with open(_LAST_PROJECT_FILE) as f:
            return json.load(f).get("path")
    except Exception:
        return None


@router.get("/")
async def index():
    """Serve index.html with a dynamic cache-buster so the browser
    always picks up the latest JS/CSS after a server restart."""
    html_path = os.path.join(STATIC_DIR, "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    # Replace static version tags with the boot timestamp
    html = html.replace("style.css?v=", f"style.css?v={_BOOT_TS}&_v=")
    for _js in ("state", "utils", "explorer", "editor", "terminal", "chat", "ws", "welcome"):
        html = html.replace(f"js/{_js}.js?v=", f"js/{_js}.js?v={_BOOT_TS}&_v=")
    resp = HTMLResponse(html)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@router.get("/api/info")
async def info():
    """Return model and config info for the frontend."""
    # If no explicit dir and no user-opened project yet, check persisted last project
    if not _state._explicit_dir and not _state._user_opened_project:
        last = _load_last_project()
        if last and os.path.isdir(last):
            _state._working_directory = last
            _state._backend = LocalBackend(last)
            _state._user_opened_project = True

    mcfg = get_model_config(model_config.model_id)
    return {
        "model_name": get_model_name(model_config.model_id),
        "model_id": model_config.model_id,
        "context_window": get_context_window(model_config.model_id),
        "max_output_tokens": mcfg.get("max_output_tokens", 0),
        "thinking": supports_thinking(model_config.model_id),
        "caching": supports_caching(model_config.model_id),
        "working_directory": os.path.abspath(_state._working_directory),
        "plan_phase_enabled": app_config.plan_phase_enabled,
        "show_welcome": not _state._explicit_dir and not _state._user_opened_project,
    }


@router.get("/api/sessions")
async def list_sessions():
    def _session_wd_key() -> str:
        if _state._ssh_info is not None:
            return _state._working_directory
        return os.path.abspath(_state._working_directory)

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


@router.post("/api/sessions/new")
async def create_session(request: Request):
    def _session_wd_key() -> str:
        if _state._ssh_info is not None:
            return _state._working_directory
        return os.path.abspath(_state._working_directory)

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


@router.get("/api/projects")
async def list_projects():
    """List all known projects from session history — used by the welcome screen."""
    store = SessionStore()
    return store.list_all_projects()


@router.post("/api/projects/remove")
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


@router.post("/api/ssh-list-dir")
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


@router.post("/api/ssh-connect")
async def ssh_connect(request: Request):
    """Connect to a remote host via SSH at runtime."""
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
        def _do_connect():
            return SSHBackend(
                host=host,
                working_directory=directory,
                user=user,
                key_path=key_path,
                port=port,
            )

        _state._backend = await asyncio.to_thread(_do_connect)
        # Resolve directory to absolute path so terminal cd .. / cd back works
        try:
            out, err, rc = await asyncio.to_thread(_state._backend.run_command, "pwd", ".", 10)
            if rc == 0 and out and out.strip():
                directory = out.strip()
                _state._backend._working_directory = directory
        except Exception:
            pass
        # Composite working directory: user@host:port:directory — unique per SSH target
        _state._working_directory = f"{user}@{host}:{port}:{directory}"
        _state._ssh_info = {
            "host": host,
            "user": user,
            "port": port,
            "key_path": key_path or "",
            "directory": directory,
        }
        display = f"{user}@{host}:{directory}"
        logger.info(f"SSH connected to {display}")
        _state._user_opened_project = True
        _persist_last_project(_state._working_directory)
        return {"ok": True, "path": _state._working_directory}
    except Exception as e:
        logger.error(f"SSH connection failed: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/api/set-directory")
async def set_directory(body: dict):
    """Change the working directory at runtime."""
    raw = body.get("path", "").strip()
    if not raw:
        return JSONResponse({"ok": False, "error": "No path provided"}, status_code=400)

    expanded = os.path.expanduser(raw)
    resolved = os.path.abspath(expanded)

    if not os.path.isdir(resolved):
        return JSONResponse({"ok": False, "error": f"Directory not found: {resolved}"})

    _state._working_directory = resolved
    _state._backend = LocalBackend(resolved)
    _state._ssh_info = None  # Clear SSH info when switching to local
    _state._user_opened_project = True
    _persist_last_project(resolved)
    return {"ok": True, "path": resolved}
