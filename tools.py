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
from dataclasses import dataclass
from typing import Dict, Any, List, Optional

from backend import Backend, LocalBackend

logger = logging.getLogger(__name__)


@dataclass
class ToolResult:
    """Result from executing a tool"""
    success: bool
    output: str
    error: Optional[str] = None


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

        content = b.read_file(path)
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


def write_file(path: str, content: str,
               backend: Optional[Backend] = None, working_directory: str = ".") -> ToolResult:
    """Create a new file or completely overwrite an existing file."""
    err = _require_path(path)
    if err:
        return err
    try:
        b = backend or LocalBackend(working_directory)
        b.write_file(path, content)
        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return ToolResult(success=True, output=f"Wrote {line_count} lines to {path}")
    except Exception as e:
        return ToolResult(success=False, output="", error=str(e))


def edit_file(path: str, old_string: str, new_string: str,
              backend: Optional[Backend] = None, working_directory: str = ".") -> ToolResult:
    """Replace an exact string in a file (must match exactly one location)."""
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
                error=f"old_string not found in {path}. Ensure it matches exactly, including whitespace and indentation.")
        if count > 1:
            return ToolResult(success=False, output="",
                error=f"Found {count} occurrences of old_string in {path}. Provide more surrounding context to make it unique.")
        new_content = content.replace(old_string, new_string, 1)
        b.write_file(path, new_content)
        return ToolResult(success=True, output=f"Applied edit to {path}")
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
        return ToolResult(success=True, output=f"Applied symbol_edit to {path} ({symbol}, lines {start}-{end})")
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
    """List files and directories at a path."""
    try:
        b = backend or LocalBackend(working_directory)
        target = path or "."

        if not b.is_dir(target):
            return ToolResult(success=False, output="", error=f"Not a directory: {target}")

        entries = b.list_dir(target)
        skip = {"__pycache__", ".git", "node_modules", "venv", ".venv"}
        lines = []
        for e in entries:
            name = e["name"]
            if name in skip:
                lines.append(f"  {name}/ (skipped)")
                continue
            if e["type"] == "directory":
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
    """Find files matching a glob pattern."""
    try:
        b = backend or LocalBackend(working_directory)
        matches = b.glob_find(pattern, ".")

        if not matches:
            return ToolResult(success=True, output="No files found matching pattern.")

        output = f"Found {len(matches)} match(es):\n" + "\n".join(f"  {m}" for m in matches[:200])
        if len(matches) > 200:
            output += f"\n  ... [{len(matches) - 200} more]"
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
        "description": "Read the contents of a file with line numbers. ALWAYS read a file before editing it — understand existing code before suggesting modifications. For large files (>500 lines), returns a structural overview (imports, classes, functions) plus head/tail. Use offset and limit to read specific sections of large files. **Batch multiple files in ONE request** — they run in parallel (5-12 files per turn is optimal). After semantic_retrieve, use targeted reads with offset/limit.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (relative to working directory)"},
                "offset": {"type": "integer", "description": "Starting line number (1-indexed). Optional."},
                "limit": {"type": "integer", "description": "Number of lines to read. Optional."},
            },
            "required": ["path"],
        },
    },
    # SDK name: Write (implementation: write_file)
    {
        "type": "custom",
        "name": "Write",
        "description": "Create a new file or completely overwrite an existing file. Only use for NEW files or when more than 50% of the file changes. For partial modifications, ALWAYS prefer Edit instead. After writing, use lint_file to verify no syntax errors were introduced.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (relative to working directory)"},
                "content": {"type": "string", "description": "The full content to write to the file"},
            },
            "required": ["path", "content"],
        },
    },
    # SDK name: Edit (implementation: edit_file)
    {
        "type": "custom",
        "name": "Edit",
        "description": "Make a targeted edit by replacing an exact string in a file. The old_string must match EXACTLY one location, including all whitespace and indentation. Include 3-5 lines of surrounding context to ensure uniqueness. If it fails with 'multiple occurrences', add more context lines. If it fails with 'not found', re-read the file first to see the current content — it may have changed. After editing, use lint_file to verify no errors were introduced.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (relative to working directory)"},
                "old_string": {"type": "string", "description": "The exact string to find (must be unique in the file, including whitespace)"},
                "new_string": {"type": "string", "description": "The replacement string"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "type": "custom",
        "name": "symbol_edit",
        "description": "Perform a symbol-aware edit of a function/class/type definition block using AST/tree-sitter when available, with regex fallback. Safer than plain string replacement for refactors. Use kind=function|class|all and occurrence to disambiguate.",
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
        "description": "Execute a shell command in the working directory. Use for running tests, installing packages, git operations, builds, linters, and type checkers. Always check both stdout and stderr in the output. Non-zero exit codes indicate failure — diagnose the error rather than retrying blindly. Use timeout for long-running processes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to execute"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default: 30)"},
            },
            "required": ["command"],
        },
    },
    # Claude Agent SDK built-in: TodoWrite — same tool name and schema as the SDK.
    {
        "type": "custom",
        "name": "TodoWrite",
        "description": "Create or update the task checklist for this session. Use at the start of multi-step tasks: list items with status pending, then set in_progress when working on one and completed when done. Only one task should be in_progress at a time. Replaces the previous list each time. Keeps progress visible and ensures nothing is dropped.",
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
        "description": "Get the current task checklist for this session. Call at the start of work, before planning next steps, or when unsure what remains. Returns the same list maintained by TodoWrite (id, content, status).",
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
        "description": "Store a critical or important fact for the rest of this session so you can reuse it later. Use when you learn: user preferences (e.g. 'preferred_language: TypeScript'), key decisions, project conventions, important errors and how they were fixed, or environment/config facts. Key: short identifier (e.g. 'api_base', 'auth_package'). Value: concise string. Do not store trivial or one-off details. Overwrites existing key. Be contextually aware: write when the information would help you in a future message or session.",
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
        "description": "Retrieve stored facts. Call with no key to get all facts; with key to get one value. Use at the start of a follow-up task, when the user asks what you know, or before acting when stored context (preferences, prior decisions) could change your approach.",
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
        "description": "Semantic codebase search. Returns the most relevant code chunks (functions, classes) for a natural-language query. **START HERE for code discovery** - much more effective than reading full files. Examples: 'where is user authentication validated', 'how are database connections handled', 'error handling patterns', 'main entry points'. Use this for exploration and understanding, then use Read with offset/limit to examine specific returned chunks. Cost-efficient: queries against semantic index instead of loading entire files. Prefer over search() for discovery - use search() only for exact strings/regex.",
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
        "description": "Regex search (ripgrep) across files. Use when you have an exact string or pattern; for exploring or finding by meaning use semantic_retrieve instead. Returns matching lines with paths and line numbers. Use `include` to filter by file type. **Fast and cheap** - use for exact matches, error messages, TODOs. For discovery, start with semantic_retrieve first.",
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
        "description": "Symbol-aware navigation helper. Finds symbol definitions and/or references using language-aware patterns across Python/JS/TS and common typed languages. **Use this before editing ambiguous symbols** with many occurrences. Essential for safe refactoring - shows you all places a symbol is used before making changes.",
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
        "description": "List files and directories at a given path with file sizes. Good first step to understand project structure before diving into specific files.",
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
        "description": "Find files matching a glob pattern recursively. Example: '**/*.py' finds all Python files. Useful for discovering files before reading them.",
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
        "name": "lint_file",
        "description": "Auto-detect the project's linter or type checker and run it on a specific file. Use this after every edit to catch syntax errors, type errors, and style issues before moving on. Returns lint output or confirms no issues found.",
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
        "description": "Fetch content from a URL (HTTP GET). Use when you need to verify information (e.g. confirm API shape, check docs, validate an error or version) or when the user asks for latest info. Returns plain text; large responses are truncated. Do not use for sensitive or authenticated endpoints.",
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
        "description": "Search the web for current information. Use when you need up-to-date docs, error messages, or general lookup. Returns a short list of relevant snippets and links. Prefer WebFetch for a specific known URL.",
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
    "lint_file": lint_file,
    "semantic_retrieve": semantic_retrieve,
    "WebFetch": web_fetch,
    "WebSearch": web_search,
}

TOOLS_REQUIRING_APPROVAL = {"Write", "Edit", "symbol_edit", "Bash"}
SAFE_TOOLS = {"Read", "search", "find_symbol", "list_directory", "Glob", "lint_file", "semantic_retrieve", "WebFetch", "WebSearch"}
SCOUT_TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    t for t in TOOL_DEFINITIONS if t["name"] in SAFE_TOOLS
]

# Claude Agent SDK built-in: AskUserQuestion (we use this name so the model uses the same tool as in the SDK).
# Handled via callback, not execute_tool; we add options and UI.
ASK_USER_QUESTION_DEFINITION: Dict[str, Any] = {
    "type": "custom",
    "name": "AskUserQuestion",
    "description": "Ask the user a clarifying question. Use when the task is ambiguous or when verification fails due to something the user explicitly asked for — do not silently override their request; ask them. Optionally provide an 'options' array of short choices so the user can select or type their answer.",
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question to ask the user (clear and concise)"},
            "context": {"type": "string", "description": "Optional: brief context so the user knows why you're asking"},
            "options": {"type": "array", "items": {"type": "string"}, "description": "Optional: list of choices the user can select (Cursor-style). User can still type a custom answer."},
        },
        "required": ["question"],
    },
}


def execute_tool(name: str, inputs: Dict[str, Any], working_directory: str = ".",
                 backend: Optional[Backend] = None) -> ToolResult:
    """Execute a tool by name with the given inputs."""
    name = TOOL_NAME_NORMALIZE.get(name, name)
    impl = TOOL_IMPLEMENTATIONS.get(name)
    if not impl:
        return ToolResult(success=False, output="", error=f"Unknown tool: {name}")
    try:
        return impl(**inputs, working_directory=working_directory, backend=backend)
    except TypeError as e:
        return ToolResult(success=False, output="", error=f"Invalid arguments for {name}: {e}")
    except Exception as e:
        logger.exception(f"Tool execution error: {name}")
        return ToolResult(success=False, output="", error=f"Tool error: {e}")


def needs_approval(tool_name: str) -> bool:
    """Check if a tool requires user approval."""
    tool_name = TOOL_NAME_NORMALIZE.get(tool_name, tool_name)
    return tool_name in TOOLS_REQUIRING_APPROVAL
