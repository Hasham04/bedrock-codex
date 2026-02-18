"""
Bedrock Codex — Web GUI server.
FastAPI + WebSocket bridge to the CodingAgent.

Run:  python -m web [--port 8765] [--dir /path/to/project]
Open: http://localhost:8765
"""

import logging

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from web.state import _active_save_fns, STATIC_DIR
from web import api_files, api_git, api_sessions, terminal, chat

logger = logging.getLogger(__name__)

# ============================================================
# FastAPI application
# ============================================================

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
# Include routers from submodules
# ============================================================

app.include_router(api_files.router)
app.include_router(api_git.router)
app.include_router(api_sessions.router)
app.include_router(terminal.router)
app.include_router(chat.router)