"""
Tool definitions and implementations for the coding agent.
Each tool has an Anthropic-compatible schema and an implementation function.
Tools use a Backend abstraction for file/command operations (local or SSH).
"""

import os
import json
import logging
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


def read_file(path: str, offset: Optional[int] = None, limit: Optional[int] = None,
              backend: Optional[Backend] = None, working_directory: str = ".") -> ToolResult:
    """Read the contents of a file. Returns line-numbered content."""
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


def run_command(command: str, timeout: int = 30,
                backend: Optional[Backend] = None, working_directory: str = ".") -> ToolResult:
    """Execute a shell command."""
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
# Tool Schemas (Anthropic-compatible)
# ============================================================

TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "name": "read_file",
        "description": "Read the contents of a file with line numbers. ALWAYS read a file before editing it — understand existing code before suggesting modifications. For large files (>500 lines), returns a structural overview (imports, classes, functions) plus head/tail. Use offset and limit to read specific sections of large files. When you need to read multiple files, request them all in a single turn — they run in parallel.",
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
    {
        "name": "write_file",
        "description": "Create a new file or completely overwrite an existing file. Only use for NEW files or when more than 50% of the file changes. For partial modifications, ALWAYS prefer edit_file instead. After writing, use lint_file to verify no syntax errors were introduced.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (relative to working directory)"},
                "content": {"type": "string", "description": "The full content to write to the file"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
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
        "name": "run_command",
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
    {
        "name": "search",
        "description": "Search for a regex pattern across files using ripgrep. Returns matching lines with file paths and line numbers. Escape special regex characters (., *, +, etc.) when searching for literal strings. Use the `include` parameter to filter by file type for faster, more focused results.",
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
    {
        "name": "glob_find",
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
]

TOOL_IMPLEMENTATIONS = {
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "run_command": run_command,
    "search": search,
    "list_directory": list_directory,
    "glob_find": glob_find,
    "lint_file": lint_file,
}

TOOLS_REQUIRING_APPROVAL = {"write_file", "edit_file", "run_command"}
SAFE_TOOLS = {"read_file", "search", "list_directory", "glob_find", "lint_file"}
SCOUT_TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    t for t in TOOL_DEFINITIONS if t["name"] in SAFE_TOOLS
]

# Plan-phase only: ask the user a clarifying question (handled by agent via callback, not execute_tool)
ASK_USER_QUESTION_DEFINITION: Dict[str, Any] = {
    "name": "ask_user_question",
    "description": "Ask the user a clarifying question before finalizing the plan. Use when the task is ambiguous: API version, sync vs async, scope, or design choice. The user's answer will be included in context. Do not over-use; only when the answer would materially change the plan.",
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question to ask the user (clear and concise)"},
            "context": {"type": "string", "description": "Optional: brief context so the user knows why you're asking"},
        },
        "required": ["question"],
    },
}


def execute_tool(name: str, inputs: Dict[str, Any], working_directory: str = ".",
                 backend: Optional[Backend] = None) -> ToolResult:
    """Execute a tool by name with the given inputs."""
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
    return tool_name in TOOLS_REQUIRING_APPROVAL
