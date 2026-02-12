# Bedrock Codex

A coding agent engine powered by Amazon Bedrock. Think Cursor / Claude Code / ChatGPT Codex, but running on your own AWS Bedrock backend.

This is **not a chatbot**. It's an autonomous coding agent that reads your codebase, edits files, runs commands, and reasons through tasks using extended thinking.

## Features

- **Agentic tool-use loop** - Reads, edits, searches, and runs commands autonomously
- **Smart approval** - Auto-approves reads/searches, asks before writes and shell commands
- **Extended thinking** - Claude thinks through complex problems step-by-step
- **Streaming output** - See thinking and actions in real-time
- **Terminal UI** - Rich terminal interface built with Textual
- **Multiple models** - Claude Opus 4.5, Sonnet 4, Claude 3.7, and more

## Tools

The agent has access to 7 core tools:

| Tool | Description | Needs Approval |
|------|-------------|:-:|
| `read_file` | Read file contents with line numbers | No |
| `write_file` | Create or overwrite a file | Yes |
| `edit_file` | Targeted string replacement in a file | Yes |
| `run_command` | Execute shell commands | Yes |
| `search` | Ripgrep-powered regex search | No |
| `list_directory` | List files and directories | No |
| `glob_find` | Find files by glob pattern | No |

## Quick Start

### 1. Install Dependencies

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure AWS Credentials

Edit `.env` with your AWS credentials:

```
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=your-key
AWS_SECRET_ACCESS_KEY=your-secret
AWS_SESSION_TOKEN=your-token  # if using temporary credentials
```

### 3. Run

```bash
python main.py
```

Or point it at a specific project:

```bash
python main.py -d ~/my-project
```

## Usage

Once running, type a task and press Enter:

```
> Fix the bug in main.py where it crashes on empty input
> Add error handling to the API endpoint in routes.py
> Refactor the database module to use async/await
> Find all TODO comments and create a summary
```

### Commands

| Command | Description |
|---------|-------------|
| `/help` | Show available commands |
| `/clear` | Clear the screen |
| `/reset` | Reset conversation history |
| `/cd <path>` | Change working directory |
| `/model` | Show current model info |
| `/tokens` | Show token usage |
| `/quit` | Exit |

### Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `Ctrl+C` | Cancel running task / Quit |
| `Ctrl+L` | Clear screen |
| `Ctrl+R` | Reset conversation |

## Configuration

All settings are in `.env`:

```
# Model
BEDROCK_MODEL_ID=us.anthropic.claude-opus-4-5-20251101-v1:0
MAX_TOKENS=64000

# Extended Thinking
ENABLE_THINKING=true
THINKING_BUDGET=50000

# Throughput
THROUGHPUT_MODE=cross-region

# Agent
MAX_TOOL_ITERATIONS=50
```

## Architecture

```
main.py              Textual TUI - terminal interface
agent.py             Agent engine - agentic tool-use loop
tools.py             Tool definitions and implementations
bedrock_service.py   AWS Bedrock API client (streaming, thinking, tool_use)
config.py            Configuration and model definitions
```

The agent loop:
1. User sends a task
2. Agent calls Bedrock with the task + tool definitions
3. Model responds with thinking + text + tool_use blocks
4. Agent executes tools (with approval for writes/commands)
5. Tool results are fed back to the model
6. Loop continues until the task is complete

## AWS Setup

You need:
1. An AWS account with Bedrock access enabled
2. Model access granted for Claude models in the Bedrock console
3. IAM credentials with `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` permissions
