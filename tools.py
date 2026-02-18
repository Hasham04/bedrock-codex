"""
Tool definitions and implementations for the coding agent.
Each tool has an Anthropic-compatible schema and an implementation function.
Tools use a Backend abstraction for file/command operations (local or SSH).
"""

import os
import json
import logging
import re
import ast
import contextvars
import difflib
import pathlib
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, Any, List, Optional, Set, Tuple

import pathspec

from backend import Backend, LocalBackend

# Context for current session todos (set by agent before tool runs; used by TodoRead when called via execute_tool)
_current_todos_ctx: contextvars.ContextVar[List[Dict[str, Any]]] = contextvars.ContextVar("current_todos", default=[])


def set_current_todos(todos: List[Dict[str, Any]]) -> None:
    """Set the current session todo list for TodoRead when called from execute_tool (e.g. agent context)."""
    _current_todos_ctx.set(list(todos) if todos is not None else [])

logger = logging.getLogger(__name__)


@dataclass
class ToolResult:
    """Result from executing a tool"""
    success: bool
    output: str
    error: Optional[str] = None


# ============================================================
# .gitignore-aware filtering
# ============================================================

_ALWAYS_SKIP_DIRS: Set[str] = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    ".mypy_cache", ".pytest_cache", ".tox", ".eggs",
    "dist", "build", ".next", ".nuxt", ".cache",
    "coverage", ".coverage", "htmlcov", ".bedrock-codex",
}

_ALWAYS_SKIP_EXTENSIONS: Set[str] = {
    ".pyc", ".pyo", ".so", ".dylib", ".o", ".a", ".class",
    ".min.js", ".min.css", ".map", ".lock",
}

_gitignore_cache: Dict[str, Optional[pathspec.PathSpec]] = {}


def _load_gitignore(working_directory: str, backend: Optional[Backend] = None) -> Optional[pathspec.PathSpec]:
    """Load and cache .gitignore patterns for a project root.

    Works with both local and SSH backends. If backend is provided and is an
    SSHBackend, reads .gitignore over SSH. Otherwise reads from local filesystem.

    Returns a PathSpec matcher or None if no .gitignore exists.
    """
    if working_directory in _gitignore_cache:
        return _gitignore_cache[working_directory]

    spec = None
    try:
        if backend is not None and getattr(backend, "_host", None) is not None:
            # SSH: read .gitignore via backend
            try:
                content = backend.read_file(".gitignore")
                if content:
                    spec = pathspec.PathSpec.from_lines("gitwildmatch", content.splitlines())
            except Exception:
                pass
        else:
            # Local filesystem
            gitignore_path = os.path.join(working_directory, ".gitignore")
            if os.path.isfile(gitignore_path):
                with open(gitignore_path, "r", encoding="utf-8", errors="replace") as f:
                    spec = pathspec.PathSpec.from_lines("gitwildmatch", f)
    except Exception as e:
        logger.debug(f"Failed to parse .gitignore: {e}")

    _gitignore_cache[working_directory] = spec
    return spec


def _is_ignored(rel_path: str, name: str, is_dir: bool,
                gitignore_spec: Optional[pathspec.PathSpec]) -> bool:
    """Check if a path should be ignored based on .gitignore + hardcoded skips."""
    if name in _ALWAYS_SKIP_DIRS and is_dir:
        return True
    if not is_dir:
        _, ext = os.path.splitext(name)
        if ext in _ALWAYS_SKIP_EXTENSIONS:
            return True
    if gitignore_spec:
        check_path = rel_path + "/" if is_dir else rel_path
        if gitignore_spec.match_file(check_path):
            return True
    return False


def invalidate_gitignore_cache(working_directory: Optional[str] = None) -> None:
    """Clear cached .gitignore specs. Call when .gitignore changes."""
    if working_directory:
        _gitignore_cache.pop(working_directory, None)
    else:
        _gitignore_cache.clear()


# ============================================================
# Tool Implementations — all accept a `backend` parameter
# ============================================================

def _extract_structure(lines: List[str]) -> str:
    """Extract a structural summary from source code: imports, classes, functions."""
    structure = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")) and i < 50:
            structure.append(f"{i+1:6}|{line.rstrip()}")
        elif stripped.startswith(("class ", "def ", "async def ")):
            structure.append(f"{i+1:6}|{line.rstrip()}")
        elif stripped.startswith("@") and i + 1 < len(lines):
            next_stripped = lines[i + 1].strip()
            if next_stripped.startswith(("class ", "def ", "async def ")):
                structure.append(f"{i+1:6}|{line.rstrip()}")
    return "\n".join(structure)


_MAX_FULL_READ_LINES = 500

# ── File content cache (avoids re-reading unchanged files within same session) ──
_file_content_cache: Dict[str, Tuple[float, str]] = {}  # path -> (mtime_or_time, content)
_FILE_CACHE_TTL = 5.0  # seconds; short enough to catch agent writes


def _cached_read(path: str, b: Backend) -> str:
    """Read file via backend with short-lived cache to avoid duplicate reads."""
    import time as _time
    now = _time.time()
    cached = _file_content_cache.get(path)
    if cached and (now - cached[0]) < _FILE_CACHE_TTL:
        return cached[1]
    content = b.read_file(path)
    _file_content_cache[path] = (now, content)
    return content


def invalidate_file_cache(path: Optional[str] = None) -> None:
    """Invalidate read cache after writes/edits. Call from write/edit tools."""
    if path:
        _file_content_cache.pop(path, None)
    else:
        _file_content_cache.clear()


def _require_path(path: str, name: str = "path") -> Optional[ToolResult]:
    """Return an error ToolResult if path is empty/whitespace; else None."""
    if not (path or "").strip():
        return ToolResult(success=False, output="", error=f"{name} is required")
    return None


def read_file(path: str, offset: Optional[int] = None, limit: Optional[int] = None,
              backend: Optional[Backend] = None, working_directory: str = ".") -> ToolResult:
    """Read the contents of a file. Returns line-numbered content."""
    err = _require_path(path)
    if err:
        return err
    try:
        b = backend or LocalBackend(working_directory)
        full_path = b.resolve_path(path)

        if not b.file_exists(path) and not b.file_exists(full_path):
            return ToolResult(success=False, output="", error=f"File not found: {path}")

        content = _cached_read(path, b)
        lines = content.splitlines(keepends=True)
        total_lines = len(lines)

        if offset is not None or limit is not None:
            start = max((offset or 1) - 1, 0)
            end = start + (limit or total_lines)
            selected = lines[start:end]
            line_start = start + 1
            numbered = [f"{line_start + i:6}|{line.rstrip()}" for i, line in enumerate(selected)]
            header = f"[{total_lines} lines total] (showing lines {line_start}-{line_start + len(selected) - 1})"
            return ToolResult(success=True, output=header + "\n" + "\n".join(numbered))

        if total_lines <= _MAX_FULL_READ_LINES:
            numbered = [f"{i+1:6}|{line.rstrip()}" for i, line in enumerate(lines)]
            return ToolResult(success=True, output=f"[{total_lines} lines total]\n" + "\n".join(numbered))

        # Large file — structural overview + head + tail
        structure = _extract_structure(lines)
        head_n, tail_n = 80, 40
        omitted = total_lines - head_n - tail_n
        head = [f"{i+1:6}|{lines[i].rstrip()}" for i in range(min(head_n, total_lines))]
        tail = [f"{total_lines - tail_n + i + 1:6}|{lines[total_lines - tail_n + i].rstrip()}" for i in range(tail_n)]
        parts = [
            f"[{total_lines} lines total — file is large, showing overview + head + tail]",
            "[Use offset/limit to read specific sections]", "",
            "── structure (classes, functions, imports) ──", structure, "",
            f"── first {head_n} lines ──", "\n".join(head),
            f"\n  ... ({omitted} lines omitted — use offset={head_n + 1} limit=N to read more) ...\n",
            f"── last {tail_n} lines ──", "\n".join(tail),
        ]
        return ToolResult(success=True, output="\n".join(parts))
    except Exception as e:
        return ToolResult(success=False, output="", error=str(e))


def _compact_diff(old_content: str, new_content: str, path: str, max_lines: int = 60) -> str:
    """Generate a compact unified diff for display in the tool panel."""
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff = list(difflib.unified_diff(old_lines, new_lines, fromfile=path, tofile=path, lineterm=""))
    if not diff:
        return ""
    # Truncate if too long
    if len(diff) > max_lines:
        diff = diff[:max_lines] + [f"... ({len(diff) - max_lines} more diff lines)"]
    return "\n".join(line.rstrip() for line in diff)


def write_file(path: str, content: str,
               backend: Optional[Backend] = None, working_directory: str = ".") -> ToolResult:
    """Create a new file or completely overwrite an existing file."""
    err = _require_path(path)
    if err:
        return err
    try:
        b = backend or LocalBackend(working_directory)
        old_content = ""
        is_new = True
        try:
            if b.file_exists(path):
                old_content = b.read_file(path)
                is_new = False
        except Exception:
            pass
        b.write_file(path, content)
        invalidate_file_cache(path)
        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        summary = f"{'Created' if is_new else 'Wrote'} {line_count} lines to {path}"
        if is_new:
            # For new files, show first few lines as a diff
            preview_lines = content.splitlines()[:30]
            diff_text = f"--- /dev/null\n+++ {path}\n@@ -0,0 +1,{len(preview_lines)} @@\n"
            diff_text += "\n".join(f"+{l}" for l in preview_lines)
            if len(content.splitlines()) > 30:
                diff_text += f"\n+... ({len(content.splitlines()) - 30} more lines)"
            return ToolResult(success=True, output=f"{summary}\n{diff_text}")
        else:
            diff_text = _compact_diff(old_content, content, path)
            if diff_text:
                return ToolResult(success=True, output=f"{summary}\n{diff_text}")
            return ToolResult(success=True, output=summary)
    except Exception as e:
        return ToolResult(success=False, output="", error=str(e))


def edit_file(path: str, old_string: str, new_string: str,
              backend: Optional[Backend] = None, working_directory: str = ".",
              replace_all: bool = False) -> ToolResult:
    """Replace an exact string in a file. By default must match exactly one location.
    With replace_all=True, replaces every occurrence (useful for renames)."""
    err = _require_path(path)
    if err:
        return err
    try:
        b = backend or LocalBackend(working_directory)
        if not b.file_exists(path):
            return ToolResult(success=False, output="", error=f"File not found: {path}")
        content = b.read_file(path)
        count = content.count(old_string)
        if count == 0:
            return ToolResult(success=False, output="",
                error=f"old_string not found in {path}. Ensure it matches exactly, including whitespace and indentation. Re-read the file to see current content — it may have changed.")
        if count > 1 and not replace_all:
            return ToolResult(success=False, output="",
                error=f"Found {count} occurrences of old_string in {path}. Add more surrounding context to make it unique, or set replace_all=true to replace all {count} occurrences.")
        if replace_all:
            new_content = content.replace(old_string, new_string)
            replaced = count
        else:
            new_content = content.replace(old_string, new_string, 1)
            replaced = 1
        b.write_file(path, new_content)
        invalidate_file_cache(path)
        diff_text = _compact_diff(content, new_content, path)
        summary = f"Applied edit to {path}" + (f" ({replaced} replacements)" if replaced > 1 else "")
        if diff_text:
            return ToolResult(success=True, output=f"{summary}\n{diff_text}")
        return ToolResult(success=True, output=summary)
    except Exception as e:
        return ToolResult(success=False, output="", error=str(e))


def _python_symbol_spans(content: str, symbol: str, kind: str = "all") -> List[tuple]:
    """Return (start_line_1idx, end_line_1idx) spans for python symbols."""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []
    spans: List[tuple] = []
    for node in ast.walk(tree):
        is_func = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        is_class = isinstance(node, ast.ClassDef)
        if not (is_func or is_class):
            continue
        if getattr(node, "name", "") != symbol:
            continue
        if kind == "function" and not is_func:
            continue
        if kind == "class" and not is_class:
            continue
        start = getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", None) or start
        if start and end:
            spans.append((int(start), int(end)))
    return spans


def _regex_symbol_spans(content: str, symbol: str, kind: str = "all") -> List[tuple]:
    """Fallback symbol span finder using language-aware regex anchors."""
    sym = re.escape(symbol)
    patterns: List[str] = []
    if kind in ("all", "function"):
        patterns.extend([
            rf"^\s*(?:export\s+)?(?:async\s+)?function\s+{sym}\s*\(",
            rf"^\s*(?:export\s+)?(?:const|let|var)\s+{sym}\s*=\s*(?:async\s*)?\(",
            rf"^\s*def\s+{sym}\s*\(",
            rf"^\s*async\s+def\s+{sym}\s*\(",
        ])
    if kind in ("all", "class"):
        patterns.extend([
            rf"^\s*class\s+{sym}\b",
            rf"^\s*interface\s+{sym}\b",
            rf"^\s*type\s+{sym}\b",
            rf"^\s*struct\s+{sym}\b",
            rf"^\s*enum\s+{sym}\b",
            rf"^\s*trait\s+{sym}\b",
        ])
    lines = content.splitlines()
    spans: List[tuple] = []
    for i, line in enumerate(lines):
        if not any(re.search(p, line) for p in patterns):
            continue
        base_indent = len(line) - len(line.lstrip(" "))
        end = i + 1
        j = i + 1
        while j < len(lines):
            ln = lines[j]
            if ln.strip() == "":
                end = j + 1
                j += 1
                continue
            indent = len(ln) - len(ln.lstrip(" "))
            if indent <= base_indent and not ln.lstrip().startswith(("@", "#")):
                break
            end = j + 1
            j += 1
        spans.append((i + 1, end))
    return spans


def symbol_edit(path: str, symbol: str, new_string: str, kind: str = "all", occurrence: int = 1,
                backend: Optional[Backend] = None, working_directory: str = ".") -> ToolResult:
    """Edit a symbol definition block using AST/tree-sitter/regex boundaries."""
    err = _require_path(path)
    if err:
        return err
    try:
        b = backend or LocalBackend(working_directory)
        if not b.file_exists(path):
            return ToolResult(success=False, output="", error=f"File not found: {path}")
        if not symbol.strip():
            return ToolResult(success=False, output="", error="symbol is required")

        content = b.read_file(path)
        ext = os.path.splitext(path.lower())[1]
        kind = (kind or "all").lower()
        spans: List[tuple] = []

        # Python first-class AST handling
        if ext == ".py":
            spans = _python_symbol_spans(content, symbol.strip(), kind=kind)

        # Optional tree-sitter path for non-python if available
        if not spans and ext in (".js", ".jsx", ".ts", ".tsx"):
            try:
                from tree_sitter_languages import get_language, get_parser  # type: ignore
                lang_name = "typescript" if ext in (".ts", ".tsx") else "javascript"
                _ = get_language(lang_name)  # Ensure language exists
                parser = get_parser(lang_name)
                tree = parser.parse(bytes(content, "utf-8"))
                root = tree.root_node
                target = symbol.strip()
                stack = [root]
                while stack:
                    node = stack.pop()
                    if node.type in {
                        "function_declaration", "class_declaration", "method_definition",
                        "lexical_declaration", "variable_declaration", "interface_declaration",
                        "type_alias_declaration", "enum_declaration",
                    }:
                        node_text = content[node.start_byte:node.end_byte]
                        if re.search(rf"\b{re.escape(target)}\b", node_text):
                            spans.append((node.start_point[0] + 1, node.end_point[0] + 1))
                    stack.extend(list(node.children))
                spans = list(dict.fromkeys(spans))
            except Exception:
                spans = []

        # Regex fallback
        if not spans:
            spans = _regex_symbol_spans(content, symbol.strip(), kind=kind)

        if not spans:
            return ToolResult(success=False, output="", error=f"Symbol '{symbol}' not found in {path}")

        if occurrence < 1:
            occurrence = 1
        if occurrence > len(spans):
            return ToolResult(success=False, output="", error=f"occurrence {occurrence} out of range (found {len(spans)} matches)")

        start, end = spans[occurrence - 1]
        lines = content.splitlines(keepends=True)
        before = "".join(lines[:start - 1])
        after = "".join(lines[end:])
        replacement = new_string
        if replacement and not replacement.endswith("\n"):
            replacement += "\n"
        new_content = before + replacement + after
        b.write_file(path, new_content)
        invalidate_file_cache(path)
        diff_text = _compact_diff(content, new_content, path)
        summary = f"Applied symbol_edit to {path} ({symbol}, lines {start}-{end})"
        if diff_text:
            return ToolResult(success=True, output=f"{summary}\n{diff_text}")
        return ToolResult(success=True, output=summary)
    except Exception as e:
        return ToolResult(success=False, output="", error=str(e))


def run_command(command: str, timeout: int = 30,
                backend: Optional[Backend] = None, working_directory: str = ".") -> ToolResult:
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

        # Truncate long output
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


def search(pattern: str, path: Optional[str] = None, include: Optional[str] = None,
           backend: Optional[Backend] = None, working_directory: str = ".") -> ToolResult:
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
                backend: Optional[Backend] = None, working_directory: str = ".") -> ToolResult:
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
            # Deduplicate while preserving order
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
                   backend: Optional[Backend] = None, working_directory: str = ".") -> ToolResult:
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
              backend: Optional[Backend] = None, working_directory: str = ".") -> ToolResult:
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


# ============================================================
# project_tree — recursive, token-budgeted, .gitignore-aware
# ============================================================

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
    """Recursively walk the directory tree via Backend, collecting entries.

    Works with both LocalBackend and SSHBackend.
    Returns a list of dicts: {name, rel, type, children?, file_count?, size?}.
    Large directories (>50 visible entries) are collapsed unless they are on
    the focus_path.
    """
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
) -> ToolResult:
    """Build a compact recursive project tree, respecting .gitignore.

    Works with both LocalBackend and SSHBackend.
    Token-budgeted: output is capped at ~2000 tokens (~8000 chars).
    Large directories (>50 entries) are collapsed unless they are on the focus_path.
    """
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
              backend: Optional[Backend] = None, working_directory: str = ".") -> ToolResult:
    """Auto-detect the project linter and run it on a file."""
    err = _require_path(path)
    if err:
        return err
    try:
        b = backend or LocalBackend(working_directory)
        if not b.file_exists(path):
            return ToolResult(success=False, output="", error=f"File not found: {path}")

        _, ext = os.path.splitext(path)
        ext = ext.lower()

        # Detect available lint/check commands based on project files
        cmd = None

        if ext in (".py", ".pyi"):
            # Python: try ruff, then flake8, then py_compile
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


# ============================================================
# Tool Schemas (Bedrock/Anthropic Messages API)
# ============================================================
# Custom tools use type "custom" per Bedrock docs. Bedrock also supports
# built-in tools (bash_20241022, text_editor_20241022, computer_20241022)
# with anthropic_beta "computer-use-2024-10-22"; we use custom tools
# for full control (read_file, edit_file, run_command, etc.).

TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    # SDK name: Read (implementation: read_file)
    {
        "type": "custom",
        "name": "Read",
        "description": "Read the contents of a file with line numbers. You MUST read a file before editing it — never propose changes to code you haven't read. For large files (>500 lines), returns a structural overview (imports, classes, functions) plus head/tail; use offset and limit to read specific sections. Batch multiple file reads in ONE request (5-12 files) — they execute in parallel. It is okay to read a file that does not exist; an error will be returned. After semantic_retrieve results, use targeted reads with offset/limit on the returned file paths. NEVER use cat, head, or tail via Bash — always use this tool for reading files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to working directory. If the file does not exist, returns an error."},
                "offset": {"type": "integer", "description": "Starting line number (1-indexed). Use with limit for targeted reading of large files."},
                "limit": {"type": "integer", "description": "Number of lines to read from offset. Combine with offset for windowed reads."},
            },
            "required": ["path"],
        },
    },
    # SDK name: Write (implementation: write_file)
    {
        "type": "custom",
        "name": "Write",
        "description": "Create a new file or completely overwrite an existing file. Shows a diff of changes for existing files. ALWAYS prefer Edit for partial modifications — only use Write for NEW files or when more than 50% of the content changes. NEVER proactively create documentation files (README, CHANGELOG) unless explicitly asked. NEVER write files that contain secrets (.env, credentials). After writing, run lint_file to verify no syntax errors. Creates parent directories automatically if needed. NEVER use echo/heredoc via Bash to create files — always use this tool.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to working directory. Parent directories are created if needed."},
                "content": {"type": "string", "description": "The full content to write. For existing files, a diff is shown."},
            },
            "required": ["path", "content"],
        },
    },
    # SDK name: Edit (implementation: edit_file)
    {
        "type": "custom",
        "name": "Edit",
        "description": "Make a targeted edit by replacing an exact string in a file. The old_string must match EXACTLY one location including all whitespace and indentation. Include 3-5 lines of surrounding context to ensure uniqueness. If it fails with 'multiple occurrences', either add more context lines OR set replace_all=true to replace every occurrence (ideal for renames). If it fails with 'not found', re-read the file — content may have changed. After editing, run lint_file to verify. NEVER use sed/awk — always use this tool.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (relative to working directory)"},
                "old_string": {"type": "string", "description": "The exact string to find (must be unique unless replace_all is true)"},
                "new_string": {"type": "string", "description": "The replacement string"},
                "replace_all": {"type": "boolean", "description": "If true, replace ALL occurrences of old_string in the file. Use for renames and bulk replacements. Default: false."},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "type": "custom",
        "name": "symbol_edit",
        "description": "Perform a symbol-aware edit of a function/class/type definition block using AST parsing (Python) or tree-sitter (JS/TS) with regex fallback. Safer than plain Edit for replacing entire function or class bodies. Use kind=function|class|all to narrow the match. Use occurrence when multiple symbols share the same name (e.g. overloaded methods). Always read the file first to verify the symbol exists.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (relative to working directory)"},
                "symbol": {"type": "string", "description": "Symbol name to edit, e.g. 'process_data'"},
                "new_string": {"type": "string", "description": "Full replacement block for the symbol definition"},
                "kind": {"type": "string", "description": "all|function|class (default: all)"},
                "occurrence": {"type": "integer", "description": "1-based match index if multiple symbols share same name"},
            },
            "required": ["path", "symbol", "new_string"],
        },
    },
    # SDK name: Bash (implementation: run_command)
    {
        "type": "custom",
        "name": "Bash",
        "description": "Execute a shell command in the working directory. Use for: running tests, installing packages, git operations, builds, and system commands that have no dedicated tool. Always check both stdout and stderr. Non-zero exit codes indicate failure — diagnose the error rather than retrying blindly. NEVER use Bash for file operations that have dedicated tools: use Read (not cat/head/tail), Edit (not sed/awk), Write (not echo/heredoc), search (not grep/rg), Glob (not find). When chaining dependent commands, use && (not ;). Set timeout for long-running processes. Output is capped at 20K chars with head/tail preserved.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to execute. Use && to chain dependent commands."},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default: 30). Increase for builds, test suites."},
            },
            "required": ["command"],
        },
    },
    # Claude Agent SDK built-in: TodoWrite — same tool name and schema as the SDK.
    {
        "type": "custom",
        "name": "TodoWrite",
        "description": "Create or update the task checklist for this session. Use proactively for any multi-step task (3+ steps). Create the full checklist at the start with all items 'pending'. Set exactly one item to 'in_progress' at a time. Mark items 'completed' immediately when done — don't batch completions. Add discovered work as new items. Replaces the previous list each time (include ALL items, not just changed ones). Skip for trivial single-step tasks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "One-line description of the task"},
                            "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"], "description": "Current status"},
                            "id": {"type": "string", "description": "Optional stable id (e.g. 1, 2); omit to use index."},
                        },
                        "required": ["content", "status"],
                    },
                    "description": "Full list of todos; replaces previous list.",
                },
            },
            "required": ["todos"],
        },
    },
    # Claude Agent SDK: TodoRead — get current todo list (handled in agent, not execute_tool).
    {
        "type": "custom",
        "name": "TodoRead",
        "description": "Get the current task checklist for this session. Call when resuming work, before planning next steps, or when unsure what remains. Returns the full list maintained by TodoWrite with id, content, and status for each item.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    # Memory: store and retrieve facts across the conversation (handled in agent).
    {
        "type": "custom",
        "name": "MemoryWrite",
        "description": "Store an important fact for the rest of this session. Use when you learn: user preferences (e.g. 'preferred_language: TypeScript'), project conventions, key architectural decisions, important error patterns and fixes, or environment facts. Key: short identifier (e.g. 'api_base', 'test_cmd'). Value: concise string. Overwrites existing key. Do NOT store trivial or one-off details. Write proactively when the information would improve your future responses.",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Short identifier for the fact (e.g. 'preferred_language', 'api_base')"},
                "value": {"type": "string", "description": "The fact or value to store (keep concise; very long values may be truncated)"},
            },
            "required": ["key", "value"],
        },
    },
    {
        "type": "custom",
        "name": "MemoryRead",
        "description": "Retrieve stored session facts. Call with no key to get all stored facts; with a specific key to get one value. Use at the start of follow-up tasks, when resuming after context, or when stored preferences/decisions could change your approach.",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Optional: specific key to read. Omit to return all stored facts."},
            },
            "required": [],
        },
    },
    {
        "type": "custom",
        "name": "semantic_retrieve",
        "description": "Semantic codebase search — finds code by meaning, not exact text. Returns the most relevant code chunks (functions, classes) ranked by semantic similarity. START HERE for code discovery and exploration. Ask complete questions: 'where is user authentication validated?', 'how are database connections pooled?', 'what happens when a payment fails?'. Then use Read with offset/limit on returned paths for full context. Much more effective than grep for understanding; use search() only for exact strings or regex patterns. Cost-efficient: queries a pre-built embedding index.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language description of what you are looking for, e.g. 'where is user authentication validated' or 'handler for POST /api/orders'"},
                "top_k": {"type": "integer", "description": "Number of chunks to return (default 10, max 20)"},
            },
            "required": ["query"],
        },
    },
    {
        "type": "custom",
        "name": "search",
        "description": "Regex search (ripgrep) across files. Use ONLY for exact strings, regex patterns, error messages, TODOs, or specific identifiers. For 'where/how' questions, use semantic_retrieve instead. Returns matching lines with file paths and line numbers. Use `include` to filter by file type (e.g. '*.py'). Fast and token-efficient. NEVER use grep/rg via Bash — always use this tool.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
                "path": {"type": "string", "description": "Directory or file to search in (default: working directory)"},
                "include": {"type": "string", "description": "Glob to filter files, e.g. '*.py', '*.ts'"},
            },
            "required": ["pattern"],
        },
    },
    {
        "type": "custom",
        "name": "find_symbol",
        "description": "Symbol-aware navigation — finds definitions and references using language-aware patterns (Python, JS/TS, Java, Rust, Go). Use BEFORE editing ambiguous symbols to see all locations where a symbol is defined or used. Essential for safe refactoring: shows every call site before you rename or modify. Use kind='definition' to find where it's declared, kind='reference' for usages, kind='all' for both.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Symbol name, e.g. 'AuthService' or 'validate_user'"},
                "kind": {"type": "string", "description": "One of: all|definition|reference (default: all)"},
                "path": {"type": "string", "description": "Directory or file to search in (default: working directory)"},
                "include": {"type": "string", "description": "Glob filter, e.g. '*.py' or '*.{ts,tsx}'"},
            },
            "required": ["symbol"],
        },
    },
    {
        "type": "custom",
        "name": "list_directory",
        "description": "List files and directories at a given path with file sizes. Respects .gitignore. For understanding overall project structure, prefer project_tree (gives a full recursive view). Use list_directory for examining one specific directory's contents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path (default: working directory)"},
            },
            "required": [],
        },
    },
    # SDK name: Glob (implementation: glob_find)
    {
        "type": "custom",
        "name": "Glob",
        "description": "Find files matching a glob pattern recursively. Examples: '**/*.py' (all Python files), 'src/**/*.test.ts' (all test files in src). Respects .gitignore and skips node_modules, __pycache__, .git, etc. Use for discovering files before reading them. NEVER use find via Bash — always use this tool. Results capped at 200 matches.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py' or 'src/**/*.ts'"},
            },
            "required": ["pattern"],
        },
    },
    {
        "type": "custom",
        "name": "project_tree",
        "description": "Get a compact recursive project tree respecting .gitignore. Token-budgeted (~2000 tokens). Use as your FIRST step on any new or unfamiliar codebase — gives a full structural overview. Large directories (>50 entries) are collapsed to 'dir/ (N items)'. Use focus_path to expand a specific subtree while collapsing siblings (e.g. focus_path='src/api' to drill into the API layer). Prefer this over repeated list_directory calls for orientation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "focus_path": {"type": "string", "description": "Optional: expand this subtree fully while collapsing siblings. E.g. 'src/main/java' or 'backend/api'."},
                "max_depth": {"type": "integer", "description": "Max directory depth to expand (default: 4). Deeper dirs are collapsed."},
            },
            "required": [],
        },
    },
    {
        "type": "custom",
        "name": "lint_file",
        "description": "Auto-detect and run the project's linter/type-checker on a file. Supports: ruff/flake8/py_compile (Python), tsc (TypeScript), eslint (JavaScript), cargo check (Rust), go vet (Go), ruby -c (Ruby), bash -n (Shell). Run after EVERY edit to catch syntax errors, type errors, and style violations before moving on. If linting reveals errors you introduced, fix them immediately.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to lint (relative to working directory)"},
            },
            "required": ["path"],
        },
    },
    {
        "type": "custom",
        "name": "WebFetch",
        "description": "Fetch content from a URL (HTTP GET). Use to verify information: confirm API shapes, check official docs, validate error messages, or get the latest version info. Returns plain text with HTML stripped. Large responses truncated at 500KB. Do NOT use for authenticated endpoints or URLs that require login. The URL must be fully formed (https://...).",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full URL to fetch (e.g. https://example.com/docs)"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default: 15, max 60)"},
            },
            "required": ["url"],
        },
    },
    {
        "type": "custom",
        "name": "WebSearch",
        "description": "Search the web for current information. Use when you need up-to-date docs, error messages, library APIs, or general lookup that may not be in your training data. Returns relevant snippets and links. Be specific in queries — include version numbers and dates when relevant. Prefer WebFetch when you have a specific known URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (e.g. 'python asyncio timeout best practice')"},
                "max_results": {"type": "integer", "description": "Max results to return (default 5, max 10)"},
            },
            "required": ["query"],
        },
    },
]

def semantic_retrieve(
    query: str,
    top_k: int = 10,
    backend: Optional[Backend] = None,
    working_directory: str = ".",
) -> ToolResult:
    """Semantic search over the codebase index. Returns relevant code chunks with path and line range.
    Works for both local and SSH projects (SSH index is cached locally)."""
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


# --- WebFetch: fetch URL content (no backend/working_directory needed) ---
_WEB_FETCH_MAX_BYTES = 500_000  # ~500KB cap to avoid token explosion
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
        # Rough strip of HTML tags for readability
        text = re.sub(r"<script[^>]*>[\s\S]*?</script>", "", text, flags=re.IGNORECASE)
        text = re.sub(r"<style[^>]*>[\s\S]*?</style>", "", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        if truncated:
            text += "\n\n[Content truncated — response was larger than 500KB.]"
        return ToolResult(success=True, output=text[:100_000], error=None)  # cap output chars too
    except urllib.error.HTTPError as e:
        return ToolResult(success=False, output="", error=f"HTTP {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        return ToolResult(success=False, output="", error=f"URL error: {e.reason}")
    except Exception as e:
        logger.exception("web_fetch failed")
        return ToolResult(success=False, output="", error=str(e))


# --- WebSearch: optional duckduckgo-search ---
def todo_write(todos: list, **kwargs: Any) -> ToolResult:
    """Create or update the task checklist for this session. Use at the start of multi-step tasks: list items with status pending, then set in_progress when working on one and completed when done. Only one task should be in_progress at a time. Replaces the previous list each time. Keeps progress visible and ensures nothing is dropped."""
    try:
        # Validate todos structure
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
    """Get the current task checklist for this session. Uses agent/session context when available."""
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


# Map API/implementation names to canonical tool names (e.g. read_file → Read)
TOOL_NAME_NORMALIZE = {
    "read_file": "Read",
    "write_file": "Write",
    "edit_file": "Edit",
    "glob_find": "Glob",
    "run_command": "Bash",
}


TOOL_IMPLEMENTATIONS = {
    # SDK names (model calls these)
    "Read": read_file,
    "Write": write_file,
    "Edit": edit_file,
    "Bash": run_command,
    "Glob": glob_find,
    "symbol_edit": symbol_edit,
    "search": search,
    "find_symbol": find_symbol,
    "list_directory": list_directory,
    "project_tree": project_tree,
    "lint_file": lint_file,
    "semantic_retrieve": semantic_retrieve,
    "WebFetch": web_fetch,
    "WebSearch": web_search,
    "TodoWrite": todo_write,
    "TodoRead": todo_read,
}

TOOLS_REQUIRING_APPROVAL = {"Write", "Edit", "symbol_edit", "Bash"}
SAFE_TOOLS = {"Read", "search", "find_symbol", "list_directory", "project_tree", "Glob", "lint_file", "semantic_retrieve", "WebFetch", "WebSearch", "TodoWrite", "TodoRead"}
SCOUT_TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    t for t in TOOL_DEFINITIONS if t["name"] in SAFE_TOOLS
]

# Claude Agent SDK built-in: AskUserQuestion (we use this name so the model uses the same tool as in the SDK).
# Handled via callback, not execute_tool; we add options and UI.
ASK_USER_QUESTION_DEFINITION: Dict[str, Any] = {
    "type": "custom",
    "name": "AskUserQuestion",
    "description": "Ask the user a structured clarifying question. Use when: (1) the task is genuinely ambiguous and you cannot infer the answer, (2) verification fails due to a conflict with what the user explicitly asked for, (3) there are multiple valid approaches with significantly different outcomes. Do NOT ask when you can reasonably infer the answer. ALWAYS provide an 'options' array with 2-5 short choices when possible — structured choices are faster for the user than typing. The user can still type a custom answer if none of the options fit.",
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "Clear, concise question. State what you need to know and why."},
            "context": {"type": "string", "description": "Brief context explaining why you're asking (what you found, what the conflict is)."},
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "2-5 short answer choices. ALWAYS provide options when the question has a finite set of answers. Examples: ['Keep existing tests', 'Rewrite tests', 'Skip tests'] or ['Python 3.10+', 'Python 3.8 compatible']. User can still type a custom answer.",
                "minItems": 2,
            },
        },
        "required": ["question"],
    },
}


def execute_tool(
    name: str,
    inputs: Dict[str, Any],
    working_directory: str = ".",
    backend: Optional[Backend] = None,
    *,
    extra_context: Optional[Dict[str, Any]] = None,
) -> ToolResult:
    """Execute a tool by name with the given inputs. extra_context can provide e.g. todos for TodoRead."""
    name = TOOL_NAME_NORMALIZE.get(name, name)
    impl = TOOL_IMPLEMENTATIONS.get(name)
    if not impl:
        return ToolResult(success=False, output="", error=f"Unknown tool: {name}")
    kwargs = dict(inputs, working_directory=working_directory, backend=backend)
    if extra_context and name == "TodoRead" and "todos" in extra_context:
        kwargs["todos"] = extra_context["todos"]
    try:
        return impl(**kwargs)
    except TypeError as e:
        return ToolResult(success=False, output="", error=f"Invalid arguments for {name}: {e}")
    except Exception as e:
        logger.exception(f"Tool execution error: {name}")
        return ToolResult(success=False, output="", error=f"Tool error: {e}")


def needs_approval(tool_name: str) -> bool:
    """Check if a tool requires user approval."""
    tool_name = TOOL_NAME_NORMALIZE.get(tool_name, tool_name)
    return tool_name in TOOLS_REQUIRING_APPROVAL
