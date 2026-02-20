"""Tool schema definitions (Bedrock/Anthropic Messages API) and dispatch maps."""

from typing import Any, Dict, List

from tools.file_ops import read_file, write_file, edit_file, symbol_edit
from tools.search_ops import search, find_symbol, list_directory, glob_find, project_tree, lint_file
from tools.external_ops import run_command, semantic_retrieve, web_fetch, web_search, todo_write, todo_read


# ---------------------------------------------------------------------------
# Anthropic native tools (schema-less — Claude is trained on these)
# ---------------------------------------------------------------------------

NATIVE_TEXT_EDITOR: Dict[str, Any] = {
    "type": "text_editor_20250728",
    "name": "str_replace_based_edit_tool",
}

NATIVE_BASH: Dict[str, Any] = {
    "type": "bash_20250124",
    "name": "bash",
}

# Names used by native tools — needed for dispatch, approval, etc.
NATIVE_EDITOR_NAME = "str_replace_based_edit_tool"
NATIVE_BASH_NAME = "bash"
NATIVE_WEB_SEARCH_NAME = "WebSearch"

# Editor sub-commands that modify files (vs read-only "view")
EDITOR_WRITE_COMMANDS = frozenset({"str_replace", "create", "insert"})

TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    NATIVE_TEXT_EDITOR,
    NATIVE_BASH,
    {
        "type": "custom",
        "name": "WebSearch",
        "description": "Search the web for current information. Returns top results with title, URL, and snippet. Use when you need up-to-date information, documentation, or to verify facts. Requires the duckduckgo-search package.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query string"},
                "max_results": {"type": "integer", "description": "Number of results to return (default: 5, max: 10)"},
            },
            "required": ["query"],
        },
    },
    {
        "type": "custom",
        "name": "symbol_edit",
        "description": "Perform a symbol-aware edit of a function/class/type definition block using AST parsing (Python) or tree-sitter (JS/TS) with regex fallback. Safer than plain str_replace for replacing entire function or class bodies. Use kind=function|class|all to narrow the match. Use occurrence when multiple symbols share the same name (e.g. overloaded methods). Always read the file first to verify the symbol exists.",
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
        "description": "Symbol-aware navigation — finds definitions and references using language-aware patterns (Python, JS/TS, Java, Rust, Go). CRITICAL for safe refactoring: before modifying ANY public function, class, or type, use this to find ALL call sites. Use kind='definition' to find where it's declared, kind='reference' for usages, kind='all' for both. In large codebases, a function may have dozens of callers across many files — modifying it without checking references is the #1 cause of breaking changes. When find_symbol returns >10 reference sites, consider whether your change needs a different strategy (additive change, wrapper, phased migration).",
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
        "description": "Auto-detect and run the project's linter/type-checker on a file. Supports: ruff/flake8/py_compile (Python), tsc (TypeScript), eslint (JavaScript), cargo check (Rust), go vet (Go), ruby -c (Ruby), bash -n (Shell). MANDATORY after EVERY edit — never skip this, even for 'small' changes. Batch lint calls: after editing 3 files, call lint_file on all 3 in one response. If linting reveals errors you introduced, fix them immediately before proceeding. After editing a file that EXPORTS interfaces (functions, classes, types used by other files), also lint the direct consumers to catch type errors or import breakage that only manifest in the importing file.",
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
]


# Map legacy/API names to canonical tool names (for backward compat)
TOOL_NAME_NORMALIZE = {
    "read_file": NATIVE_EDITOR_NAME,
    "write_file": NATIVE_EDITOR_NAME,
    "edit_file": NATIVE_EDITOR_NAME,
    "glob_find": "Glob",
    "run_command": NATIVE_BASH_NAME,
    # Legacy names from old tool definitions / saved sessions
    "Read": NATIVE_EDITOR_NAME,
    "Write": NATIVE_EDITOR_NAME,
    "Edit": NATIVE_EDITOR_NAME,
    "Bash": NATIVE_BASH_NAME,
    "WebSearch": NATIVE_WEB_SEARCH_NAME,
    "web_search": NATIVE_WEB_SEARCH_NAME,
}


TOOL_IMPLEMENTATIONS = {
    # Native tools are dispatched through execute_tool with special routing
    NATIVE_EDITOR_NAME: None,   # Handled by dispatch.py routing logic
    NATIVE_BASH_NAME: None,     # Handled by dispatch.py routing logic
    # Custom tools
    "WebSearch": web_search,
    "Glob": glob_find,
    "symbol_edit": symbol_edit,
    "search": search,
    "find_symbol": find_symbol,
    "list_directory": list_directory,
    "project_tree": project_tree,
    "lint_file": lint_file,
    "semantic_retrieve": semantic_retrieve,
    "WebFetch": web_fetch,
    "TodoWrite": todo_write,
    "TodoRead": todo_read,
}

TOOLS_REQUIRING_APPROVAL = {NATIVE_EDITOR_NAME, NATIVE_BASH_NAME, "symbol_edit"}
SAFE_TOOLS = {
    NATIVE_WEB_SEARCH_NAME,
    "search", "find_symbol", "list_directory", "project_tree", "Glob",
    "lint_file", "semantic_retrieve", "WebFetch", "TodoWrite", "TodoRead",
}
# Note: str_replace_based_edit_tool with command="view" is safe (read-only),
# but the tool as a whole can also write, so it stays in TOOLS_REQUIRING_APPROVAL.
# The dispatch/execution layer handles the view-is-safe exception.

SCOUT_TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    t for t in TOOL_DEFINITIONS
    if t.get("name") in SAFE_TOOLS or t.get("name") == NATIVE_EDITOR_NAME
]

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
