"""
Tool definitions and implementations for the coding agent.
Each tool has an Anthropic-compatible schema and an implementation function.
Tools use a Backend abstraction for file/command operations (local or SSH).
"""

from tools._common import ToolResult, set_current_todos, _current_todos_ctx  # noqa: F401
from tools.gitignore import (  # noqa: F401
    _load_gitignore,
    _is_ignored,
    invalidate_gitignore_cache,
    _ALWAYS_SKIP_DIRS,
    _ALWAYS_SKIP_EXTENSIONS,
    _gitignore_cache,
)
from tools.file_ops import (  # noqa: F401
    read_file,
    write_file,
    edit_file,
    symbol_edit,
    invalidate_file_cache,
)
from tools.search_ops import (  # noqa: F401
    search,
    find_symbol,
    list_directory,
    glob_find,
    project_tree,
    lint_file,
)
from tools.external_ops import (  # noqa: F401
    run_command,
    semantic_retrieve,
    web_fetch,
    web_search,
    todo_write,
    todo_read,
)
from tools.schemas import (  # noqa: F401
    TOOL_DEFINITIONS,
    SCOUT_TOOL_DEFINITIONS,
    SAFE_TOOLS,
    TOOLS_REQUIRING_APPROVAL,
    TOOL_NAME_NORMALIZE,
    TOOL_IMPLEMENTATIONS,
    ASK_USER_QUESTION_DEFINITION,
    NATIVE_TEXT_EDITOR,
    NATIVE_BASH,
    NATIVE_EDITOR_NAME,
    NATIVE_BASH_NAME,
    NATIVE_WEB_SEARCH_NAME,
    EDITOR_WRITE_COMMANDS,
)
from tools.dispatch import execute_tool, needs_approval  # noqa: F401
