"""Tool execution dispatch and approval logic."""

import logging
from typing import Any, Dict, Optional

from backend import Backend, LocalBackend
from tools._common import ToolResult
from tools.schemas import (
    TOOL_NAME_NORMALIZE, TOOL_IMPLEMENTATIONS, TOOLS_REQUIRING_APPROVAL,
    NATIVE_EDITOR_NAME, NATIVE_BASH_NAME,
    EDITOR_WRITE_COMMANDS,
)
from tools.file_ops import read_file, write_file, edit_file
from tools.external_ops import run_command

logger = logging.getLogger(__name__)


def _dispatch_text_editor(
    inputs: Dict[str, Any],
    working_directory: str,
    backend: Optional[Backend],
) -> ToolResult:
    """Route str_replace_based_edit_tool commands to our backend implementations."""
    command = inputs.get("command", "")
    path = inputs.get("path", "")

    if command == "view":
        view_range = inputs.get("view_range")
        kwargs: Dict[str, Any] = {"path": path, "working_directory": working_directory, "backend": backend}
        if view_range and isinstance(view_range, list) and len(view_range) == 2:
            start, end = view_range
            kwargs["offset"] = start
            if end != -1:
                kwargs["limit"] = end - start + 1
        return read_file(**kwargs)

    elif command == "str_replace":
        return edit_file(
            path=path,
            old_string=inputs.get("old_str", ""),
            new_string=inputs.get("new_str", ""),
            working_directory=working_directory,
            backend=backend,
        )

    elif command == "create":
        return write_file(
            path=path,
            content=inputs.get("file_text", ""),
            working_directory=working_directory,
            backend=backend,
        )

    elif command == "insert":
        insert_line = inputs.get("insert_line", 0)
        insert_text = inputs.get("insert_text", "")
        b = backend or LocalBackend(working_directory)
        try:
            content = b.read_file(path)
            lines = content.split("\n")
            new_lines = insert_text.split("\n")
            lines[insert_line:insert_line] = new_lines
            new_content = "\n".join(lines)
            b.write_file(path, new_content)
            return ToolResult(success=True, output=f"Inserted {len(new_lines)} lines after line {insert_line}.")
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

    else:
        return ToolResult(success=False, output="", error=f"Unknown text_editor command: {command}")


def _dispatch_bash(
    inputs: Dict[str, Any],
    working_directory: str,
    backend: Optional[Backend],
) -> ToolResult:
    """Route bash tool to our run_command implementation."""
    if inputs.get("restart"):
        return ToolResult(success=True, output="Shell session restarted.")
    command = inputs.get("command", "")
    timeout = inputs.get("timeout", 30)
    return run_command(command=command, timeout=timeout, working_directory=working_directory, backend=backend)


def _normalize_legacy_inputs(original_name: str, inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Convert legacy tool inputs to native tool format when called via old names."""
    if inputs.get("command"):
        return inputs  # Already has native command field
    if original_name == "Read" or original_name == "read_file":
        normalized = {"command": "view", "path": inputs.get("path", "")}
        if inputs.get("offset") or inputs.get("limit"):
            start = inputs.get("offset", 1)
            limit = inputs.get("limit")
            if limit:
                normalized["view_range"] = [start, start + limit - 1]
            else:
                normalized["view_range"] = [start, -1]
        return normalized
    if original_name == "Write" or original_name == "write_file":
        return {"command": "create", "path": inputs.get("path", ""), "file_text": inputs.get("content", "")}
    if original_name == "Edit" or original_name == "edit_file":
        return {
            "command": "str_replace",
            "path": inputs.get("path", ""),
            "old_str": inputs.get("old_string", ""),
            "new_str": inputs.get("new_string", ""),
        }
    if original_name in ("Bash", "run_command"):
        return inputs  # Bash inputs are already compatible
    return inputs


def execute_tool(
    name: str,
    inputs: Dict[str, Any],
    working_directory: str = ".",
    backend: Optional[Backend] = None,
    *,
    extra_context: Optional[Dict[str, Any]] = None,
) -> ToolResult:
    """Execute a tool by name with the given inputs. extra_context can provide e.g. todos for TodoRead."""
    original_name = name
    name = TOOL_NAME_NORMALIZE.get(name, name)

    # Native tool routing (with legacy input normalization)
    if name == NATIVE_EDITOR_NAME:
        normalized_inputs = _normalize_legacy_inputs(original_name, inputs)
        return _dispatch_text_editor(normalized_inputs, working_directory, backend)

    if name == NATIVE_BASH_NAME:
        return _dispatch_bash(inputs, working_directory, backend)

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


def needs_approval(tool_name: str, tool_input: Optional[Dict[str, Any]] = None) -> bool:
    """Check if a tool requires user approval.
    
    str_replace_based_edit_tool with command="view" is read-only and safe.
    """
    tool_name = TOOL_NAME_NORMALIZE.get(tool_name, tool_name)
    if tool_name == NATIVE_EDITOR_NAME:
        cmd = (tool_input or {}).get("command", "")
        return cmd in EDITOR_WRITE_COMMANDS
    return tool_name in TOOLS_REQUIRING_APPROVAL
