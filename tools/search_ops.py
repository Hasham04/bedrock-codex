"""Search, discovery, and navigation tools."""

import os
import re
import pathlib
import logging
from typing import Any, Dict, List, Optional, Set

import pathspec

from backend import Backend, LocalBackend
from tools._common import ToolResult
from tools.gitignore import (
    _load_gitignore,
    _is_ignored,
    _ALWAYS_SKIP_DIRS,
    _ALWAYS_SKIP_EXTENSIONS,
)

logger = logging.getLogger(__name__)


def search(pattern: str, path: Optional[str] = None, include: Optional[str] = None,
           backend: Optional[Backend] = None, working_directory: str = ".", **kw: Any) -> ToolResult:
    """Search for a regex pattern using ripgrep (or grep fallback)."""
    try:
        b = backend or LocalBackend(working_directory)
        result = b.search(pattern, path or ".", include=include, cwd=".")

        if not result:
            return ToolResult(success=True, output="No matches found.")

        lines = result.split("\n")
        if len(lines) > 100:
            result = "\n".join(lines[:100]) + f"\n\n... [{len(lines) - 100} more matches truncated]"

        return ToolResult(success=True, output=result)
    except Exception as e:
        if "timed out" in str(e).lower():
            return ToolResult(success=False, output="", error="Search timed out")
        return ToolResult(success=False, output="", error=str(e))


def find_symbol(symbol: str, kind: str = "all", path: Optional[str] = None, include: Optional[str] = None,
                backend: Optional[Backend] = None, working_directory: str = ".", **kw: Any) -> ToolResult:
    """Find symbol definitions/references with language-aware regex heuristics."""
    try:
        b = backend or LocalBackend(working_directory)
        target = path or "."
        sym = re.escape(symbol.strip())
        if not sym:
            return ToolResult(success=False, output="", error="symbol is required")

        definition_patterns = [
            rf"^\s*def\s+{sym}\s*\(",
            rf"^\s*class\s+{sym}\b",
            rf"^\s*async\s+def\s+{sym}\s*\(",
            rf"^\s*(?:export\s+)?(?:async\s+)?function\s+{sym}\s*\(",
            rf"^\s*(?:export\s+)?(?:const|let|var)\s+{sym}\s*=\s*(?:async\s*)?\(",
            rf"^\s*(?:public|private|protected)?\s*(?:static\s+)?{sym}\s*\(",
            rf"\b{sym}\s*:\s*(?:function|\()",
            rf"^\s*interface\s+{sym}\b",
            rf"^\s*type\s+{sym}\b",
            rf"^\s*struct\s+{sym}\b",
            rf"^\s*enum\s+{sym}\b",
            rf"^\s*trait\s+{sym}\b",
        ]
        reference_pattern = rf"\b{sym}\b"

        outputs: List[str] = []
        if kind in ("all", "definition", "definitions", "def"):
            def_hits: List[str] = []
            for pat in definition_patterns:
                res = b.search(pat, target, include=include, cwd=".")
                if res:
                    def_hits.extend([ln for ln in res.split("\n") if ln.strip()])
            seen = set()
            dedup_defs = []
            for line in def_hits:
                if line not in seen:
                    seen.add(line)
                    dedup_defs.append(line)
            if dedup_defs:
                outputs.append("Definitions:\n" + "\n".join(dedup_defs[:120]))
            else:
                outputs.append("Definitions:\nNo matches found.")

        if kind in ("all", "reference", "references", "ref"):
            refs = b.search(reference_pattern, target, include=include, cwd=".")
            if refs:
                lines = [ln for ln in refs.split("\n") if ln.strip()]
                outputs.append("References:\n" + "\n".join(lines[:160]))
            else:
                outputs.append("References:\nNo matches found.")

        return ToolResult(success=True, output="\n\n".join(outputs))
    except Exception as e:
        if "timed out" in str(e).lower():
            return ToolResult(success=False, output="", error="Symbol search timed out")
        return ToolResult(success=False, output="", error=str(e))


def list_directory(path: Optional[str] = None,
                   backend: Optional[Backend] = None, working_directory: str = ".", **kw: Any) -> ToolResult:
    """List files and directories at a path, respecting .gitignore."""
    try:
        b = backend or LocalBackend(working_directory)
        target = path or "."

        if not b.is_dir(target):
            return ToolResult(success=False, output="", error=f"Not a directory: {target}")

        entries = b.list_dir(target)
        wd = b.working_directory if hasattr(b, 'working_directory') else os.path.abspath(working_directory)
        gi = _load_gitignore(wd, backend=b)
        lines = []
        for e in entries:
            name = e["name"]
            is_dir = e["type"] == "directory"
            rel = os.path.join(target, name) if target != "." else name
            if _is_ignored(rel, name, is_dir, gi):
                continue
            if is_dir:
                lines.append(f"  {name}/")
            else:
                size = e.get("size", 0)
                lines.append(f"  {name} ({_format_size(size)})")

        display = b.resolve_path(target)
        output = f"{display}/\n" + "\n".join(lines) if lines else f"{display}/ (empty)"
        return ToolResult(success=True, output=output)
    except Exception as e:
        return ToolResult(success=False, output="", error=str(e))


def glob_find(pattern: str,
              backend: Optional[Backend] = None, working_directory: str = ".", **kw: Any) -> ToolResult:
    """Find files matching a glob pattern, respecting .gitignore."""
    try:
        b = backend or LocalBackend(working_directory)
        raw_matches = b.glob_find(pattern, ".")
        wd = b.working_directory if hasattr(b, 'working_directory') else os.path.abspath(working_directory)
        gi = _load_gitignore(wd, backend=b)

        matches = []
        for m in raw_matches:
            name = os.path.basename(m)
            parts = pathlib.PurePath(m).parts
            if any(p in _ALWAYS_SKIP_DIRS for p in parts):
                continue
            _, ext = os.path.splitext(name)
            if ext in _ALWAYS_SKIP_EXTENSIONS:
                continue
            if gi and gi.match_file(m):
                continue
            matches.append(m)

        if not matches:
            return ToolResult(success=True, output="No files found matching pattern.")

        output = f"Found {len(matches)} match(es):\n" + "\n".join(f"  {m}" for m in matches[:200])
        if len(matches) > 200:
            output += f"\n  ... [{len(matches) - 200} more]"
        return ToolResult(success=True, output=output)
    except Exception as e:
        return ToolResult(success=False, output="", error=str(e))


# ── project_tree — recursive, token-budgeted, .gitignore-aware ──

_KEY_CONFIG_FILES: Set[str] = {
    "package.json", "tsconfig.json", "pyproject.toml", "setup.py", "setup.cfg",
    "requirements.txt", "Pipfile", "pom.xml", "build.gradle", "build.gradle.kts",
    "settings.gradle", "Makefile", "Dockerfile", "docker-compose.yml",
    "docker-compose.yaml", ".env.example", "Cargo.toml", "go.mod",
    "CMakeLists.txt", "README.md", "AGENTS.md",
}


def _walk_tree(
    b: Backend,
    rel: str,
    gi: Optional[pathspec.PathSpec],
    max_depth: int,
    focus_path: Optional[str],
    depth: int = 0,
) -> List[Dict[str, Any]]:
    """Recursively walk the directory tree via Backend, collecting entries."""
    target = rel if rel else "."
    try:
        entries = b.list_dir(target)
    except Exception:
        return []

    dirs_out: List[Dict[str, Any]] = []
    files_out: List[Dict[str, Any]] = []

    for e in sorted(entries, key=lambda x: x.get("name", "")):
        name = e.get("name", "")
        if not name:
            continue
        if name.startswith(".") and name not in (".env.example",):
            continue
        is_dir = e.get("type") == "directory"
        child_rel = (rel + "/" + name) if rel else name

        if _is_ignored(child_rel, name, is_dir, gi):
            continue

        if is_dir:
            on_focus = focus_path and (
                focus_path.startswith(child_rel + "/") or focus_path == child_rel
            )
            if depth >= max_depth and not on_focus:
                try:
                    sub_entries = b.list_dir(child_rel)
                    count = len([se for se in sub_entries if not se.get("name", "").startswith(".")])
                except Exception:
                    count = 0
                dirs_out.append({"name": name, "rel": child_rel, "type": "directory",
                                 "collapsed": True, "file_count": count})
            else:
                children = _walk_tree(b, child_rel, gi, max_depth, focus_path, depth + 1)
                child_file_count = sum(
                    1 for c in children if c["type"] == "file"
                ) + sum(
                    c.get("file_count", 0) for c in children if c["type"] == "directory"
                )

                should_collapse = (
                    len(children) > 50
                    and not on_focus
                    and depth > 0
                )

                if should_collapse:
                    dirs_out.append({"name": name, "rel": child_rel, "type": "directory",
                                     "collapsed": True, "file_count": child_file_count})
                else:
                    dirs_out.append({"name": name, "rel": child_rel, "type": "directory",
                                     "children": children, "file_count": child_file_count})
        else:
            _, ext = os.path.splitext(name)
            if ext in _ALWAYS_SKIP_EXTENSIONS:
                continue
            size = e.get("size", 0)
            files_out.append({"name": name, "rel": child_rel, "type": "file", "size": size})

    return dirs_out + files_out


def _render_tree(entries: List[Dict[str, Any]], indent: int = 0, lines: Optional[List[str]] = None,
                 char_budget: int = 8000) -> List[str]:
    """Render tree entries into compact indented text lines, respecting a char budget."""
    if lines is None:
        lines = []
    prefix = "  " * indent
    for e in entries:
        if sum(len(l) + 1 for l in lines) > char_budget:
            lines.append(f"{prefix}... (truncated — tree exceeds budget)")
            break
        if e["type"] == "directory":
            if e.get("collapsed"):
                count = e.get("file_count", 0)
                lines.append(f"{prefix}{e['name']}/ ({count} items)")
            else:
                children = e.get("children", [])
                count = e.get("file_count", 0)
                if count > 0:
                    lines.append(f"{prefix}{e['name']}/ ({count} files)")
                else:
                    lines.append(f"{prefix}{e['name']}/")
                _render_tree(children, indent + 1, lines, char_budget)
        else:
            size = e.get("size", 0)
            name = e["name"]
            if name in _KEY_CONFIG_FILES:
                lines.append(f"{prefix}{name} ({_format_size(size)})")
            elif size > 100_000:
                lines.append(f"{prefix}{name} ({_format_size(size)})")
            else:
                lines.append(f"{prefix}{name}")
    return lines


def project_tree(
    focus_path: Optional[str] = None,
    max_depth: int = 4,
    backend: Optional[Backend] = None,
    working_directory: str = ".",
    **kw: Any,
) -> ToolResult:
    """Build a compact recursive project tree, respecting .gitignore."""
    try:
        b = backend or LocalBackend(working_directory)
        wd = b.working_directory if hasattr(b, 'working_directory') else working_directory
        gi = _load_gitignore(wd, backend=b)

        entries = _walk_tree(b, "", gi, max_depth, focus_path)
        lines = _render_tree(entries, indent=0, char_budget=8000)

        proj_name = os.path.basename(wd) or wd
        header = f"Project: {proj_name}"
        if focus_path:
            header += f"  (focused: {focus_path})"
        output = header + "\n" + "\n".join(lines)
        return ToolResult(success=True, output=output)
    except Exception as e:
        return ToolResult(success=False, output="", error=str(e))


def lint_file(path: str,
              backend: Optional[Backend] = None, working_directory: str = ".", **kw: Any) -> ToolResult:
    """Auto-detect the project linter and run it on a file."""
    from tools.file_ops import _require_path
    err = _require_path(path)
    if err:
        return err
    try:
        b = backend or LocalBackend(working_directory)
        if not b.file_exists(path):
            return ToolResult(success=False, output="", error=f"File not found: {path}")

        _, ext = os.path.splitext(path)
        ext = ext.lower()

        cmd = None

        if ext in (".py", ".pyi"):
            if b.file_exists("pyproject.toml") or b.file_exists("ruff.toml") or b.file_exists(".ruff.toml"):
                cmd = f"ruff check {path} 2>&1 || python -m py_compile {path} 2>&1"
            elif b.file_exists(".flake8") or b.file_exists("setup.cfg"):
                cmd = f"flake8 {path} 2>&1 || python -m py_compile {path} 2>&1"
            else:
                cmd = f"python -m py_compile {path} 2>&1"
        elif ext in (".ts", ".tsx"):
            if b.file_exists("tsconfig.json"):
                cmd = f"npx tsc --noEmit --pretty 2>&1 | head -50"
            elif b.file_exists(".eslintrc.js") or b.file_exists(".eslintrc.json") or b.file_exists("eslint.config.js"):
                cmd = f"npx eslint {path} 2>&1"
        elif ext in (".js", ".jsx", ".mjs"):
            if b.file_exists(".eslintrc.js") or b.file_exists(".eslintrc.json") or b.file_exists("eslint.config.js"):
                cmd = f"npx eslint {path} 2>&1"
            elif b.file_exists("package.json"):
                cmd = f"node --check {path} 2>&1"
        elif ext == ".rs":
            cmd = f"cargo check --message-format=short 2>&1 | head -30"
        elif ext == ".go":
            cmd = f"go vet {path} 2>&1"
        elif ext in (".rb",):
            cmd = f"ruby -c {path} 2>&1"
        elif ext in (".sh", ".bash"):
            cmd = f"bash -n {path} 2>&1"

        if not cmd:
            return ToolResult(success=True, output=f"No linter configured for {ext} files. Skipping.")

        stdout, stderr, rc = b.run_command(cmd, cwd=".", timeout=30)
        output = stdout.strip()
        if stderr and stderr.strip():
            output = f"{output}\n{stderr.strip()}" if output else stderr.strip()

        if rc == 0 and not output:
            return ToolResult(success=True, output=f"No lint issues found in {path}")
        elif rc == 0:
            return ToolResult(success=True, output=output)
        else:
            return ToolResult(success=False, output=output,
                            error=f"Lint check found issues in {path}")
    except Exception as e:
        return ToolResult(success=False, output="", error=str(e))


def _format_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"
