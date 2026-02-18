"""Shared types and state for the tools package."""

import contextvars
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


# Context for current session todos (set by agent before tool runs; used by TodoRead when called via execute_tool)
_current_todos_ctx: contextvars.ContextVar[List[Dict[str, Any]]] = contextvars.ContextVar("current_todos", default=[])


def set_current_todos(todos: List[Dict[str, Any]]) -> None:
    """Set the current session todo list for TodoRead when called from execute_tool (e.g. agent context)."""
    _current_todos_ctx.set(list(todos) if todos is not None else [])


@dataclass
class ToolResult:
    """Result from executing a tool"""
    success: bool
    output: str
    error: Optional[str] = None
