"""
Git-related REST API endpoints.

Handles git status, file diffs, and diff stats.
"""

import asyncio
import logging
import os
import posixpath
import shlex
import subprocess
from typing import Optional, Dict, Any, List

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from backend import Backend, LocalBackend
from web.state import _backend, _working_directory

logger = logging.getLogger(__name__)

router = APIRouter()


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


@router.get("/api/git-status")
async def api_git_status():
    """Return git status for the project. Path -> 'M'|'A'|'D'|'U'. Empty if not a git repo or SSH."""
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


@router.get("/api/git-file-diff")
async def api_git_file_diff(path: str = Query(...)):
    """Return original (HEAD) and current (working tree) content for a file. For inline git diffs."""
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


@router.get("/api/git-diff-stats")
async def api_git_diff_stats():
    """Return per-file and total diff stats (additions/deletions) for working tree vs HEAD.
    Used by the Cursor-style modified files dropdown."""
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
