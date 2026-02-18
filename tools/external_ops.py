"""External and miscellaneous tools: run_command, web_fetch, web_search, semantic_retrieve, todos."""

import os
import re
import json
import logging
from typing import Any, Dict, List, Optional

from backend import Backend, LocalBackend
from tools._common import ToolResult, _current_todos_ctx

logger = logging.getLogger(__name__)


def run_command(command: str, timeout: int = 30,
                backend: Optional[Backend] = None, working_directory: str = ".", **kw: Any) -> ToolResult:
    """Execute a shell command."""
    if not (command or "").strip():
        return ToolResult(success=False, output="", error="command is required")
    try:
        b = backend or LocalBackend(working_directory)
        stdout, stderr, rc = b.run_command(command, cwd=".", timeout=timeout)

        parts = []
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append(f"[stderr]\n{stderr}")
        output = "\n".join(parts) if parts else "(no output)"
        if rc != 0:
            output = f"[exit code: {rc}]\n{output}"

        if len(output) > 20000:
            lines_out = output.split("\n")
            if len(lines_out) > 200:
                output = "\n".join(lines_out[:100]) + f"\n\n... [{len(lines_out) - 150} lines truncated] ...\n\n" + "\n".join(lines_out[-50:])
            else:
                output = output[:10000] + "\n\n... [truncated] ...\n\n" + output[-5000:]

        return ToolResult(
            success=rc == 0, output=output,
            error=None if rc == 0 else f"Command exited with code {rc}",
        )
    except ValueError as e:
        if "disallowed" in str(e).lower() or "metacharacters" in str(e).lower():
            return ToolResult(success=False, output="", error=str(e))
        return ToolResult(success=False, output="", error=str(e))
    except Exception as e:
        if "timed out" in str(e).lower() or "TimeoutExpired" in type(e).__name__:
            return ToolResult(success=False, output="", error=f"Command timed out after {timeout}s")
        return ToolResult(success=False, output="", error=str(e))


def semantic_retrieve(
    query: str,
    top_k: int = 10,
    backend: Optional[Backend] = None,
    working_directory: str = ".",
    **kw: Any,
) -> ToolResult:
    """Semantic search over the codebase index."""
    try:
        from config import app_config
        if not getattr(app_config, "codebase_index_enabled", True):
            return ToolResult(success=False, output="", error="Codebase index is disabled.")
        from codebase_index import get_index, get_embed_fn
        is_ssh = backend is not None and getattr(backend, "_host", None) is not None
        wd = working_directory if is_ssh else os.path.abspath(working_directory)
        embed_fn = get_embed_fn()
        index = get_index(wd, embed_fn=embed_fn, backend=backend)
        if not index.chunks and embed_fn and backend:
            index.build(backend, force_reindex=False)
        if not index.chunks:
            return ToolResult(
                success=False,
                output="",
                error="Index empty. Ensure CODEBASE_INDEX_ENABLED=true and the project has been indexed (e.g. run a task once to trigger index build).",
            )
        k = max(1, min(20, top_k))
        chunks = index.retrieve(query.strip(), top_k=k)
        if not chunks:
            return ToolResult(success=True, output="No relevant chunks found for this query. Try a different query or use search/Read.")
        lines = [f"Semantic retrieval (top {len(chunks)}):", ""]
        for i, c in enumerate(chunks, 1):
            lines.append(f"--- Result {i}: {c.path}:{c.start_line}-{c.end_line} [{c.kind}] {c.name} ---")
            lines.append(c.to_search_snippet())
            lines.append("")
        return ToolResult(success=True, output="\n".join(lines))
    except Exception as e:
        logger.exception("semantic_retrieve failed")
        return ToolResult(success=False, output="", error=str(e))


# --- WebFetch ---
_WEB_FETCH_MAX_BYTES = 500_000
_WEB_FETCH_DEFAULT_TIMEOUT = 15


def web_fetch(url: str, timeout: Optional[int] = None, **kwargs: Any) -> ToolResult:
    """Fetch content from a URL via HTTP GET. Returns plain text; HTML is stripped roughly."""
    url = (url or "").strip()
    if not url:
        return ToolResult(success=False, output="", error="url is required")
    if not url.startswith(("http://", "https://")):
        return ToolResult(success=False, output="", error="url must start with http:// or https://")
    try:
        import urllib.request
        import urllib.error
        req = urllib.request.Request(url, headers={"User-Agent": "BedrockAgent/1.0"})
        to = min(60, max(1, timeout or _WEB_FETCH_DEFAULT_TIMEOUT))
        with urllib.request.urlopen(req, timeout=to) as resp:
            body = resp.read(_WEB_FETCH_MAX_BYTES + 1)
            if len(body) > _WEB_FETCH_MAX_BYTES:
                body = body[:_WEB_FETCH_MAX_BYTES]
                truncated = True
            else:
                truncated = False
            try:
                text = body.decode("utf-8", errors="replace")
            except Exception:
                text = body.decode("latin-1", errors="replace")
        text = re.sub(r"<script[^>]*>[\s\S]*?</script>", "", text, flags=re.IGNORECASE)
        text = re.sub(r"<style[^>]*>[\s\S]*?</style>", "", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        if truncated:
            text += "\n\n[Content truncated — response was larger than 500KB.]"
        return ToolResult(success=True, output=text[:100_000], error=None)
    except Exception as e:
        logger.exception("web_fetch failed")
        return ToolResult(success=False, output="", error=str(e))


def todo_write(todos: list, **kwargs: Any) -> ToolResult:
    """Create or update the task checklist for this session."""
    try:
        if not isinstance(todos, list):
            return ToolResult(success=False, output="", error="todos must be a list")

        valid_statuses = {"pending", "in_progress", "completed", "cancelled"}
        for i, todo in enumerate(todos):
            if not isinstance(todo, dict):
                return ToolResult(success=False, output="", error=f"todo[{i}] must be a dict")
            if "content" not in todo or "status" not in todo:
                return ToolResult(success=False, output="", error=f"todo[{i}] missing required fields: content, status")
            if todo["status"] not in valid_statuses:
                return ToolResult(success=False, output="", error=f"todo[{i}] invalid status: {todo['status']}")

        return ToolResult(
            success=True,
            output=f"Updated task checklist with {len(todos)} items",
        )
    except Exception as e:
        return ToolResult(success=False, output="", error=f"todo_write failed: {str(e)}")


def todo_read(
    working_directory: str = ".",
    backend: Optional[Backend] = None,
    todos: Optional[List[Dict[str, Any]]] = None,
    **kwargs: Any,
) -> ToolResult:
    """Get the current task checklist for this session."""
    try:
        if todos is not None:
            lst = todos
        else:
            lst = _current_todos_ctx.get()
        if not lst:
            return ToolResult(success=True, output="No active task checklist found.")
        return ToolResult(success=True, output=json.dumps(lst, indent=2))
    except Exception as e:
        return ToolResult(success=False, output="", error=f"todo_read failed: {str(e)}")


def web_search(query: str, max_results: int = 5, **kwargs: Any) -> ToolResult:
    """Search the web; uses duckduckgo_search if installed."""
    query = (query or "").strip()
    if not query:
        return ToolResult(success=False, output="", error="query is required")
    max_results = max(1, min(10, max_results or 5))
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        if not results:
            return ToolResult(success=True, output="No results found for that query.", error=None)
        lines = [f"Web search: \"{query}\"\n"]
        for i, r in enumerate(results, 1):
            title = (r.get("title") or "").strip()
            href = (r.get("href") or r.get("link") or "").strip()
            body = (r.get("body") or "").strip()[:400]
            lines.append(f"{i}. {title}\n   {href}\n   {body}\n")
        return ToolResult(success=True, output="\n".join(lines), error=None)
    except ImportError:
        return ToolResult(
            success=False,
            output="",
            error="Web search requires the duckduckgo-search package. Install with: pip install duckduckgo-search",
        )
    except Exception as e:
        logger.exception("web_search failed")
        return ToolResult(success=False, output="", error=str(e))
