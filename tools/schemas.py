"""Tool schema definitions (Bedrock/Anthropic Messages API) and dispatch maps."""

from typing import Any, Dict, List

from tools.file_ops import read_file, write_file, edit_file, symbol_edit
from tools.search_ops import search, find_symbol, list_directory, glob_find, project_tree, lint_file
from tools.external_ops import run_command, semantic_retrieve, web_fetch, web_search, todo_write, todo_read


TOOL_DEFINITIONS: List[Dict[str, Any]] = [
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


# Map API/implementation names to canonical tool names
TOOL_NAME_NORMALIZE = {
    "read_file": "Read",
    "write_file": "Write",
    "edit_file": "Edit",
    "glob_find": "Glob",
    "run_command": "Bash",
}


TOOL_IMPLEMENTATIONS = {
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
