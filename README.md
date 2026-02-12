# Bedrock Codex

Bedrock Codex is a **Cursor-style coding engine** powered by Amazon Bedrock.

It is not a simple chatbot UI. It is an agentic system that can:
- inspect and edit real project files,
- run commands and stream terminal output live,
- produce plan/build workflows,
- keep/revert code changes with diff review,
- persist and replay sessions,
- work on both local and SSH-backed projects.

## What You Get

- **Agentic IDE workflow**: scout -> plan -> build -> verify
- **Web IDE**: file explorer + Monaco editor + agent panel
- **Plan mode + Build gate**: editable plan steps before execution
- **Keep/Revert gate**: review diffs before finalizing changes
- **Live command streaming**: incremental terminal output with follow/pause
- **Session checkpoints + rewind**: restore risky batches fast
- **Symbol-aware refactors**: `symbol_edit` (AST/tree-sitter first, fallback safe heuristics)
- **Parallel manager-worker assistance**: parallel lane analysis for complex plans
- **Test impact selection**: run likely impacted tests first before full suite
- **Conversation/session persistence**: reconnect and continue where you left off
- **SSH remote support**: open and run projects on remote hosts

## UIs

### Web IDE (recommended)

```bash
python web.py --dir .
```

Open: `http://127.0.0.1:8765`

### Textual TUI (legacy/alternative)

```bash
python main.py -d .
```

## Install

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## AWS / Bedrock Setup

Set credentials in `.env` or your shell environment:

```env
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_SESSION_TOKEN=...   # optional
```

Required IAM permissions:
- `bedrock:InvokeModel`
- `bedrock:InvokeModelWithResponseStream`

## Remote Projects via SSH

Start local server, then connect from the welcome page, or start directly in SSH mode:

```bash
python web.py --ssh user@host --dir /path/on/remote
python web.py --ssh user@host --key ~/.ssh/id_rsa --ssh-port 22 --dir /opt/app
```

All reads/writes/commands run on the selected backend (local or SSH).

## Tooling Surface

| Tool | Purpose |
|---|---|
| `read_file` | Read file content (supports scoped reads) |
| `write_file` | Create/overwrite files |
| `edit_file` | Exact-context string edits |
| `symbol_edit` | Symbol-level refactors via AST/tree-sitter where available |
| `search` | Project-wide regex search |
| `find_symbol` | Symbol definition/reference navigation |
| `list_directory` | Directory listing |
| `glob_find` | Glob-based file discovery |
| `lint_file` | Per-file lint/typecheck detection and execution |
| `run_command` | Shell command execution (with streaming support) |

## Core Workflow

1. Send a coding task.
2. Agent decides whether scout/plan is needed.
3. For complex work, it drafts an actionable plan with verification steps.
4. On build, it executes tools, streams output, and verifies changes.
5. Review diffs and choose Keep/Revert.
6. Resume later from persisted session state.

## Runtime Features You Can Use

- `/checkpoints` -> list available checkpoints
- `/rewind latest` -> restore most recent checkpoint
- `/rewind <checkpoint-id>` -> restore specific checkpoint
- `cancel`/Stop button -> stop active command/task

## Key Config Flags

See `config.py` and `.env` for full list. Important flags include:

```env
# Models / thinking
BEDROCK_MODEL_ID=us.anthropic.claude-opus-4-6-v1
MAX_TOKENS=128000
ENABLE_THINKING=true
THINKING_BUDGET=120000
USE_ADAPTIVE_THINKING=true

# Agent execution
MAX_TOOL_ITERATIONS=200
PLAN_PHASE_ENABLED=true
SCOUT_ENABLED=true
AUTO_APPROVE_COMMANDS=false

# Advanced execution features
LIVE_COMMAND_STREAMING=true
SESSION_CHECKPOINTS_ENABLED=true
PARALLEL_SUBAGENTS_ENABLED=true
PARALLEL_SUBAGENTS_MAX_WORKERS=3
TEST_IMPACT_SELECTION_ENABLED=true
TEST_RUN_FULL_AFTER_IMPACT=true
```

## Architecture

```text
web.py              FastAPI + WebSocket bridge + session/replay orchestration
agent.py            Core coding engine (scout/plan/build, verification, policy, learning)
tools.py            Tool schemas + implementations (including symbol-aware edits)
backend.py          LocalBackend + SSHBackend abstraction
bedrock_service.py  Bedrock streaming client and prompt formatting
sessions.py         Session persistence store
static/             Browser IDE frontend (explorer/editor/chat/tool timeline)
```

## Troubleshooting

### Port already in use (`Errno 48`)

Run on another port:

```bash
python web.py --dir . --port 8766
```

### Bedrock "on-demand throughput isn't supported" for scout model

Use a supported inference profile/model ID in `.env` (especially for `SCOUT_MODEL`), or disable scout:

```env
SCOUT_ENABLED=false
```

### Reconnect and lost UI state

The app replays history and state from sessions. If behavior looks stale, hard refresh once and reconnect; session replay should restore plan/diff state where available.
