"""File operation tools: read, write, edit, symbol_edit."""

import os
import re
import ast
import difflib
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from backend import Backend, LocalBackend
from tools._common import ToolResult

logger = logging.getLogger(__name__)


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
              backend: Optional[Backend] = None, working_directory: str = ".", **kw: Any) -> ToolResult:
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
    if len(diff) > max_lines:
        diff = diff[:max_lines] + [f"... ({len(diff) - max_lines} more diff lines)"]
    return "\n".join(line.rstrip() for line in diff)


def write_file(path: str, content: str,
               backend: Optional[Backend] = None, working_directory: str = ".", **kw: Any) -> ToolResult:
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
        try:
            from codebase_index import notify_file_changed_global
            notify_file_changed_global(path, working_directory)
        except Exception:
            pass
        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        summary = f"{'Created' if is_new else 'Wrote'} {line_count} lines to {path}"
        if is_new:
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
              replace_all: bool = False, **kw: Any) -> ToolResult:
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
        try:
            from codebase_index import notify_file_changed_global
            notify_file_changed_global(path, working_directory)
        except Exception:
            pass
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
                backend: Optional[Backend] = None, working_directory: str = ".", **kw: Any) -> ToolResult:
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

        if ext == ".py":
            spans = _python_symbol_spans(content, symbol.strip(), kind=kind)

        if not spans and ext in (".js", ".jsx", ".ts", ".tsx"):
            try:
                from tree_sitter_languages import get_language, get_parser  # type: ignore
                lang_name = "typescript" if ext in (".ts", ".tsx") else "javascript"
                _ = get_language(lang_name)
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
        try:
            from codebase_index import notify_file_changed_global
            notify_file_changed_global(path, working_directory)
        except Exception:
            pass
        diff_text = _compact_diff(content, new_content, path)
        summary = f"Applied symbol_edit to {path} ({symbol}, lines {start}-{end})"
        if diff_text:
            return ToolResult(success=True, output=f"{summary}\n{diff_text}")
        return ToolResult(success=True, output=summary)
    except Exception as e:
        return ToolResult(success=False, output="", error=str(e))
