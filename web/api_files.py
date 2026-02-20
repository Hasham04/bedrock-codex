"""
File-related REST API endpoints.

Handles file tree, read/write, delete, rename, mkdir, search, replace, find-symbol,
file diffs (agent snapshots).
"""

import asyncio
import base64
import difflib
import errno
import logging
import os
import posixpath
import re
from typing import Optional, Dict, Any, List

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from backend import Backend, LocalBackend
import web.state as _state
from web.state import (
    _IGNORE_DIRS, _IGNORE_EXTENSIONS,
    _MAX_FILE_SIZE, _MAX_IMAGE_ATTACHMENTS, _MAX_IMAGE_BYTES,
    _MAX_IMAGE_TOTAL_BYTES, _ALLOWED_IMAGE_MEDIA_TYPES,
)

logger = logging.getLogger(__name__)

router = APIRouter()


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


@router.get("/api/files")
async def list_files(path: str = "", recursive: bool = False):
    """Return file tree entries for a directory.

    - Default (recursive=false): lazy — one level at a time.
    - recursive=true: flat list of ALL files (for fuzzy file search in explorer).
    """
    import posixpath
    b = _state._backend or LocalBackend(os.path.abspath(_state._working_directory))
    is_ssh = hasattr(b, '_client')  # SSHBackend has _client attr

    if recursive:
        return await asyncio.to_thread(_list_all_files_recursive, os.path.abspath(_state._working_directory), is_ssh, backend=b)

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


@router.get("/api/find-symbol")
async def api_find_symbol(symbol: str = Query(...), kind: str = Query("definition")):
    """Find symbol definitions for Go to Definition in the editor."""
    from tools import find_symbol as _find_symbol
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                _find_symbol, symbol, kind=kind, working_directory=_state._working_directory
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


@router.get("/api/file")
async def read_file(path: str = Query(...)):
    """Return the contents of a file as plain text."""
    path = (path or "").strip().replace("\\", "/")
    if not path or path.endswith("/") or ".." in path or path.startswith("/"):
        return JSONResponse({"error": "Invalid path or directory"}, status_code=400)
    b = _state._backend or LocalBackend(os.path.abspath(_state._working_directory))
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


@router.put("/api/file")
async def write_file(request: Request):
    """Save file content from the editor."""
    b = _state._backend or LocalBackend(os.path.abspath(_state._working_directory))
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


@router.post("/api/file/delete")
async def delete_file(request: Request):
    """Delete a file or directory (recursively). Works over SSH."""
    body = await request.json()
    rel_path = _validate_rel_path(body.get("path", ""))
    if not rel_path:
        return JSONResponse({"ok": False, "error": "Invalid path"}, status_code=400)
    b = _state._backend or LocalBackend(os.path.abspath(_state._working_directory))
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


@router.post("/api/file/rename")
async def rename_file(request: Request):
    """Rename (move) a file or directory. Works over SSH."""
    body = await request.json()
    old_path = _validate_rel_path(body.get("old_path", ""))
    new_path = _validate_rel_path(body.get("new_path", ""))
    if not old_path or not new_path:
        return JSONResponse({"ok": False, "error": "Invalid path"}, status_code=400)
    b = _state._backend or LocalBackend(os.path.abspath(_state._working_directory))
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


@router.post("/api/file/mkdir")
async def mkdir_file(request: Request):
    """Create a new directory (including intermediate parents). Works over SSH."""
    body = await request.json()
    rel_path = _validate_rel_path(body.get("path", ""))
    if not rel_path:
        return JSONResponse({"ok": False, "error": "Invalid path"}, status_code=400)
    b = _state._backend or LocalBackend(os.path.abspath(_state._working_directory))
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
# File diff (agent snapshots)
# ------------------------------------------------------------------

def _get_effective_wd() -> str:
    """Get the effective working directory (remote dir for SSH, absolute path for local)."""
    if _state._ssh_info:
        return _state._ssh_info["directory"]
    return os.path.abspath(_state._working_directory)


@router.get("/api/file-diff")
async def file_diff(path: str = Query(...)):
    """Return original and current content for a file the agent modified."""
    wd = _get_effective_wd()
    safe = _safe_path(wd, path)
    if safe is None:
        return JSONResponse({"error": "Invalid path"}, status_code=400)

    agent = _state._active_agent
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
    b = _state._backend or LocalBackend(wd)
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

@router.get("/api/search")
async def api_search(
    pattern: str = Query(...),
    path: str = Query(""),
    include: str = Query(""),
):
    """Search for a regex pattern across the project. Returns structured results."""
    b = _state._backend or LocalBackend(os.path.abspath(_state._working_directory))
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


@router.post("/api/replace")
async def api_replace(request: Request):
    """Search-and-replace across specified files."""
    b = _state._backend or LocalBackend(os.path.abspath(_state._working_directory))
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
