"""Tool execution dispatch and approval logic."""

import logging
from typing import Any, Dict, Optional

from backend import Backend
from tools._common import ToolResult
from tools.schemas import TOOL_NAME_NORMALIZE, TOOL_IMPLEMENTATIONS, TOOLS_REQUIRING_APPROVAL

logger = logging.getLogger(__name__)


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
