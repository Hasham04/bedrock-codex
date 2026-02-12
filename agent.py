"""
Core coding agent engine.
Implements the agentic loop: prompt -> think -> use tools -> respond.
"""

import asyncio
import json
import logging
import os
import queue
import re
import threading
import time
from collections import defaultdict
from typing import List, Dict, Any, Optional, Callable, Awaitable
from dataclasses import dataclass, field

from bedrock_service import BedrockService, GenerationConfig, BedrockError
from tools import TOOL_DEFINITIONS, SCOUT_TOOL_DEFINITIONS, SAFE_TOOLS, execute_tool, needs_approval, ToolResult, ASK_USER_QUESTION_DEFINITION
from backend import Backend, LocalBackend
from config import model_config, supports_thinking, app_config, get_context_window

logger = logging.getLogger(__name__)


# ============================================================
# System Prompts
# ============================================================

SCOUT_SYSTEM_PROMPT = """You are a senior engineer doing a deep code review before implementation begins. Your reconnaissance directly determines whether the implementation succeeds or fails.

You are not skimming. You are building a mental model of this codebase — how it thinks, how it's structured, what patterns it follows, where the bodies are buried.

<constraints>
- READ-ONLY tools. You cannot modify anything.
- Batch reads: request multiple files in a single turn — they run in parallel.
- Do NOT stop after listing directories. Read the actual source files. Understand the implementation, not just the structure.
</constraints>

<strategy>
1. **Start smart**: List root. Read manifest files (package.json, pyproject.toml, Cargo.toml, go.mod, requirements.txt, Makefile). Know the stack.
2. **Read with purpose**: Every batch of file reads should answer a specific question — "how does auth work?", "what does this handler call?", "what patterns do the tests follow?" Don't read files just because they exist.
3. **Batch aggressively**: Read 3-8 files per turn. They execute in parallel. Don't read files one by one.
4. **Follow the relevant path**: Read the files that will be touched, their imports, and related tests. Understand the data flow end-to-end for THIS task. Don't map the entire codebase.
5. **Know when to stop**: You have enough context when you can answer: What's the stack? What files need changing? What patterns must I follow? What could go wrong? Once you can answer all four, produce your summary and stop.
6. Skip vendored code, lock files, build artifacts, node_modules, __pycache__, generated files.
</strategy>

<output_format>
### Stack
Language, framework, key dependencies. Versions where they matter.

### Architecture
How the application is organized. How modules communicate. Where state lives. The mental model someone needs to work here.

### Files Relevant to This Task
For each file the coding agent should read or modify:
- **path/to/file.py** — What it does, why it matters for this task, key functions/classes.

### Conventions & Patterns
- Naming, formatting, error handling, logging patterns
- Architectural patterns (DI, middleware, event systems, state management)
- Existing utilities/helpers that MUST be reused (don't reinvent)

### Build / Test / Lint
- Exact commands to lint, test, build, type-check

### Risks & Concerns
- Edge cases, gotchas, tight coupling, tech debt, breaking change potential
</output_format>

<how_you_think>
After every batch of reads, pause and reflect:
1. **What did I just learn?** — Any surprises? Unexpected dependencies? Unusual patterns?
2. **What's my mental model now?** — How do I understand this codebase so far? What are the key moving parts?
3. **What gaps remain?** — What do I still not understand that would matter for this task? What should I read next?
4. **Am I deep enough?** — Have I just seen the surface, or do I understand the actual implementation? Can I predict how a change in file A would affect file B?
</how_you_think>

<working_directory>{working_directory}</working_directory>

Use relative paths. Be thorough — the implementation depends entirely on your understanding."""

PLAN_SYSTEM_PROMPT = """You are a principal engineer designing an implementation plan. You think like someone who has shipped production software for 20 years and has the scars to prove it.

Your plan will be handed to an implementation agent. If your plan is vague, the implementation will be vague. If your plan is precise, the implementation will be precise. Every ambiguity you leave will become a bug.

You have READ-ONLY tools. Use them to understand the codebase before writing the plan.

<planning_process>
Phase 1 — UNDERSTAND: Read the source files relevant to this task. Be smart about what you read — batch multiple reads per turn, follow imports, read related tests. Read as many files as you need to fully understand the problem. But read with purpose — each batch of reads should answer a specific question, not just "let me see what's in this directory."
Phase 2 — THINK: What are the constraints? What patterns does this codebase use? What existing code can be reused? What could go wrong? What's the simplest approach that fully solves the problem?
Phase 3 — WRITE: Once you have enough understanding, produce the complete plan document. Every step must be specific enough that an implementer can execute it without asking a single clarifying question.

IMPORTANT: When you feel you've gathered sufficient context, STOP calling tools and write the plan. Don't read "one more file just in case." The key signal is: could you write a precise, actionable plan right now? If yes, stop reading and write it.

If the task is ambiguous (e.g. which API version, sync vs async, scope of the feature), use the ask_user_question tool to ask the user before finalizing the plan. Incorporate their answer into your plan. Ask only when the answer would materially change the plan.
</planning_process>

<plan_document_format>
# Plan: {{concise title}}

## Why
What problem does this solve? What is the current state vs desired state? Someone reading this should understand the motivation without seeing the original request.

## Approach
High-level design. How will the pieces fit together?
- What architectural pattern applies
- What existing code/utilities/patterns to reuse (cite specific file paths and function names)
- What alternatives were considered and why this approach wins
- Diagram if the change involves data flow or multi-component interaction:
```
ComponentA → ComponentB → ComponentC
```

## Affected Files

| File | Action | What Changes |
|------|--------|--------------|
| path/to/file.py | EDIT | Specific description |
| path/to/new.py | CREATE | What it contains and why |

## Checklist
Short execution checklist for the implementer. One line per distinct user-requested item.
Use strict numbered format:
1. <distinct item #1>
2. <distinct item #2>
3. <distinct item #3>

## Steps
Each step must name the exact file, exact function/class, and describe the specific change — not "update the handler" but "in `handle_request()`, add a check for `if user.is_admin` before the existing authorization block, returning 403 if false."

1. **[EDIT]** `path/file.py` → `function_name()` — Specific change description
2. **[CREATE]** `path/new.py` — What it contains, key classes/functions, why it exists
3. **[RUN]** `exact command` — What it verifies

## Edge Cases & Risks
For each risk: the scenario, the consequence if unhandled, and the mitigation.

- **Risk**: Description → **Impact**: What breaks → **Fix**: How to handle it

## Verification
Exact commands and checks. Not "run the tests" but "run `pytest tests/test_auth.py -v` and expect the new `test_admin_access` to pass alongside existing tests."

<critical_requirements>
- Do not output a single vague todo.
- If the request includes multiple actions/locations/files, produce multiple checklist items and multiple numbered steps.
- Every numbered step must be actionable and specific (file + function/class + exact change).
- Never use meta placeholders like "let me check X" as a plan step.
</critical_requirements>
</plan_document_format>

<working_directory>{working_directory}</working_directory>

A great plan is one where the implementer never has to stop and think "what did they mean by this?" Be that precise."""

BUILD_SYSTEM_PROMPT = """You are a senior engineer implementing an approved plan. You write code the way a craftsman builds furniture — every joint matters, nothing is left rough, and the result should feel like it was always part of the original.

Your code will be read by other engineers. It will run in production. Write it accordingly.

<checklist_workflow>
When the user or plan gives multiple distinct tasks (e.g. "do X. Also do Y. Then check Z" or bullet points or "in A inject B; now check C at line N"), treat it as a checklist:
1. **List the todos first**: At the start, output a short numbered checklist of exactly what you will do (e.g. "1. In ExpireInodeBase inject actions for getExtraInodeAttributes and hub.write (VFSException). 2. Check CentaurTestBase line 393 (InterruptedException catch)."). This makes progress visible and ensures nothing is dropped.
2. **Work through each in order**: Before tackling each item, state which todo you're on (e.g. "Todo 1: ..."). Do the work (read, edit, verify). Then briefly confirm (e.g. "Todo 1 done.") before starting the next. Do not skip or merge items unless the user explicitly said to.
</checklist_workflow>

<execution_principles>
1. **Follow the plan, think for yourself**: Execute steps in order, but you're not a robot. If you see something the plan missed — a better approach, a missed edge case, a simpler solution — adapt. State what you changed and why.
2. **Read before write. Always.**: Never modify a file you haven't read in this session. Understand the surrounding code, the imports, the patterns. Your change must fit seamlessly into the existing codebase — same style, same patterns, same level of quality.
3. **Surgical precision**: Use edit_file with enough context (3-5 surrounding lines) to match exactly one location. Use write_file only for new files or complete rewrites. Every edit should be the minimum change that fully solves the problem.
4. **Verify everything**: After every file modification:
   (a) Re-read the changed section. Confirm the edit is correct.
   (b) Run the linter or type checker on the file. Fix any errors before moving on.
   (c) If a test exists for this code, run it.
   You are not done with a step until verification passes.
5. **When things go wrong, diagnose**: If a tool call fails or a test breaks, stop. Read the error. Think about the root cause. Don't retry blindly — understand what went wrong, fix the cause, then proceed.
6. **Write code that belongs**: Match the existing codebase's conventions exactly — naming, formatting, error handling, abstraction level. Your code should look like the same person wrote it. Do not introduce new patterns, new abstractions, or new dependencies unless the plan explicitly calls for it.
7. **Think about the edges**: Empty inputs. None values. Concurrent access. Error paths. Large inputs. Off-by-one errors. Missing permissions. These are where bugs live. Handle them or explicitly note why they don't apply.
8. **Security is non-negotiable**: No command injection. No XSS. No SQL injection. No path traversal. If user-controlled input touches a dangerous operation, sanitize it.
9. **Final pass**: After all steps are complete, re-read every modified file. Run the full linter/test suite. Fix anything broken. The bar is: someone reviewing this PR would approve it without comments.
</execution_principles>

<how_you_think>
After every action — every file read, every edit, every command — pause and reflect before your next move:

1. **What did I just learn?** — Did the file look like I expected? Did the command succeed? Did the edit apply cleanly? Were there surprises — unexpected imports, different data structures, failing tests?
2. **Does this change my approach?** — Based on what I now know, is the plan still correct? Should I adjust a later step? Did I discover a dependency or constraint the plan missed?
3. **What are my next 2-3 moves?** — Think ahead. Don't just react to the last result. What files do I need to touch next? What might go wrong? What should I verify?
4. **Am I done with this step?** — Did I verify the change? Did the linter pass? Would a reviewer approve this?

This deliberate reflection between actions is what separates careful engineering from blind tool-calling. Use your thinking time generously — it's the most valuable thing you can do.
</how_you_think>

<plan_next_move>
Before every batch of tool calls, output 1-2 sentences stating what you will do next and why (e.g. "Next I will read src/auth.py and the test file to confirm the current logic, then edit the validation block."). Then call the tools. This keeps your reasoning explicit and makes the next step obvious.
</plan_next_move>

<tool_strategy>
**Search before you read.** Don't read entire large files to find the section you need. Use `search` to locate the exact function, class, or pattern, then `read_file` with `offset` and `limit` to read just that section.

- To find where to make a change: `search` for the function/class name, then targeted `read_file`
- To check all usages of something you changed: `search` with `include` filter
- To verify an edit: `read_file` with offset/limit on just the changed section
- **Batch tool calls**: request multiple reads/searches in one turn — they run in parallel
</tool_strategy>

<tool_usage>
- `read_file`: Reads with line numbers. Use `offset` + `limit` to read specific sections of large files instead of the whole file.
- `edit_file`: old_string must match EXACTLY one location, including all whitespace. Include surrounding lines for uniqueness. If it fails, re-read the file — the content may have changed.
- `write_file`: Overwrites entirely. Only for new files or when >50% changes. Prefer edit_file.
- `run_command`: Runs in working directory. Check stdout AND stderr. Non-zero exit = failure.
- `search`: Regex search across files with ripgrep. Returns matching lines with paths and line numbers. Use `include` to filter by file type.
- `glob_find`: Find files matching a pattern. Use to discover files before reading.
- `lint_file`: Auto-detects the project linter. Use after every edit.
</tool_usage>

<examples>
**edit_file — Do:** Include 3-5 lines of surrounding context so old_string matches exactly one place.
  Do: old_string = "    def handle(self):\\n        return None" with exact indentation and newline.
**edit_file — Don't:** Don't use a single line or a generic pattern that could match multiple spots.
  Don't: old_string = "return None" (multiple occurrences).
**Code style — Do:** Match the file's existing style: same quotes, same naming, same error-handling pattern.
**Code style — Don't:** Don't introduce a new pattern (e.g. f-strings) if the file uses .format(); don't add a new dependency the project doesn't use.
</examples>

<working_directory>{working_directory}</working_directory>

No preambles. No filler. Implement with precision and care."""

# System prompt used in direct mode (no plan phase)
AGENT_SYSTEM_PROMPT = """You are an expert software engineer and a thoughtful problem solver. You combine deep technical skill with good judgment about what to build and how to build it.

You don't just complete tasks — you understand them. When someone asks you to do something, you think about what they actually need, not just what they literally said. You consider the context, the codebase conventions, the edge cases, and the downstream effects.

You care about quality. Not gold-plating — but genuine quality. Code that works, reads clearly, handles errors gracefully, and fits naturally into the existing codebase. Code that another engineer would look at and think "this is clean."

<how_you_work>
1. **Understand first**: Before writing any code, make sure you understand the request, the existing codebase, and the constraints. Read the relevant files. Follow the imports. Understand the data flow. If something is unclear, look at the code — the answer is usually there.
2. **Think, then act**: Don't rush to the first solution. Consider the approach. Is there existing code that already does something similar? What's the simplest way to solve this that's also correct? Are there edge cases? Then execute with confidence.
3. **Write code that belongs**: Your changes should be indistinguishable from the best code already in the project. Same naming conventions. Same error handling patterns. Same level of abstraction. If the codebase uses snake_case, you use snake_case. If it handles errors with Result types, you do too. You're joining an existing team, not starting a new project.
4. **Verify your work**: After every modification:
   - Re-read the changed code. Does it look right?
   - Run the linter if one exists. Fix any issues.
   - Think: "If I were reviewing this PR, would I approve it?"
5. **Be honest about uncertainty**: If you're not sure about something, say so. If you're making an assumption, state it. If there are multiple valid approaches, explain the tradeoffs. Don't fake confidence.
</how_you_work>

<principles>
- **Read before write**: Never modify a file you haven't read in this session.
- **Minimal, complete changes**: Do exactly what's needed — no more, no less. Don't refactor unrelated code. Don't add unnecessary abstractions. But DO handle the edge cases that matter.
- **Security matters**: No injection vulnerabilities. Sanitize user input at boundaries. Don't log secrets.
- **When things break, diagnose**: If a tool call fails, understand why. Don't retry blindly. Read the error, think about the cause, fix the root problem.
- **Batch when possible**: When you need multiple files, read them all in one turn.
</principles>

<how_you_think>
After every action (reading a file, running a command, making an edit), pause and reflect before your next move:

1. **What did I just learn?** — Did the file contain what I expected? Did the command succeed? Did the edit apply correctly? Any surprises?
2. **Does this change my approach?** — Based on what I now know, is my original plan still the best path? Do I need to adjust?
3. **What are my next 2-3 moves?** — Don't just react to the last result. Think ahead. What do I need to do next, and what might I need after that?
4. **Am I done?** — Have I fully solved the problem? Are there loose ends? Would I be confident shipping this?

This deliberate reflection between actions is what separates careful engineering from blind tool-calling. Use your thinking time for this — it's the most valuable thing you can do.
</how_you_think>

<plan_next_move>
Before every batch of tool calls, output 1-2 sentences stating your next move(s) and why (e.g. "Next I will read the handler and its tests, then add the null check."). Then call the tools.
</plan_next_move>

<tool_strategy>
**Search before you read.** Don't read a 1000-line file to find one function. Use `search` to locate what you need, then `read_file` with `offset` and `limit` to read just that section. This is faster and keeps your context clean.

- To find where something is defined: `search` for the class/function name
- To understand a specific function: `search` to find it, then `read_file` with offset/limit for ~50 lines around it
- To read a whole small file (<200 lines): just `read_file` with no offset
- To understand a large file: `read_file` with no offset (gets structural overview), then targeted reads of sections you care about
- To find all usages of something: `search` with `include` to filter by file type
- **Batch reads**: when you need multiple files or sections, request them all in one turn — they run in parallel
</tool_strategy>

<tool_usage>
- `read_file`: Reads with line numbers. For large files (>500 lines), returns structural overview. Use `offset` + `limit` to read specific sections.
- `edit_file`: old_string must match exactly one location, including whitespace. Include 3-5 surrounding lines. If "not found", re-read the file.
- `write_file`: Overwrites entirely. Use only for new files or major rewrites. Prefer edit_file.
- `run_command`: Runs in working directory. Check stdout and stderr. Non-zero exit = failure.
- `search`: Regex search across files using ripgrep. Returns matching lines with file paths and line numbers. Use `include` to filter by file type (e.g. `*.py`).
- `glob_find`: Find files matching a pattern (e.g. `**/*.test.ts`). Use to discover files before reading.
- `lint_file`: Auto-detects project linter. Use after every edit.
</tool_usage>

<examples>
**edit_file — Do:** Include 3-5 lines of context so old_string matches exactly one location. Copy indentation and newlines from the file.
**edit_file — Don't:** Don't use a single line that could match multiple places; don't guess whitespace — re-read the file first.
**Code — Do:** Match existing style (naming, error handling, imports). Your change should look like the same author wrote it.
**Code — Don't:** Don't refactor unrelated code; don't add new dependencies or patterns the codebase doesn't use.
</examples>

<working_directory>{working_directory}</working_directory>

When the task is creative (writing, design, brainstorming), bring genuine creativity and depth — don't produce generic filler.
When the task is technical, be precise and thorough.
When the task is simple, don't overcomplicate it.

Match the energy of the request. A quick question deserves a quick answer. A complex implementation deserves careful thought. Always bring your best work."""


# ============================================================
# Agent Event Types
# ============================================================

@dataclass
class AgentEvent:
    """Event emitted by the agent during execution"""
    type: str
    # Types: phase_start, phase_plan, phase_end,
    #        thinking_start, thinking, thinking_end,
    #        text_start, text, text_end,
    #        tool_call, tool_result, tool_rejected, auto_approved,
    #        scout_start, scout_progress, scout_end,
    #        plan_step_progress,
    #        stream_retry, stream_recovering, stream_failed,
    #        error, done, cancelled
    content: str = ""
    data: Optional[Dict[str, Any]] = None


# ============================================================
# Helpers
# ============================================================

_PLAN_RE = re.compile(r"<plan>\s*(.*?)\s*</plan>", re.DOTALL)


def _extract_plan(text: str) -> Optional[str]:
    """Extract the content between <plan>...</plan> tags, if present."""
    m = _PLAN_RE.search(text)
    return m.group(1).strip() if m else None


# ============================================================
# Intelligent intent classification — uses a fast LLM call
# instead of brittle regex heuristics
# ============================================================

_CLASSIFY_SYSTEM = """You are a task classifier for a coding agent. Given a user message, decide:
1. Does the agent need to SCOUT the codebase first? (read files, understand structure)
2. Does the agent need to PLAN before executing? (multi-step, multi-file, or architectural work)
3. How complex is this task? (trivial / simple / complex)

Return ONLY a JSON object — no markdown, no explanation:
{"scout": true/false, "plan": true/false, "complexity": "trivial"|"simple"|"complex"}

Guidelines:
- Greetings, thanks, yes/no, small talk → {"scout": false, "plan": false, "complexity": "trivial"}
- Follow-up to previous work ("now do X", "also fix Y", "looks good but change Z") → {"scout": false, "plan": false, "complexity": "simple"}
- Simple questions about the codebase → {"scout": true, "plan": false, "complexity": "simple"}
- Simple single-file edits, quick fixes → {"scout": true, "plan": false, "complexity": "simple"}
- Reading/finding/searching files → {"scout": true, "plan": false, "complexity": "simple"}
- Running a command → {"scout": false, "plan": false, "complexity": "trivial"}
- Creative tasks (write a paper, generate content) → {"scout": false, "plan": false, "complexity": "simple"}
- Multi-file changes, refactoring, new features → {"scout": true, "plan": true, "complexity": "complex"}
- Architecture changes, migrations, large rewrites → {"scout": true, "plan": true, "complexity": "complex"}
- If you're unsure, lean toward scout=true (cheap) but plan=false (only plan when clearly needed)
- Plan should only be true for tasks that genuinely need multi-step coordination across multiple files

Complexity guide:
- "trivial": greetings, yes/no, simple questions, running a single command
- "simple": single-file reads, explanations, small edits, creative writing
- "complex": multi-file changes, refactoring, new features, architecture work"""

# Cache for the classifier — avoids re-calling for the same message
_classify_cache: Dict[str, Dict[str, bool]] = {}


def classify_intent(task: str, service=None) -> Dict[str, Any]:
    """Use a fast LLM call to classify whether a task needs scouting and/or planning,
    and determine task complexity for smart model routing.

    Returns {"scout": bool, "plan": bool, "complexity": "trivial"|"simple"|"complex"}.
    Falls back to conservative defaults if the LLM call fails.
    """
    stripped = task.strip()
    if not stripped:
        return {"scout": False, "plan": False, "complexity": "trivial"}

    # Check cache
    cache_key = stripped[:200].lower()
    if cache_key in _classify_cache:
        return _classify_cache[cache_key]

    # If no service available, fall back to simple heuristic
    if service is None:
        result = _classify_fallback(stripped)
        _classify_cache[cache_key] = result
        return result

    try:
        from bedrock_service import GenerationConfig
        config = GenerationConfig(
            max_tokens=80,
            enable_thinking=False,
            throughput_mode="cross-region",
        )
        resp = service.generate_response(
            messages=[{"role": "user", "content": stripped}],
            system_prompt=_CLASSIFY_SYSTEM,
            model_id=app_config.scout_model,
            config=config,
        )
        # Parse the JSON from the response
        text = resp.content.strip()
        # Handle possible markdown wrapping
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        import json as _json
        result = _json.loads(text)
        complexity = result.get("complexity", "simple")
        if complexity not in ("trivial", "simple", "complex"):
            complexity = "simple"
        result = {
            "scout": bool(result.get("scout", True)),
            "plan": bool(result.get("plan", False)),
            "complexity": complexity,
        }
        logger.info(f"Intent classification: {result} for: {stripped[:80]}...")
    except Exception as e:
        logger.warning(f"Intent classification failed ({e}), using fallback")
        result = _classify_fallback(stripped)

    _classify_cache[cache_key] = result
    return result


def _classify_fallback(task: str) -> Dict[str, Any]:
    """Simple fallback when LLM classification is unavailable."""
    stripped = task.strip().rstrip("!?.").lower()
    words = stripped.split()
    # Very short or trivial → no scout, no plan
    if len(words) <= 2:
        return {"scout": False, "plan": False, "complexity": "trivial"}
    # Default: scout yes (cheap and helpful), plan no
    return {"scout": True, "plan": False, "complexity": "simple"}


def needs_planning(task: str, service=None) -> bool:
    """Use LLM-based intent classification to decide if planning is needed."""
    return classify_intent(task, service).get("plan", False)


# ============================================================
# Coding Agent
# ============================================================

class CodingAgent:
    """
    Core coding agent that orchestrates the tool-use loop with Bedrock.
    
    Flow:
    1. User sends a task
    2. Agent calls Bedrock with messages + tool definitions
    3. Bedrock responds with text and/or tool_use blocks
    4. If tool_use: execute tools (with approval for writes), send results back, loop
    5. If end_turn: return the final text response
    """

    def __init__(
        self,
        bedrock_service: BedrockService,
        working_directory: str = ".",
        max_iterations: int = 50,
        backend: Optional["Backend"] = None,
    ):
        self.service = bedrock_service
        self.working_directory = os.path.abspath(working_directory)
        self.backend: Backend = backend or LocalBackend(self.working_directory)
        self.max_iterations = max_iterations
        self.history: List[Dict[str, Any]] = []
        self.system_prompt = AGENT_SYSTEM_PROMPT.format(working_directory=self.working_directory)
        self._cancelled = False
        self._current_plan: Optional[List[str]] = None  # plan steps from last plan phase
        self._scout_context: Optional[str] = None  # cached scout context for reuse across phases
        self._plan_step_index: int = 0  # current plan step being executed
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._cache_read_tokens = 0
        self._cache_write_tokens = 0
        # Track approved operations so we don't re-prompt for the same thing
        self._approved_commands: set = set()
        # File snapshots: {abs_path: original_content_or_None_if_new_file}
        self._file_snapshots: Dict[str, Optional[str]] = {}
        # Track history length at last API call for accurate token counting
        self._history_len_at_last_call = 0
        # Running conversation summary — persists across trims
        self._running_summary: str = ""
        # File content cache: avoids re-reading unchanged files
        # {abs_path: (content_str, read_time)}
        self._file_cache: Dict[str, tuple] = {}
        # Per-step checkpoints: {step_num: {abs_path: content_or_None}}
        self._step_checkpoints: Dict[int, Dict[str, Optional[str]]] = {}

    @property
    def total_tokens(self) -> int:
        return self._total_input_tokens + self._total_output_tokens

    @property
    def modified_files(self) -> Dict[str, Optional[str]]:
        """Return a copy of the file snapshots (abs_path -> original content or None for new files)."""
        return dict(self._file_snapshots)

    def cancel(self):
        """Cancel the current agent run and kill any running command."""
        self._cancelled = True
        # Kill any currently running subprocess / SSH command
        if self.backend:
            try:
                self.backend.cancel_running_command()
            except Exception:
                pass

    def reset(self):
        """Reset conversation history"""
        self.history = []
        self._cancelled = False
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._cache_read_tokens = 0
        self._cache_write_tokens = 0
        self._approved_commands = set()
        self._history_len_at_last_call = 0
        self._running_summary = ""
        self._current_plan = None
        self._scout_context = None
        self._file_snapshots = {}
        self._plan_step_index = 0
        self._file_cache = {}
        self._step_checkpoints = {}

    # ------------------------------------------------------------------
    # Plan step progress tracking
    # ------------------------------------------------------------------

    # Regex to detect "step N", "Step N:", "working on step N", etc.
    _STEP_RE = re.compile(
        r"(?:step|working on step|executing step|starting step)\s+(\d+)",
        re.IGNORECASE,
    )

    def _detect_plan_step(self, text: str) -> Optional[int]:
        """Parse assistant text for references to plan step numbers
        and update _plan_step_index to track progress.
        Returns the new step number if it changed, else None."""
        if not self._current_plan:
            return None
        matches = self._STEP_RE.findall(text[:500])  # only check first 500 chars
        if matches:
            try:
                step_num = int(matches[-1])  # use the last match
                if 1 <= step_num <= len(self._current_plan):
                    old = self._plan_step_index
                    self._plan_step_index = step_num
                    if step_num != old:
                        # Capture checkpoint: snapshot all currently-modified files
                        self._capture_step_checkpoint(old)
                        return step_num
            except (ValueError, IndexError):
                pass
        return None

    def _capture_step_checkpoint(self, step_num: int) -> None:
        """Snapshot the current state of all modified files at a given plan step."""
        if step_num <= 0:
            return
        checkpoint: Dict[str, Optional[str]] = {}
        for abs_path in self._file_snapshots:
            try:
                content = self.backend.read_file(abs_path)
                checkpoint[abs_path] = content
            except Exception:
                checkpoint[abs_path] = None
        self._step_checkpoints[step_num] = checkpoint
        logger.debug(f"Captured checkpoint for step {step_num}: {len(checkpoint)} files")

    def revert_to_step(self, step_num: int) -> List[str]:
        """Revert all files to the state captured at a given plan step checkpoint.
        Returns list of reverted file paths."""
        if step_num not in self._step_checkpoints:
            return []
        checkpoint = self._step_checkpoints[step_num]
        reverted = []
        for abs_path, content in checkpoint.items():
            try:
                if content is not None:
                    self.backend.write_file(abs_path, content)
                    reverted.append(abs_path)
            except Exception as e:
                logger.warning(f"Failed to revert {abs_path} to step {step_num}: {e}")
        # Remove checkpoints after this step
        for s in list(self._step_checkpoints.keys()):
            if s > step_num:
                del self._step_checkpoints[s]
        self._plan_step_index = step_num
        return reverted

    # ------------------------------------------------------------------
    # Project rules (Cursor-style .cursor/rules, .cursorrules, RULE.md)
    # ------------------------------------------------------------------

    _PROJECT_RULES_MAX_CHARS = 8000

    def _load_project_rules(self) -> str:
        """Load project rule files and return concatenated content for system prompt.
        Tries: .cursorrules, RULE.md, .cursor/RULE.md, .cursor/rules/*.mdc, .cursor/rules/*.md.
        Capped at _PROJECT_RULES_MAX_CHARS."""
        parts: List[str] = []
        total = 0

        def _add(path: str, label: str) -> None:
            nonlocal total
            if total >= self._PROJECT_RULES_MAX_CHARS:
                return
            try:
                if not self.backend.file_exists(path):
                    return
                content = self.backend.read_file(path).strip()
                if not content:
                    return
                chunk = f"--- {label} ---\n{content}" if label else content
                take = min(len(chunk), self._PROJECT_RULES_MAX_CHARS - total)
                if take > 0:
                    parts.append(chunk[:take])
                    total += take
            except Exception as e:
                logger.debug(f"Could not load project rule {path}: {e}")

        _add(".cursorrules", "cursorrules")
        _add("RULE.md", "RULE.md")
        _add(".cursor/RULE.md", ".cursor/RULE.md")

        try:
            if self.backend.file_exists(".cursor/rules") and self.backend.is_dir(".cursor/rules"):
                entries = self.backend.list_dir(".cursor/rules")
                for ent in sorted(entries, key=lambda e: e.get("name", "")):
                    if ent.get("type") != "file":
                        continue
                    name = ent.get("name", "")
                    if not name.endswith((".mdc", ".md")):
                        continue
                    _add(f".cursor/rules/{name}", f".cursor/rules/{name}")
        except Exception as e:
            logger.debug(f"Could not list .cursor/rules: {e}")

        if not parts:
            return ""
        return "\n\n".join(parts)

    _PROJECT_DOCS_MAX_CHARS = 6000

    def _load_project_docs(self) -> str:
        """Load project-docs/ and key root docs (README, CONTRIBUTING) for context injection."""
        parts: List[str] = []
        total = 0

        def _add(path: str, label: str) -> None:
            nonlocal total
            if total >= self._PROJECT_DOCS_MAX_CHARS:
                return
            try:
                if not self.backend.file_exists(path):
                    return
                content = self.backend.read_file(path).strip()
                if not content:
                    return
                chunk = f"--- {label} ---\n{content}"
                take = min(len(chunk), self._PROJECT_DOCS_MAX_CHARS - total)
                if take > 0:
                    parts.append(chunk[:take])
                    total += take
            except Exception as e:
                logger.debug(f"Could not load project doc {path}: {e}")

        # Prefer project-docs/ then root docs
        doc_names = ["overview.md", "tech-specs.md", "requirements.md", "index.md", "README.md"]
        for name in doc_names:
            _add(f"project-docs/{name}", f"project-docs/{name}")
        _add("README.md", "README.md")
        _add("CONTRIBUTING.md", "CONTRIBUTING.md")

        try:
            if self.backend.file_exists("project-docs") and self.backend.is_dir("project-docs"):
                entries = self.backend.list_dir("project-docs")
                for ent in sorted(entries, key=lambda e: e.get("name", "")):
                    if ent.get("type") != "file":
                        continue
                    name = ent.get("name", "")
                    if not name.lower().endswith((".md", ".mdx", ".txt")):
                        continue
                    if name in doc_names:
                        continue  # already added
                    _add(f"project-docs/{name}", f"project-docs/{name}")
        except Exception as e:
            logger.debug(f"Could not list project-docs: {e}")

        if not parts:
            return ""
        return "\n\n".join(parts)

    def _effective_system_prompt(self, base: str) -> str:
        """Return system prompt with project rules appended when present."""
        rules = self._load_project_rules()
        if not rules:
            return base
        return base + "\n\n<project_rules>\nThese project-specific rules MUST be followed:\n\n" + rules + "\n</project_rules>"

    # ------------------------------------------------------------------
    # File snapshots — capture originals before modifications
    # ------------------------------------------------------------------

    def _discover_test_files(self, modified_paths: List[str]) -> List[str]:
        """Find test files that likely correspond to modified source files.
        Searches for common test naming patterns."""
        test_files = []
        seen = set()
        for abs_path in modified_paths:
            base = os.path.basename(abs_path)
            name, ext = os.path.splitext(base)
            dir_path = os.path.dirname(abs_path)

            # Common test file patterns
            candidates = [
                os.path.join(dir_path, f"test_{name}{ext}"),
                os.path.join(dir_path, f"{name}_test{ext}"),
                os.path.join(dir_path, f"{name}.test{ext}"),
                os.path.join(dir_path, f"{name}.spec{ext}"),
                # Test directory variants
                os.path.join(dir_path, "tests", f"test_{name}{ext}"),
                os.path.join(dir_path, "test", f"test_{name}{ext}"),
                os.path.join(dir_path, "__tests__", f"{name}.test{ext}"),
                os.path.join(dir_path, "__tests__", f"{name}.spec{ext}"),
                # Parent test directory
                os.path.join(os.path.dirname(dir_path), "tests", f"test_{name}{ext}"),
                os.path.join(os.path.dirname(dir_path), "test", f"test_{name}{ext}"),
            ]
            # Also check for TypeScript/JS specific patterns
            if ext in (".ts", ".tsx", ".js", ".jsx"):
                js_ext = ext
                candidates.extend([
                    os.path.join(dir_path, f"{name}.test{js_ext}"),
                    os.path.join(dir_path, f"{name}.spec{js_ext}"),
                    os.path.join(dir_path, "__tests__", f"{name}{js_ext}"),
                ])

            for candidate in candidates:
                if candidate not in seen:
                    try:
                        # Check if the file exists
                        self.backend.read_file(candidate)
                        rel = os.path.relpath(candidate, self.working_directory)
                        test_files.append(rel)
                        seen.add(candidate)
                    except Exception:
                        pass
        return test_files

    def _snapshot_file(self, tool_name: str, tool_input: Dict[str, Any]) -> None:
        """Capture the original content of a file before it's modified.
        Only snapshots once per file per build run — first write wins."""
        if tool_name not in ("write_file", "edit_file"):
            return

        rel_path = tool_input.get("path", "")
        abs_path = self.backend.resolve_path(rel_path)

        if abs_path in self._file_snapshots:
            return  # already snapshotted

        try:
            self._file_snapshots[abs_path] = self.backend.read_file(rel_path)
        except FileNotFoundError:
            self._file_snapshots[abs_path] = None  # new file
        except Exception:
            self._file_snapshots[abs_path] = None

    def clear_snapshots(self) -> None:
        """Clear all file snapshots (called after user keeps or reverts)."""
        self._file_snapshots = {}

    def revert_all(self) -> List[str]:
        """Revert all modified files to their original content.
        Returns a list of reverted file paths."""
        reverted = []
        for abs_path, original in self._file_snapshots.items():
            try:
                if original is None:
                    # File was created by the agent — delete it
                    if self.backend.file_exists(abs_path):
                        self.backend.remove_file(abs_path)
                        reverted.append(abs_path)
                else:
                    self.backend.write_file(abs_path, original)
                    reverted.append(abs_path)
            except Exception as e:
                logger.error(f"Failed to revert {abs_path}: {e}")
        self._file_snapshots = {}
        return reverted

    # ------------------------------------------------------------------
    # Approval memory – skip re-prompting for previously-approved ops
    # ------------------------------------------------------------------

    def _approval_key(self, tool_name: str, tool_input: Dict[str, Any]) -> str:
        """Return a hashable key that uniquely identifies an operation for approval purposes."""
        if tool_name == "run_command":
            # Match on the exact command string
            return f"cmd:{tool_input.get('command', '')}"
        elif tool_name in ("write_file", "edit_file"):
            # Match on (operation, absolute path)
            path = tool_input.get("path", "")
            return f"{tool_name}:{os.path.abspath(os.path.join(self.working_directory, path))}"
        return f"{tool_name}:{json.dumps(tool_input, sort_keys=True)}"

    def was_previously_approved(self, tool_name: str, tool_input: Dict[str, Any]) -> bool:
        """Check whether this exact operation was already approved in this session."""
        key = self._approval_key(tool_name, tool_input)
        return key in self._approved_commands

    def remember_approval(self, tool_name: str, tool_input: Dict[str, Any]) -> None:
        """Remember that the user approved this operation."""
        key = self._approval_key(tool_name, tool_input)
        self._approved_commands.add(key)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Serialize agent state for session persistence."""
        # Serialize file snapshots — skip binary files (> 1MB or decode failure)
        snapshots = {}
        for path, content in self._file_snapshots.items():
            if content is None:
                snapshots[path] = None  # new file marker
            elif len(content) < 1_000_000:
                try:
                    content.encode("utf-8")  # verify it's text
                    snapshots[path] = content
                except (UnicodeDecodeError, UnicodeEncodeError):
                    pass  # skip binary files

        return {
            "history": self.history,
            "token_usage": {
                "input_tokens": self._total_input_tokens,
                "output_tokens": self._total_output_tokens,
                "cache_read_tokens": self._cache_read_tokens,
                "cache_write_tokens": self._cache_write_tokens,
            },
            "approved_commands": list(self._approved_commands),
            "running_summary": self._running_summary,
            "current_plan": self._current_plan,
            "scout_context": self._scout_context,
            "file_snapshots": snapshots,
            "plan_step_index": self._plan_step_index,
        }

    def from_dict(self, data: Dict[str, Any]) -> None:
        """Restore agent state from a persisted session."""
        self.history = data.get("history", [])
        usage = data.get("token_usage", {})
        self._total_input_tokens = usage.get("input_tokens", 0)
        self._total_output_tokens = usage.get("output_tokens", 0)
        self._cache_read_tokens = usage.get("cache_read_tokens", 0)
        self._cache_write_tokens = usage.get("cache_write_tokens", 0)
        self._approved_commands = set(data.get("approved_commands", []))
        self._running_summary = data.get("running_summary", "")
        self._current_plan = data.get("current_plan")
        self._scout_context = data.get("scout_context")
        self._plan_step_index = data.get("plan_step_index", 0)
        self._cancelled = False
        # Restore file snapshots
        raw_snapshots = data.get("file_snapshots", {})
        if isinstance(raw_snapshots, dict):
            self._file_snapshots = raw_snapshots
        else:
            self._file_snapshots = {}

    def _default_config(self) -> GenerationConfig:
        """Create default generation config from environment settings"""
        model_id = self.service.model_id
        return GenerationConfig(
            max_tokens=model_config.max_tokens,
            enable_thinking=model_config.enable_thinking and supports_thinking(model_id),
            thinking_budget=model_config.thinking_budget if supports_thinking(model_id) else 0,
            throughput_mode=model_config.throughput_mode,
        )

    # ------------------------------------------------------------------
    # Context window management — intelligent, like Cursor
    # ------------------------------------------------------------------

    def _estimate_tokens(self, text: str) -> int:
        """Token estimate: ~3.5 chars per token for mixed English/code."""
        return max(1, int(len(text) / 3.5))

    def _block_tokens(self, block: Any) -> int:
        """Estimate tokens in a single content block."""
        if isinstance(block, str):
            return self._estimate_tokens(block)
        if isinstance(block, dict):
            total = 10  # overhead for block structure
            for key in ("text", "thinking", "content"):
                val = block.get(key, "")
                if isinstance(val, str):
                    total += self._estimate_tokens(val)
            inp = block.get("input")
            if isinstance(inp, dict):
                total += self._estimate_tokens(json.dumps(inp))
            return total
        return 0

    def _message_tokens(self, msg: Dict[str, Any]) -> int:
        """Estimate tokens in a single message."""
        content = msg.get("content", "")
        if isinstance(content, str):
            return self._estimate_tokens(content) + 5
        if isinstance(content, list):
            return sum(self._block_tokens(b) for b in content) + 5
        return 5

    def _total_history_tokens(self) -> int:
        """Estimate total tokens across all history messages."""
        return sum(self._message_tokens(m) for m in self.history)

    def _extract_file_paths_from_history(self) -> set:
        """Find file paths referenced in the last few messages (working set)."""
        paths = set()
        # Look at last 8 messages for recently referenced files
        for msg in self.history[-8:]:
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        inp = block.get("input", {})
                        if isinstance(inp, dict) and "path" in inp:
                            paths.add(inp["path"])
        return paths

    def _compress_tool_result(self, text: str, tool_name: str, is_hot: bool) -> str:
        """Intelligently compress a tool result, keeping what matters.
        For file reads, preserves structural info (function/class signatures)
        so the model retains understanding of file contents for audit-style work."""
        if len(text) < 500:
            return text

        lines = text.split("\n")

        if tool_name == "read_file":
            if is_hot:
                # Hot file (recently edited) — keep generous context
                if len(lines) > 60:
                    return "\n".join(
                        lines[:30]
                        + [f"  ... ({len(lines) - 40} lines omitted, file in working set) ..."]
                        + lines[-10:]
                    )
                return text
            else:
                # Cold file — keep structure (signatures, imports) + head + tail
                # This preserves the "shape" of the file for cross-file analysis
                if len(lines) > 30:
                    # Extract structural lines (imports, class/function defs)
                    structure = []
                    for i, line in enumerate(lines):
                        stripped = line.lstrip("0123456789| ").strip()
                        if (stripped.startswith(("import ", "from ", "class ", "def ", "async def ", "@"))
                                or stripped.startswith(("export ", "function ", "const ", "interface ", "type "))):
                            structure.append(lines[i])

                    head = lines[:10]  # first 10 lines (header, imports)
                    tail = lines[-5:]   # last 5 lines

                    parts = head
                    if structure:
                        parts += ["", "  [structure — function/class signatures:]"]
                        # Dedupe with head/tail and cap
                        seen = set(l.strip() for l in head + tail)
                        for s in structure[:30]:
                            if s.strip() not in seen:
                                parts.append(s)
                                seen.add(s.strip())
                    parts += [f"  ... ({len(lines) - len(parts) - 5} lines omitted) ..."]
                    parts += tail

                    return "\n".join(parts)
                return text

        # For search results: keep first matches + count
        if tool_name == "search":
            if len(lines) > 20:
                return "\n".join(lines[:15] + [f"  ... ({len(lines) - 15} more matches) ..."])

        # For command output: keep first + last
        if tool_name == "run_command":
            if len(lines) > 30:
                return "\n".join(
                    lines[:12]
                    + [f"  ... ({len(lines) - 17} lines omitted) ..."]
                    + lines[-5:]
                )

        # For directory listings: keep entries
        if tool_name in ("list_directory", "glob_find"):
            if len(lines) > 40:
                return "\n".join(lines[:30] + [f"  ... ({len(lines) - 30} more entries) ..."])

        # Generic: keep meaningful amount
        if len(text) > 1000:
            return text[:600] + f"\n... ({len(text) - 600} chars omitted) ..."

        return text

    def _summarize_old_messages(self, messages: List[Dict[str, Any]]) -> str:
        """Create a concise summary of old conversation messages.
        Tries an LLM call (Haiku) for quality; falls back to heuristics."""
        # ── Try LLM-based summary (much better quality) ──
        try:
            text_parts = []
            for msg in messages:
                role = msg.get("role", "?")
                content = msg.get("content", "")
                if isinstance(content, str):
                    text_parts.append(f"[{role}]: {content[:500]}")
                elif isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict):
                            if b.get("type") == "text":
                                text_parts.append(f"[{role}]: {b['text'][:500]}")
                            elif b.get("type") == "tool_use":
                                text_parts.append(f"[tool]: {b.get('name', '?')}({json.dumps(b.get('input', {}))[:200]})")
                            elif b.get("type") == "tool_result":
                                text_parts.append(f"[result]: {str(b.get('content', ''))[:300]}")
            
            conversation_text = "\n".join(text_parts)
            # Cap to avoid blowing Haiku's context
            if len(conversation_text) > 30000:
                conversation_text = conversation_text[:15000] + "\n...\n" + conversation_text[-15000:]

            summary_config = GenerationConfig(
                max_tokens=2000,
                enable_thinking=False,
                thinking_budget=0,
                throughput_mode="cross-region",
            )
            result = self.service.generate_response(
                messages=[{"role": "user", "content": conversation_text}],
                system_prompt=(
                    "Summarize this coding agent conversation segment concisely. Focus on:\n"
                    "1. What task was the user working on?\n"
                    "2. What files were read and modified?\n"
                    "3. What key decisions were made and why?\n"
                    "4. What is the current state?\n"
                    "Be concise — this summary replaces the original messages in context.\n"
                    "Use bullet points. Max 500 words."
                ),
                model_id=app_config.scout_model,
                config=summary_config,
            )
            if result.content and result.content.strip():
                return f"<conversation_summary>\n{result.content.strip()}\n</conversation_summary>"
        except Exception as e:
            logger.debug(f"LLM summary failed, falling back to heuristic: {e}")

        # ── Fallback: heuristic-based summary ──
        return self._summarize_old_messages_heuristic(messages)

    def _summarize_old_messages_heuristic(self, messages: List[Dict[str, Any]]) -> str:
        """Heuristic-based summary — fast fallback when LLM is unavailable."""
        actions = []
        files_read = []
        files_edited = []
        commands_run = []
        key_decisions = []

        for msg in messages:
            content = msg.get("content", [])
            if isinstance(content, str):
                if msg.get("role") == "user" and len(content) < 200:
                    continue
                continue
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "tool_use":
                    name = block.get("name", "")
                    inp = block.get("input", {})
                    if name == "read_file":
                        files_read.append(inp.get("path", "?"))
                    elif name in ("write_file", "edit_file"):
                        files_edited.append(inp.get("path", "?"))
                    elif name == "run_command":
                        commands_run.append(inp.get("command", "?")[:80])
                    elif name == "search":
                        actions.append(f"searched for '{inp.get('pattern', '?')}'")
                elif btype == "text":
                    text = block.get("text", "").strip()
                    if text and len(text) < 300:
                        key_decisions.append(text)

        parts = ["<conversation_summary>", "Earlier work:"]
        if files_read:
            parts.append(f"- Read: {', '.join(list(dict.fromkeys(files_read))[:15])}")
        if files_edited:
            parts.append(f"- Edited: {', '.join(list(dict.fromkeys(files_edited))[:15])}")
        if commands_run:
            parts.append(f"- Commands: {'; '.join(commands_run[:8])}")
        if actions:
            parts.append(f"- Other: {'; '.join(actions[:5])}")
        if key_decisions:
            parts.append("- Notes:")
            for d in key_decisions[-5:]:
                parts.append(f"  - {d[:150]}")
        parts.append("</conversation_summary>")
        return "\n".join(parts)

    def _current_token_estimate(self) -> int:
        """Best-effort estimate of current context size.
        Uses real API token count + delta estimate, falls back to pure estimate."""
        if self._total_input_tokens > 0:
            msgs_since = max(0, len(self.history) - self._history_len_at_last_call)
            extra = sum(
                self._message_tokens(self.history[-i - 1])
                for i in range(msgs_since) if i < len(self.history)
            )
            return self._total_input_tokens + extra
        return self._total_history_tokens()

    def _trim_history(self) -> None:
        """Proactive, multi-tier context management. Runs every iteration.

        Tier 1 (>50% full): Gentle — strip old thinking, compress cold file reads.
        Tier 2 (>70% full): Aggressive — summarize old messages, compress hot files.
        Tier 3 (>85% full): Emergency — drop to summary + recent messages only.

        Because tool results are already capped at ingestion (_cap_tool_results),
        Tier 1 is usually sufficient. Tiers 2-3 are safety nets.
        """
        context_window = get_context_window(self.service.model_id)
        tier1_limit = int(context_window * 0.50)   # gentle compression starts
        tier2_limit = int(context_window * 0.70)    # aggressive compression
        tier3_limit = int(context_window * 0.85)    # emergency

        current = self._current_token_estimate()
        if current <= tier1_limit:
            return  # plenty of room

        hot_files = self._extract_file_paths_from_history()
        safe_tail = min(6, len(self.history))

        # ── Tier 1: Gentle compression (>50%) ─────────────────────────
        # Strip thinking from old messages, compress cold file reads
        logger.info(
            f"Context tier 1: ~{current:,} tokens > {tier1_limit:,} soft limit. "
            f"{len(self.history)} messages."
        )

        for i in range(max(0, len(self.history) - safe_tail)):
            msg = self.history[i]
            content = msg.get("content")

            if isinstance(content, str) and len(content) > 3000:
                self.history[i]["content"] = content[:800] + "\n... (earlier context compressed) ..."
                continue
            if not isinstance(content, list):
                continue

            for j, block in enumerate(content):
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")

                # Always strip old thinking — it's huge and the model doesn't need it
                if btype == "thinking":
                    content[j] = {"type": "thinking", "thinking": "..."}
                    if block.get("signature"):
                        content[j]["signature"] = block["signature"]

                # Compress tool results (cold files aggressively, hot files gently)
                elif btype == "tool_result":
                    text = block.get("content", "")
                    if isinstance(text, str) and len(text) > 400:
                        tool_name = self._find_tool_name_for_result(
                            block.get("tool_use_id", ""), i
                        )
                        is_hot = any(hp in text[:500] for hp in hot_files) if hot_files else False
                        compressed = self._compress_tool_result(text, tool_name, is_hot)
                        content[j] = {**block, "content": compressed}

                # Compress long assistant text
                elif btype == "text":
                    text = block.get("text", "")
                    if len(text) > 1500:
                        paragraphs = text.split("\n\n")
                        if len(paragraphs) > 3:
                            content[j] = {
                                **block,
                                "text": (
                                    "\n\n".join(paragraphs[:2])
                                    + f"\n\n... ({len(paragraphs) - 3} paragraphs omitted) ...\n\n"
                                    + paragraphs[-1]
                                ),
                            }

        current = self._total_history_tokens()
        if current <= tier2_limit:
            logger.info(f"Context tier 1 sufficient: ~{current:,} tokens")
            return

        # ── Tier 2: Aggressive — summarize old messages (>70%) ────────
        logger.info(f"Context tier 2: ~{current:,} tokens > {tier2_limit:,}. Summarizing.")

        ratio = current / tier2_limit
        if ratio > 3:
            keep_last = min(4, len(self.history))
        elif ratio > 1.5:
            keep_last = min(6, len(self.history))
        else:
            keep_last = min(8, len(self.history))

        # Merge the running summary with newly summarized messages
        keep_first = 1
        if len(self.history) > keep_first + keep_last:
            old_messages = self.history[keep_first:-keep_last]
            summary = self._summarize_old_messages(old_messages)

            # If there's an existing summary, merge it
            if self._running_summary:
                summary = self._running_summary + "\n\n" + summary
            self._running_summary = summary

            self.history = (
                self.history[:keep_first]
                + [{"role": "user", "content": summary}]
                + self.history[-keep_last:]
            )

            current = self._total_history_tokens()
            logger.info(
                f"Context tier 2: summarized {len(old_messages)} messages. "
                f"~{current:,} tokens, {len(self.history)} messages"
            )

        if current <= tier3_limit:
            return

        # ── Tier 3: Emergency (>85%) — drop everything non-essential ──
        logger.info(f"Context tier 3 emergency: ~{current:,} tokens > {tier3_limit:,}")

        # Drop all thinking blocks entirely
        for msg in self.history[:-1]:
            content = msg.get("content")
            if isinstance(content, list):
                msg["content"] = [
                    b for b in content
                    if not (isinstance(b, dict) and b.get("type") == "thinking")
                ]
        current = self._total_history_tokens()

        if current > tier3_limit:
            # Compress everything to bare minimum
            for msg in self.history[:-1]:
                content = msg.get("content")
                if isinstance(content, list):
                    for j, block in enumerate(content):
                        if isinstance(block, dict):
                            for key in ("content", "text"):
                                val = block.get(key, "")
                                if isinstance(val, str) and len(val) > 100:
                                    content[j] = {**block, key: val[:80] + " (trimmed)"}
                elif isinstance(content, str) and len(content) > 500:
                    msg["content"] = content[:200] + " (trimmed)"
            current = self._total_history_tokens()

        if current > tier3_limit and len(self.history) > 3:
            # Last resort: keep only first message + summary + last 2
            first = self.history[0]
            last_two = self.history[-2:]
            summary_msg = {"role": "user", "content": self._running_summary or "(earlier work trimmed)"}
            self.history = [first, summary_msg] + last_two
            current = self._total_history_tokens()

        if current > tier3_limit:
            # Absolute final: compress even the last messages
            for msg in self.history:
                content = msg.get("content")
                if isinstance(content, list):
                    for j, block in enumerate(content):
                        if isinstance(block, dict):
                            for key in ("content", "text", "thinking"):
                                val = block.get(key, "")
                                if isinstance(val, str) and len(val) > 100:
                                    content[j] = {**block, key: val[:80] + " (trimmed)"}
            current = self._total_history_tokens()

        logger.info(f"Context tier 3 done: ~{current:,} tokens, {len(self.history)} messages")

    def _repair_history(self) -> None:
        """Validate and repair conversation history before each API call.

        Fixes orphaned tool_use blocks that don't have matching tool_result
        in the next message. This can happen after stream failures, context
        trimming, or session restore. The API rejects such histories.
        """
        if len(self.history) < 2:
            return

        repaired = False
        i = 0
        while i < len(self.history):
            msg = self.history[i]
            if msg.get("role") != "assistant":
                i += 1
                continue

            content = msg.get("content", [])
            if not isinstance(content, list):
                i += 1
                continue

            # Collect tool_use IDs from this assistant message
            tool_use_ids = set()
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tid = block.get("id", "")
                    if tid:
                        tool_use_ids.add(tid)

            if not tool_use_ids:
                i += 1
                continue

            # Check if the next message has matching tool_results
            next_idx = i + 1
            if next_idx >= len(self.history):
                # Last message is an assistant with tool_use — orphaned
                # Remove the tool_use blocks, keep text/thinking
                cleaned = [
                    b for b in content
                    if not (isinstance(b, dict) and b.get("type") == "tool_use")
                ]
                if cleaned:
                    self.history[i]["content"] = cleaned
                else:
                    self.history.pop(i)
                repaired = True
                logger.warning(f"Repaired orphaned tool_use at end of history (msg {i})")
                continue  # re-check from same index

            next_msg = self.history[next_idx]
            next_content = next_msg.get("content", [])

            # Collect tool_result IDs from the next message
            result_ids = set()
            if isinstance(next_content, list):
                for block in next_content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        result_ids.add(block.get("tool_use_id", ""))

            # Check for missing results
            missing = tool_use_ids - result_ids
            if missing:
                if next_msg.get("role") == "user" and result_ids:
                    # Some results present, some missing — add dummy results
                    for mid in missing:
                        next_content.append({
                            "type": "tool_result",
                            "tool_use_id": mid,
                            "content": "(result unavailable — recovered from stream failure)",
                            "is_error": True,
                        })
                    self.history[next_idx]["content"] = next_content
                    repaired = True
                    logger.warning(
                        f"Added {len(missing)} dummy tool_results at msg {next_idx}"
                    )
                elif next_msg.get("role") != "user":
                    # Next message isn't even a user message — insert one
                    dummy_results = []
                    for mid in tool_use_ids:
                        dummy_results.append({
                            "type": "tool_result",
                            "tool_use_id": mid,
                            "content": "(result unavailable — recovered from stream failure)",
                            "is_error": True,
                        })
                    self.history.insert(next_idx, {
                        "role": "user",
                        "content": dummy_results,
                    })
                    repaired = True
                    logger.warning(
                        f"Inserted dummy tool_result message at {next_idx} "
                        f"for {len(tool_use_ids)} orphaned tool_use blocks"
                    )

            i += 1

        if repaired:
            logger.info(f"History repaired. {len(self.history)} messages.")

    def _find_tool_name_for_result(self, tool_use_id: str, before_idx: int) -> str:
        """Look backwards in history to find which tool produced a given result."""
        for i in range(before_idx, -1, -1):
            content = self.history[i].get("content", [])
            if isinstance(content, list):
                for block in content:
                    if (isinstance(block, dict)
                            and block.get("type") == "tool_use"
                            and block.get("id") == tool_use_id):
                        return block.get("name", "")
        return ""

    def _adaptive_result_cap(self) -> int:
        """Return the max chars per tool result based on how full the context is.
        Generous when there's room; tight when approaching the limit."""
        context_window = get_context_window(self.service.model_id)
        current = self._current_token_estimate()
        usage = current / context_window if context_window > 0 else 0

        if usage < 0.25:
            return 50000   # ~14k tokens — very generous, full file reads
        elif usage < 0.40:
            return 30000   # ~8.5k tokens — moderate
        elif usage < 0.55:
            return 20000   # ~5.7k tokens — getting tighter
        elif usage < 0.70:
            return 14000   # ~4k tokens — compact
        else:
            return 8000    # ~2.3k tokens — tight, preserve room

    def _cap_tool_results(self, tool_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Cap tool result content at ingestion — adaptive based on context usage.
        Generous early in the session, progressively tighter as context fills."""
        cap = self._adaptive_result_cap()
        capped = []
        for result in tool_results:
            text = result.get("content", "")
            if isinstance(text, str) and len(text) > cap:
                lines = text.split("\n")
                if len(lines) > 50:
                    # Proportional head/tail based on budget
                    head_n = max(20, cap // 400)
                    tail_n = max(10, cap // 800)
                    head = "\n".join(lines[:head_n])
                    tail = "\n".join(lines[-tail_n:])
                    text = (
                        head
                        + f"\n\n... ({len(lines) - head_n - tail_n} lines omitted to fit context budget"
                        + f" — use offset/limit to read specific sections) ...\n\n"
                        + tail
                    )
                else:
                    text = text[:cap - 200] + "\n... (output truncated to fit context budget) ..."
                capped.append({**result, "content": text})
            else:
                capped.append(result)
        return capped

    # ------------------------------------------------------------------
    # Scout sub-agent — fast read-only reconnaissance
    # ------------------------------------------------------------------

    async def _run_scout(
        self,
        task: str,
        on_event: Callable[[AgentEvent], Awaitable[None]],
    ) -> Optional[str]:
        """
        Run a fast, cheap model (Haiku) to gather codebase context before the
        main agent starts. Returns a context summary string, or None if scouting
        is disabled or fails.
        """
        if not app_config.scout_enabled:
            return None

        await on_event(AgentEvent(type="scout_start", content="Scouting codebase..."))

        scout_system = SCOUT_SYSTEM_PROMPT.format(working_directory=self.working_directory)
        scout_config = GenerationConfig(
            max_tokens=8192,
            enable_thinking=False,
            thinking_budget=0,
            throughput_mode="cross-region",
        )
        scout_user_content = f"Explore this codebase and gather context for the following task:\n\n{task}"
        project_docs = self._load_project_docs()
        if project_docs:
            scout_user_content = f"<project_context>\n{project_docs}\n</project_context>\n\n" + scout_user_content
        scout_messages: List[Dict[str, Any]] = [
            {"role": "user", "content": scout_user_content}
        ]

        loop = asyncio.get_event_loop()
        scout_iteration = 0
        max_iters = app_config.scout_max_iterations

        try:
            while scout_iteration < max_iters and not self._cancelled:
                scout_iteration += 1

                # Non-streaming call to Haiku (fast, no thinking)
                result = await loop.run_in_executor(
                    None,
                    lambda: self.service.generate_response(
                        messages=scout_messages,
                        system_prompt=self._effective_system_prompt(scout_system),
                        model_id=app_config.scout_model,
                        config=scout_config,
                        tools=SCOUT_TOOL_DEFINITIONS,
                    ),
                )

                # Track tokens (attribute to cache counters so they're visible)
                self._total_input_tokens += result.input_tokens
                self._total_output_tokens += result.output_tokens

                # Build assistant content for history
                assistant_content: List[Dict[str, Any]] = []
                if result.content:
                    assistant_content.append({"type": "text", "text": result.content})
                for tu in result.tool_uses:
                    assistant_content.append({
                        "type": "tool_use",
                        "id": tu.id,
                        "name": tu.name,
                        "input": tu.input,
                    })

                scout_messages.append({"role": "assistant", "content": assistant_content})

                if not result.tool_uses:
                    # Scout is done — its final text is the context summary
                    await on_event(AgentEvent(
                        type="scout_end",
                        content=f"Scout finished ({scout_iteration} iterations)",
                    ))
                    return result.content.strip() if result.content else None

                # Execute scout tools in parallel (all safe/read-only)
                async def _exec_scout_tool(tu) -> tuple:
                    r = await loop.run_in_executor(
                        None, lambda: execute_tool(tu.name, tu.input, self.working_directory, backend=self.backend)
                    )
                    return tu, r

                tool_results_raw = await asyncio.gather(
                    *[_exec_scout_tool(tu) for tu in result.tool_uses]
                )

                tool_results = []
                for tu, tr in tool_results_raw:
                    text = tr.output if tr.success else (tr.error or "Unknown error")
                    # Cap scout tool results to avoid blowing Haiku's context
                    if isinstance(text, str) and len(text) > 8000:
                        lines = text.split("\n")
                        text = "\n".join(lines[:60]) + f"\n... ({len(lines) - 60} lines omitted) ..."
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": text,
                        "is_error": not tr.success,
                    })

                    await on_event(AgentEvent(
                        type="scout_progress",
                        content=f"{tu.name}: {tu.input.get('path', tu.input.get('pattern', '?'))}",
                    ))

                scout_messages.append({"role": "user", "content": tool_results})

            await on_event(AgentEvent(
                type="scout_end",
                content=f"Scout done ({scout_iteration} iterations, hit limit)",
            ))

            # Extract last assistant text as summary
            for msg in reversed(scout_messages):
                if msg.get("role") == "assistant":
                    content = msg.get("content", [])
                    if isinstance(content, list):
                        for block in content:
                            if block.get("type") == "text" and block.get("text"):
                                return block["text"].strip()
                    elif isinstance(content, str):
                        return content.strip()
            return None

        except Exception as e:
            logger.warning(f"Scout failed (non-fatal): {e}")
            await on_event(AgentEvent(
                type="scout_end",
                content=f"Scout failed: {e}",
            ))
            return None

    _TASK_REFINE_SYSTEM = """You are a task refiner for a coding agent. Given the user's raw request, output a refined task that includes:

1) **Output specification** (1-2 sentences): What does "done" look like? What should exist or behave after the work is complete?
2) **Constraints** (2-3 bullets): What must NOT be changed? What patterns or files must be preserved? Any explicit "don't" from the user?
3) **Task**: The original request (you may slightly clarify wording but keep the same scope).

Format your response exactly as:

## Output specification
<1-2 sentences>

## Constraints
- <constraint 1>
- <constraint 2>

## Task
<original or lightly clarified task>

Keep the whole response under 300 words. If the request is already very clear and minimal, you may return it with only brief output spec and "None" for constraints."""

    async def _refine_task(self, task: str, on_event: Callable[[AgentEvent], Awaitable[None]]) -> Optional[str]:
        """Refine raw user task into output spec + constraints (Cursor-style). Returns refined text or None."""
        if not task.strip():
            return None
        try:
            await on_event(AgentEvent(type="thinking_start", content=""))
            await on_event(AgentEvent(type="thinking", content="Refining task into output spec and constraints..."))
            await on_event(AgentEvent(type="thinking_end", content=""))
            loop = asyncio.get_event_loop()
            cfg = GenerationConfig(max_tokens=500, enable_thinking=False, throughput_mode="cross-region")
            result = await loop.run_in_executor(
                None,
                lambda: self.service.generate_response(
                    messages=[{"role": "user", "content": task}],
                    system_prompt=self._TASK_REFINE_SYSTEM,
                    model_id=app_config.scout_model,
                    config=cfg,
                ),
            )
            if result.content and result.content.strip():
                return result.content.strip()
        except Exception as e:
            logger.debug(f"Task refinement failed: {e}")
        return None

    # ------------------------------------------------------------------
    # Plan phase — produce a plan, stop, and wait for user
    # ------------------------------------------------------------------

    async def run_plan(
        self,
        task: str,
        on_event: Callable[[AgentEvent], Awaitable[None]],
        request_question_answer: Optional[Callable[[str, Optional[str], str], Awaitable[str]]] = None,
    ) -> Optional[List[str]]:
        """
        Generate a plan for the task using an agentic loop with read-only tools.
        The planner can read files, search, list directories, glob, and optionally
        ask the user clarifying questions (when request_question_answer is provided).

        Returns a list of plan step strings, or None if planning fails/is cancelled.
        The plan is stored in self._current_plan for run_build() to use.
        Also writes the plan to a markdown file on disk.
        """
        self._cancelled = False
        self._current_plan = None

        # Run scout for first message — decision comes from classify_intent in web.py
        # run_plan is only called when intent["plan"]=True, which implies scouting
        scout_context = None
        if app_config.scout_enabled and len(self.history) == 0:
            scout_context = await self._run_scout(task, on_event)
            self._scout_context = scout_context  # cache for build phase

        await on_event(AgentEvent(type="phase_start", content="plan"))

        # Optional: refine task into output spec + constraints before planning
        task_for_plan = task
        if app_config.task_refinement_enabled:
            refined = await self._refine_task(task, on_event)
            if refined:
                task_for_plan = refined

        # Build the planning prompt
        plan_system = PLAN_SYSTEM_PROMPT.format(working_directory=self.working_directory)
        plan_user = task_for_plan
        if scout_context:
            plan_user = (
                f"<codebase_context>\n{scout_context}\n</codebase_context>\n\n"
                f"{plan_user}"
            )
        project_docs = self._load_project_docs()
        if project_docs:
            plan_user = f"<project_context>\n{project_docs}\n</project_context>\n\n" + plan_user

        # Agentic loop with STREAMING + read-only tools so the user
        # sees thinking/text in real time during plan generation
        loop = asyncio.get_event_loop()
        plan_config = GenerationConfig(
            max_tokens=model_config.max_tokens,
            enable_thinking=model_config.enable_thinking and supports_thinking(self.service.model_id),
            thinking_budget=model_config.thinking_budget if supports_thinking(self.service.model_id) else 0,
            throughput_mode=model_config.throughput_mode,
        )

        plan_messages: List[Dict[str, Any]] = [
            {"role": "user", "content": plan_user}
        ]
        max_plan_iters = 50  # generous — let it read as much as it needs
        plan_text = ""

        async def _stream_plan_call(messages, tools_list):
            """Run a single streaming plan LLM call. Returns (text, tool_uses, assistant_content)."""
            cq: queue.Queue = queue.Queue()

            def _producer():
                try:
                    for c in self.service.generate_response_stream(
                        messages=messages,
                        system_prompt=self._effective_system_prompt(plan_system),
                        model_id=None,
                        config=plan_config,
                        tools=tools_list,
                    ):
                        cq.put(c)
                    cq.put(None)
                except Exception as exc:
                    cq.put(exc)

            t = threading.Thread(target=_producer, daemon=True)
            t.start()

            a_content: List[Dict[str, Any]] = []
            c_text = ""
            c_thinking = ""
            c_thinking_sig = None
            t_uses: List[Dict[str, Any]] = []
            c_tool = None
            t_json: List[str] = []

            while True:
                if self._cancelled:
                    t.join(timeout=2)
                    return "", [], []

                chunk = await loop.run_in_executor(None, cq.get)
                if chunk is None:
                    break
                if isinstance(chunk, Exception):
                    raise chunk

                ct = chunk.get("type", "")
                cc = chunk.get("content", "")

                if ct == "thinking_start":
                    c_thinking = ""
                    c_thinking_sig = None
                    await on_event(AgentEvent(type="thinking_start"))
                elif ct == "thinking":
                    c_thinking += cc
                    await on_event(AgentEvent(type="thinking", content=cc))
                elif ct == "thinking_end":
                    c_thinking_sig = chunk.get("signature")
                    tb: Dict[str, Any] = {"type": "thinking", "thinking": c_thinking}
                    if c_thinking_sig:
                        tb["signature"] = c_thinking_sig
                    a_content.append(tb)
                    await on_event(AgentEvent(type="thinking_end"))

                elif ct == "text_start":
                    c_text = ""
                    await on_event(AgentEvent(type="text_start"))
                elif ct == "text":
                    c_text += cc
                    await on_event(AgentEvent(type="text", content=cc))
                elif ct == "text_end":
                    if c_text:
                        a_content.append({"type": "text", "text": c_text})
                    await on_event(AgentEvent(type="text_end"))

                elif ct == "tool_use_start":
                    c_tool = chunk.get("data", {})
                    t_json = []
                elif ct == "tool_use_delta":
                    t_json.append(cc)
                elif ct == "tool_use_end":
                    if c_tool:
                        try:
                            inp = json.loads("".join(t_json))
                        except json.JSONDecodeError:
                            inp = {}
                        tb2 = {
                            "type": "tool_use",
                            "id": c_tool.get("id", ""),
                            "name": c_tool.get("name", ""),
                            "input": inp,
                        }
                        a_content.append(tb2)
                        t_uses.append(tb2)
                        await on_event(AgentEvent(
                            type="tool_call",
                            content=c_tool.get("name", ""),
                            data={"id": c_tool.get("id", ""), "name": c_tool.get("name", ""), "input": inp},
                        ))
                        c_tool = None

                elif ct == "usage_start":
                    usage = chunk.get("usage", {})
                    self._total_input_tokens += usage.get("input_tokens", 0)
                    self._cache_read_tokens += usage.get("cache_read_input_tokens", 0)
                elif ct == "message_end":
                    usage = chunk.get("usage", {})
                    self._total_output_tokens += usage.get("output_tokens", 0)

            t.join(timeout=5)
            return c_text, t_uses, a_content

        try:
            nudge_sent = False  # only nudge once

            for plan_iter in range(max_plan_iters):
                if self._cancelled:
                    return None

                await on_event(AgentEvent(
                    type="scout_progress",
                    content=f"Planning: iteration {plan_iter + 1} — {'reading codebase' if plan_iter < 3 else 'analyzing & planning'}...",
                ))

                # After many iterations without concluding, nudge the model
                if plan_iter >= 15 and not nudge_sent:
                    nudge_sent = True
                    plan_messages.append({
                        "role": "user",
                        "content": (
                            "You have read many files and should have a strong understanding by now. "
                            "When you are ready, write the complete implementation plan using the "
                            "plan document format. You may read a few more files if truly needed, "
                            "but prioritize producing the plan."
                        ),
                    })

                # On the absolute last iteration, strip tools to guarantee conclusion
                plan_tools = (SCOUT_TOOL_DEFINITIONS + [ASK_USER_QUESTION_DEFINITION]) if request_question_answer else SCOUT_TOOL_DEFINITIONS
                iter_tools = plan_tools if plan_iter < max_plan_iters - 1 else None

                text, tool_uses, assistant_content = await _stream_plan_call(
                    plan_messages, iter_tools,
                )
                if self._cancelled:
                    return None

                plan_messages.append({"role": "assistant", "content": assistant_content})

                if not tool_uses:
                    plan_text = text.strip()
                    break

                # Split into clarifying questions (need user) vs read-only tools
                question_calls = [tu for tu in tool_uses if tu.get("name") == "ask_user_question"]
                other_calls = [tu for tu in tool_uses if tu.get("name") != "ask_user_question"]

                tool_results = []

                # Handle ask_user_question via callback (Cursor-style clarifying questions)
                for tu in question_calls:
                    inp = tu.get("input", {})
                    question = inp.get("question", "")
                    context = inp.get("context") or ""
                    if request_question_answer and question:
                        try:
                            answer = await request_question_answer(question, context, tu["id"])
                            text_r = f"User answered: {answer}"
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tu["id"],
                                "content": text_r,
                                "is_error": False,
                            })
                            await on_event(AgentEvent(type="tool_result", content=text_r[:200], data={"id": tu["id"], "name": "ask_user_question", "success": True}))
                        except Exception as e:
                            text_r = f"Clarification failed or skipped: {e}"
                            tool_results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": text_r, "is_error": True})
                            await on_event(AgentEvent(type="tool_result", content=text_r, data={"id": tu["id"], "name": "ask_user_question", "success": False}))
                    else:
                        text_r = "Clarification not available; proceed with your best assumption."
                        tool_results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": text_r, "is_error": False})
                        await on_event(AgentEvent(type="tool_result", content=text_r, data={"id": tu["id"], "name": "ask_user_question", "success": True}))

                # Execute read-only tools in parallel
                async def _exec_plan_tool(tu):
                    r = await loop.run_in_executor(
                        None, lambda tu=tu: execute_tool(tu["name"], tu["input"], self.working_directory, backend=self.backend)
                    )
                    return tu, r

                if other_calls:
                    tool_results_raw = await asyncio.gather(
                        *[_exec_plan_tool(tu) for tu in other_calls]
                    )
                    for tu, tr in tool_results_raw:
                        text_r = tr.output if tr.success else (tr.error or "Unknown error")
                        if isinstance(text_r, str) and len(text_r) > 10000:
                            lines = text_r.split("\n")
                            text_r = "\n".join(lines[:80]) + f"\n... ({len(lines) - 80} lines omitted) ..."
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tu["id"],
                            "content": text_r,
                            "is_error": not tr.success,
                        })
                        await on_event(AgentEvent(
                            type="tool_result",
                            content=text_r[:200] if isinstance(text_r, str) else str(text_r)[:200],
                            data={"id": tu["id"], "name": tu["name"], "success": tr.success},
                        ))

                plan_messages.append({"role": "user", "content": tool_results})

            # ── Force a conclusion if the loop ended without a plan ──
            if not plan_text:
                await on_event(AgentEvent(
                    type="scout_progress",
                    content="Planning: finalizing plan document...",
                ))
                # One final call with NO tools — the model MUST produce text
                plan_messages.append({
                    "role": "user",
                    "content": (
                        "You have read enough files. Now produce the COMPLETE implementation "
                        "plan using the plan document format I specified. Include: Why, Approach, "
                        "Affected Files table, numbered Steps with exact file paths and function "
                        "names, Edge Cases & Risks, and Verification commands. Be thorough and specific."
                    ),
                })
                final_text, _, final_content = await _stream_plan_call(
                    plan_messages, None,  # no tools
                )
                if final_text:
                    plan_text = final_text.strip()
                    plan_messages.append({"role": "assistant", "content": final_content})

            if not plan_text:
                await on_event(AgentEvent(type="error", content="Planning produced no output."))
                return None

            # Support legacy <plan> tags
            extracted = _extract_plan(plan_text)
            if extracted:
                plan_text = extracted

            # Parse numbered steps from the Implementation Steps section
            steps = self._parse_plan_steps(plan_text)

            # Quality gate: if the plan is too shallow, force a stricter rewrite.
            # This prevents one-line "todo" outputs from passing through.
            repair_attempts = 0
            while repair_attempts < 2 and not self._plan_quality_sufficient(task_for_plan, plan_text, steps):
                repair_attempts += 1
                await on_event(AgentEvent(
                    type="scout_progress",
                    content=(
                        f"Planning: refining plan quality (attempt {repair_attempts}) — "
                        "requesting explicit multi-item checklist and actionable steps..."
                    ),
                ))
                min_steps = 3 if self._task_looks_multi_item(task_for_plan) else 1
                plan_messages.append({
                    "role": "user",
                    "content": (
                        "Your previous plan was too shallow. Rewrite the COMPLETE plan now.\n\n"
                        "STRICT REQUIREMENTS:\n"
                        "1) Include sections: Why, Approach, Affected Files, Checklist, Steps, Verification.\n"
                        f"2) Provide at least {min_steps} numbered, actionable Steps.\n"
                        "3) Each step must include specific file path + target function/class + exact change.\n"
                        "4) Do NOT output meta/planning chatter (e.g. 'let me check...').\n"
                        "5) If the request has multiple asks, include all asks in Checklist and Steps.\n"
                    ),
                })
                repaired_text, _, repaired_content = await _stream_plan_call(plan_messages, None)
                if not repaired_text:
                    break
                plan_text = repaired_text.strip()
                extracted = _extract_plan(plan_text)
                if extracted:
                    plan_text = extracted
                plan_messages.append({"role": "assistant", "content": repaired_content})
                steps = self._parse_plan_steps(plan_text)

            self._current_plan = steps

            # Write plan to a markdown file on disk
            plan_file_path = self._write_plan_file(task, plan_text)

            # Emit plan steps + full plan text + file path
            await on_event(AgentEvent(
                type="phase_plan",
                content="\n".join(steps),
                data={
                    "steps": steps,
                    "plan_text": plan_text,
                    "plan_file": plan_file_path,
                },
            ))

            return steps

        except Exception as e:
            logger.error(f"Plan phase failed: {e}")
            await on_event(AgentEvent(type="error", content=f"Planning failed: {e}"))
            return None

    @staticmethod
    def _parse_plan_steps(plan_text: str) -> List[str]:
        """Extract actionable numbered steps from the plan document.
        Prefers the explicit Steps section and avoids swallowing generic bullets."""
        # 1) Prefer explicit steps section ("## Steps" / "## Implementation Steps")
        steps_section = None
        sec_match = re.search(
            r"(?ims)^##\s*(?:implementation\s+steps|steps)\s*$\n(.*?)(?=^##\s+|\Z)",
            plan_text,
        )
        if sec_match:
            steps_section = sec_match.group(1).strip()

        target = steps_section if steps_section else plan_text

        # 2) Primary parse: numbered lines only
        steps: List[str] = []
        for raw_line in target.split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            if re.match(r"^\d+[\.\)]\s+", line):
                steps.append(line)
            elif steps and not line.startswith("#") and not re.match(r"^\|.*\|$", line):
                # Continuation line for the previous numbered step
                if raw_line.startswith(" ") or raw_line.startswith("\t") or line.startswith(("-", "*")):
                    steps[-1] += " " + line

        # 3) Fallback parse if none found: structured action bullets only
        if not steps:
            for raw_line in target.split("\n"):
                line = raw_line.strip()
                if re.match(r"^[-*]\s+\*\*\[(EDIT|CREATE|RUN|VERIFY|DELETE)\]\*\*", line, flags=re.IGNORECASE):
                    steps.append(line)

        # 4) Last resort: numbered lines anywhere
        if not steps and plan_text:
            for raw_line in plan_text.split("\n"):
                line = raw_line.strip()
                if re.match(r"^\d+[\.\)]\s+", line):
                    steps.append(line)

        return steps

    @staticmethod
    def _task_looks_multi_item(task: str) -> bool:
        """Heuristic: does the request likely contain multiple distinct items?"""
        if not task:
            return False
        t = task.lower()
        if re.search(r"\n\s*[-*]\s+", t) or re.search(r"\n\s*\d+[\.\)]\s+", t):
            return True
        markers = [
            " also ",
            " then ",
            " next ",
            " in addition ",
            " as well ",
            " after that ",
            " plus ",
        ]
        if any(m in t for m in markers):
            return True
        # Two or more "and" often indicates multiple asks
        return t.count(" and ") >= 2

    @staticmethod
    def _is_actionable_plan_step(step: str) -> bool:
        """Filter out weak/meta steps like 'let me check X'."""
        s = (step or "").strip()
        if len(s) < 20:
            return False
        low = s.lower()
        weak_prefixes = (
            "ok",
            "okay",
            "let me",
            "now let me",
            "i will check",
            "check line",
            "todo",
        )
        if any(low.startswith(p) for p in weak_prefixes):
            return False
        verbs = (
            "edit", "update", "change", "replace", "add", "remove", "create",
            "run", "test", "lint", "verify", "refactor", "fix", "inject",
        )
        return any(v in low for v in verbs)

    def _plan_quality_sufficient(self, task: str, plan_text: str, steps: List[str]) -> bool:
        """Require structured, actionable plans before moving to build."""
        low = (plan_text or "").lower()
        has_steps_section = ("## steps" in low) or ("## implementation steps" in low)
        if not has_steps_section:
            return False
        required_sections = ("## affected files", "## verification")
        if not all(sec in low for sec in required_sections):
            return False

        multi_item = self._task_looks_multi_item(task)
        min_steps = 3 if multi_item else 1
        if len(steps) < min_steps:
            return False

        actionable_count = sum(1 for s in steps if self._is_actionable_plan_step(s))
        return actionable_count >= min_steps

    def _write_plan_file(self, task: str, plan_text: str) -> Optional[str]:
        """Write the plan as a markdown file under .bedrock-codex/plans/.
        Uses the backend so it works over SSH too."""
        try:
            from datetime import datetime
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            # Create a short slug from the task
            slug = re.sub(r"[^a-z0-9]+", "-", task[:50].lower().strip()).strip("-")[:30]
            filename = f"plan-{timestamp}-{slug}.md"
            # Use forward-slash relative path — backend.write_file handles mkdir
            rel_path = f".bedrock-codex/plans/{filename}"
            self.backend.write_file(rel_path, plan_text)
            logger.info(f"Plan written to {rel_path}")
            return rel_path
        except Exception as e:
            logger.warning(f"Failed to write plan file: {e}")
            return None

    # ------------------------------------------------------------------
    # Build phase — execute an approved plan
    # ------------------------------------------------------------------

    async def run_build(
        self,
        task: str,
        plan_steps: List[str],
        on_event: Callable[[AgentEvent], Awaitable[None]],
        request_approval: Callable[[str, str, Dict], Awaitable[bool]],
        config: Optional[GenerationConfig] = None,
    ):
        """
        Execute a previously approved plan. This is the build phase.
        The plan is injected into the conversation so the model follows it.
        """
        self._cancelled = False
        self._file_snapshots = {}  # fresh snapshot tracking per build

        await on_event(AgentEvent(type="phase_start", content="build"))

        # Switch to the build-specific system prompt for plan execution
        saved_prompt = self.system_prompt
        self.system_prompt = BUILD_SYSTEM_PROMPT.format(working_directory=self.working_directory)

        # Build the user message with the approved plan and scout context
        plan_block = "\n".join(plan_steps)
        parts = []
        if self._scout_context:
            parts.append(f"<codebase_context>\n{self._scout_context}\n</codebase_context>")
        parts.append(f"<approved_plan>\n{plan_block}\n</approved_plan>")
        parts.append(task)
        parts.append(
            "Execute this plan step by step.\n\n"
            "Before touching files, first output a short numbered TODO checklist that covers all plan items.\n"
            "Then execute them in order and clearly report progress like 'Todo i of N'.\n\n"
            "For each step:\n"
            "1. State which step you are working on (e.g. 'Step 3: ...')\n"
            "2. Read the target file(s) first — never edit blind\n"
            "3. Make the changes with surgical precision\n"
            "4. Verify: re-read the changed section, run lint_file\n"
            "5. Only move to the next step once this one is verified\n\n"
            "If you discover something the plan missed — a dependency, an edge case, "
            "a better approach — adapt. State what you changed and why."
        )
        user_content = "\n\n".join(parts)

        # Add to history
        self.history.append({"role": "user", "content": user_content})

        # Run the main agent loop
        await self._agent_loop(on_event, request_approval, config)

        # Post-build verification pass
        await self._run_post_build_verification(on_event, request_approval, config)

        # Restore the general-purpose system prompt
        self.system_prompt = saved_prompt

        await on_event(AgentEvent(type="phase_end", content="build"))

    async def _run_post_build_verification(
        self,
        on_event: Callable[[AgentEvent], Awaitable[None]],
        request_approval: Callable[[str, str, Dict], Awaitable[bool]],
        config: Optional[GenerationConfig] = None,
    ):
        """Run a final verification pass after the build loop completes.
        Injects a verification reminder and runs one more loop iteration
        so the model can re-read files, run lints, and fix issues."""
        if self._cancelled:
            return
        # Only verify if there are modified files to check
        if not self._file_snapshots:
            return

        modified = list(self._file_snapshots.keys())
        files_str = ", ".join(os.path.basename(f) for f in modified[:10])
        if len(modified) > 10:
            files_str += f" (+{len(modified) - 10} more)"

        # ── Auto-discover test files for modified files ──
        test_files_found = self._discover_test_files(modified)
        test_section = ""
        if test_files_found:
            test_section = (
                f"\n\nRelevant test files found:\n"
                + "\n".join(f"  - {tf}" for tf in test_files_found[:10])
                + "\nRun these tests and fix any failures."
            )

        verify_msg = (
            f"You have completed all plan steps. Modified files: {files_str}\n\n"
            "Do a final verification pass — this is your quality gate:\n"
            "1. Re-read each modified file. Look for: typos, missing imports, incorrect "
            "variable names, logic errors, incomplete changes.\n"
            "2. Run lint_file on each changed file. Fix any errors.\n"
            f"3. Run relevant tests.{test_section}\n"
            "4. Think: did I miss anything from the plan? Are there edge cases I didn't handle?\n"
            "5. Briefly report what you verified and the results.\n\n"
            "Do NOT skip this. A shipped bug is worse than a slow verification."
        )
        self.history.append({"role": "user", "content": verify_msg})

        # Run one more iteration of the loop for verification
        saved_max = self.max_iterations
        self.max_iterations = saved_max + 20  # give headroom for verify loop
        await self._agent_loop(on_event, request_approval, config)
        self.max_iterations = saved_max

    # ------------------------------------------------------------------
    # Direct run (no plan gate) — for when plan phase is disabled
    # ------------------------------------------------------------------

    async def run(
        self,
        task: str,
        on_event: Callable[[AgentEvent], Awaitable[None]],
        request_approval: Callable[[str, str, Dict], Awaitable[bool]],
        config: Optional[GenerationConfig] = None,
        enable_scout: bool = True,
    ):
        """
        Run the agent on a task. If plan phase is enabled, this is called
        by the TUI which handles the plan->approve->build flow.
        When plan phase is disabled, this runs everything directly.

        enable_scout: Whether to run the scout phase. Set by intent classification.
        """
        self._cancelled = False
        self._file_snapshots = {}  # fresh snapshot tracking per run

        # Run scout for first message — controlled by intent classification
        scout_context = None
        if enable_scout and app_config.scout_enabled and len(self.history) == 0:
            scout_context = await self._run_scout(task, on_event)

        # Build the user message — prepend project context and scout context when available
        project_docs = self._load_project_docs() if len(self.history) == 0 else ""
        if scout_context:
            user_content = (
                f"<codebase_context>\n{scout_context}\n</codebase_context>\n\n"
                f"{task}"
            )
        else:
            user_content = task
        if project_docs:
            user_content = f"<project_context>\n{project_docs}\n</project_context>\n\n" + user_content

        # Add user message
        self.history.append({"role": "user", "content": user_content})

        await self._agent_loop(on_event, request_approval, config)

    # ------------------------------------------------------------------
    # Core agent loop (used by both run and run_build)
    # ------------------------------------------------------------------

    async def _agent_loop(
        self,
        on_event: Callable[[AgentEvent], Awaitable[None]],
        request_approval: Callable[[str, str, Dict], Awaitable[bool]],
        config: Optional[GenerationConfig] = None,
    ):
        """Core streaming agent loop with tool execution."""
        gen_config = config or self._default_config()
        iteration = 0

        while iteration < self.max_iterations and not self._cancelled:
            iteration += 1

            # Soft limit: when approaching max iterations, tell the model to wrap up
            soft_limit = int(self.max_iterations * 0.85)
            if iteration == soft_limit:
                self.history.append({
                    "role": "user",
                    "content": (
                        f"[SYSTEM] You have used {iteration} of {self.max_iterations} iterations. "
                        "You are approaching the limit. Please wrap up your current task — "
                        "summarize what you've done so far and what remains, then stop."
                    ),
                })

            # Trim history if approaching context window limit
            self._trim_history()

            # Validate history — fix orphaned tool_use blocks
            self._repair_history()

            # -----------------------------------------------------------
            # Stream with retry — recovers from connection drops
            # -----------------------------------------------------------
            max_retries = app_config.stream_max_retries
            retry_backoff = app_config.stream_retry_backoff
            stream_succeeded = False

            # Snapshot token counters so we can rollback on retry
            snapshot_input = self._total_input_tokens
            snapshot_output = self._total_output_tokens
            snapshot_cache_read = self._cache_read_tokens
            snapshot_cache_write = self._cache_write_tokens

            for attempt in range(1, max_retries + 1):
                try:
                    # Reset per-attempt accumulators
                    assistant_content = []
                    current_tool_use = None
                    tool_use_json_parts: List[str] = []
                    current_text = ""
                    current_thinking = ""
                    current_thinking_signature: Optional[str] = None

                    # Rollback token counters to pre-attempt snapshot
                    self._total_input_tokens = snapshot_input
                    self._total_output_tokens = snapshot_output
                    self._cache_read_tokens = snapshot_cache_read
                    self._cache_write_tokens = snapshot_cache_write

                    if attempt > 1:
                        # Tell the UI we are retrying
                        await on_event(AgentEvent(
                            type="stream_retry",
                            content=f"Connection lost — retrying ({attempt}/{max_retries})...",
                            data={"attempt": attempt, "max_retries": max_retries},
                        ))

                    chunk_queue: queue.Queue = queue.Queue()

                    def _stream_producer():
                        """Run the sync generator in a background thread, forwarding chunks to the queue."""
                        try:
                            for c in self.service.generate_response_stream(
                                messages=self.history,
                                system_prompt=self._effective_system_prompt(self.system_prompt),
                                model_id=None,
                                config=gen_config,
                                tools=TOOL_DEFINITIONS,
                            ):
                                chunk_queue.put(c)
                            chunk_queue.put(None)  # sentinel: stream complete
                        except Exception as exc:
                            chunk_queue.put(exc)

                    producer_thread = threading.Thread(target=_stream_producer, daemon=True)
                    producer_thread.start()

                    # Consume chunks from the queue in the async loop
                    loop = asyncio.get_event_loop()
                    while True:
                        if self._cancelled:
                            break

                        chunk = await loop.run_in_executor(None, chunk_queue.get)

                        if chunk is None:
                            break  # stream complete
                        if isinstance(chunk, Exception):
                            raise chunk

                        chunk_type = chunk.get("type", "")
                        content = chunk.get("content", "")

                        # --- Thinking events (with continuity) ---
                        if chunk_type == "thinking_start":
                            current_thinking = ""
                            current_thinking_signature = None
                            await on_event(AgentEvent(type="thinking_start"))
                        elif chunk_type == "thinking":
                            current_thinking += content
                            await on_event(AgentEvent(type="thinking", content=content))
                        elif chunk_type == "thinking_end":
                            # Capture signature for thinking continuity
                            current_thinking_signature = chunk.get("signature")
                            # Preserve thinking block in assistant content for multi-turn continuity
                            thinking_block: Dict[str, Any] = {
                                "type": "thinking",
                                "thinking": current_thinking,
                            }
                            if current_thinking_signature:
                                thinking_block["signature"] = current_thinking_signature
                            assistant_content.append(thinking_block)
                            await on_event(AgentEvent(type="thinking_end"))

                        # --- Text events ---
                        elif chunk_type == "text_start":
                            current_text = ""
                            await on_event(AgentEvent(type="text_start"))
                        elif chunk_type == "text":
                            current_text += content
                            await on_event(AgentEvent(type="text", content=content))
                        elif chunk_type == "text_end":
                            if current_text:
                                assistant_content.append({"type": "text", "text": current_text})
                                # Track plan step progress from assistant text
                                new_step = self._detect_plan_step(current_text)
                                if new_step is not None:
                                    await on_event(AgentEvent(
                                        type="plan_step_progress",
                                        content=str(new_step),
                                        data={
                                            "step": new_step,
                                            "total": len(self._current_plan) if self._current_plan else 0,
                                        },
                                    ))
                            await on_event(AgentEvent(type="text_end"))

                        # --- Tool use events ---
                        elif chunk_type == "tool_use_start":
                            current_tool_use = chunk.get("data", {})
                            tool_use_json_parts = []
                        elif chunk_type == "tool_use_delta":
                            tool_use_json_parts.append(content)
                        elif chunk_type == "tool_use_end":
                            if current_tool_use:
                                try:
                                    input_json = json.loads("".join(tool_use_json_parts))
                                except json.JSONDecodeError:
                                    input_json = {}

                                tool_block = {
                                    "type": "tool_use",
                                    "id": current_tool_use.get("id", ""),
                                    "name": current_tool_use.get("name", ""),
                                    "input": input_json,
                                }
                                assistant_content.append(tool_block)

                                await on_event(AgentEvent(
                                    type="tool_call",
                                    content=current_tool_use.get("name", ""),
                                    data={
                                        "id": current_tool_use.get("id", ""),
                                        "name": current_tool_use.get("name", ""),
                                        "input": input_json,
                                    },
                                ))
                                current_tool_use = None

                        # --- Usage / cache metrics ---
                        elif chunk_type == "usage_start":
                            usage = chunk.get("usage", {})
                            self._total_input_tokens += usage.get("input_tokens", 0)
                            self._cache_read_tokens += usage.get("cache_read_input_tokens", 0)
                            self._cache_write_tokens += usage.get("cache_creation_input_tokens", 0)

                        elif chunk_type == "message_end":
                            usage = chunk.get("usage", {})
                            self._total_output_tokens += usage.get("output_tokens", 0)

                    producer_thread.join(timeout=5)
                    stream_succeeded = True
                    self._history_len_at_last_call = len(self.history)
                    break  # exit retry loop — stream completed

                except (BedrockError, Exception) as stream_err:
                    producer_thread.join(timeout=2)

                    # Determine if this error is retryable (connection/timeout/throttle)
                    err_str = str(stream_err).lower()
                    retryable_keywords = [
                        "timeout", "timed out", "connection", "reset by peer",
                        "broken pipe", "eof", "throttl", "serviceunav",
                        "read timeout", "endpoint url", "connect timeout",
                        "network", "socket", "aborted",
                    ]
                    is_retryable = any(kw in err_str for kw in retryable_keywords)

                    if not is_retryable or attempt >= max_retries:
                        # Non-retryable error or exhausted retries
                        if attempt >= max_retries and is_retryable:
                            err_msg = f"Stream failed after {max_retries} retries: {stream_err}"
                        else:
                            err_msg = str(stream_err)
                        logger.error(f"Stream error (attempt {attempt}): {stream_err}")

                        # Rollback: clean up history so it's valid for the API.
                        # The API requires every tool_use to have a matching
                        # tool_result in the immediately following user message.
                        # Remove the last assistant msg if it has orphaned tool_use
                        # blocks, and any trailing user message from this turn.
                        rollback_count = 0

                        # First: remove trailing user message (could be from this
                        # iteration's task submission, or partial tool results)
                        if (self.history
                                and self.history[-1].get("role") == "user"):
                            self.history.pop()
                            rollback_count += 1

                        # Second: if the last message is now an assistant with
                        # tool_use blocks, those are orphaned — remove it too
                        if self.history:
                            last = self.history[-1]
                            if last.get("role") == "assistant":
                                content = last.get("content", [])
                                has_orphan_tool_use = (
                                    isinstance(content, list)
                                    and any(
                                        isinstance(b, dict)
                                        and b.get("type") == "tool_use"
                                        for b in content
                                    )
                                )
                                if has_orphan_tool_use:
                                    self.history.pop()
                                    rollback_count += 1

                        logger.info(
                            f"Rolled back {rollback_count} messages after stream "
                            f"failure ({len(self.history)} remain)"
                        )

                        # Restore token counters to pre-attempt snapshot
                        self._total_input_tokens = snapshot_input
                        self._total_output_tokens = snapshot_output
                        self._cache_read_tokens = snapshot_cache_read
                        self._cache_write_tokens = snapshot_cache_write

                        # Single event with full error — no double display
                        await on_event(AgentEvent(
                            type="stream_failed",
                            content=f"Streaming error: {err_msg}\n\nYour message was rolled back — you can re-send it.",
                        ))
                        stream_succeeded = False
                        break  # exit retry loop

                    # Retryable — wait and try again
                    wait_secs = retry_backoff * (2 ** (attempt - 1))  # exponential: 2s, 4s, 8s …
                    logger.warning(
                        f"Stream error (attempt {attempt}/{max_retries}), "
                        f"retrying in {wait_secs:.1f}s: {stream_err}"
                    )

                    # Notify UI about the retry — this clears partial output
                    await on_event(AgentEvent(
                        type="stream_recovering",
                        content=f"Connection lost — retrying in {wait_secs:.0f}s...",
                        data={"attempt": attempt, "wait_seconds": wait_secs},
                    ))

                    await asyncio.sleep(wait_secs)
                    continue  # next attempt

            if not stream_succeeded:
                break  # exit the outer agent loop

            if self._cancelled:
                await on_event(AgentEvent(type="cancelled"))
                break

            # Add assistant message to history (includes thinking blocks for continuity)
            if assistant_content:
                self.history.append({"role": "assistant", "content": assistant_content})

            # Check for tool calls
            tool_uses = [b for b in assistant_content if b.get("type") == "tool_use"]

            if not tool_uses:
                # No tool calls — agent is done
                ctx_est = self._current_token_estimate()
                ctx_window = get_context_window(self.service.model_id)
                await on_event(AgentEvent(
                    type="done",
                    data={
                        "input_tokens": self._total_input_tokens,
                        "output_tokens": self._total_output_tokens,
                        "cache_read_tokens": self._cache_read_tokens,
                        "context_usage_pct": round(ctx_est / ctx_window * 100) if ctx_window else 0,
                    },
                ))
                break

            # Execute tools — parallel when possible
            tool_results = await self._execute_tools_parallel(
                tool_uses, on_event, request_approval
            )

            # Cap tool results before they enter history (prevention > cure)
            capped_results = self._cap_tool_results(tool_results)

            # Post-edit verification nudge: if any write tools were used,
            # append a system hint reminding the model to verify its changes.
            write_tools_used = {
                tu.get("name") for tu in tool_uses
                if tu.get("name") in ("edit_file", "write_file")
            }
            if write_tools_used:
                modified_files = [
                    tu.get("input", {}).get("path", "?")
                    for tu in tool_uses
                    if tu.get("name") in ("edit_file", "write_file")
                ]
                files_str = ", ".join(modified_files)
                verify_hint = {
                    "type": "text",
                    "text": (
                        f"[System] You just modified: {files_str}. "
                        "Verify your changes: re-read the modified sections to confirm edits applied correctly. "
                        "Run lint_file on each changed file to catch any syntax errors or issues. "
                        "Fix any problems before proceeding to the next step."
                    ),
                }
                capped_results.append(verify_hint)

            self.history.append({"role": "user", "content": capped_results})

        if iteration >= self.max_iterations:
            await on_event(AgentEvent(
                type="error",
                content=f"Reached maximum iterations ({self.max_iterations}). Stopping.",
            ))

    async def _execute_tools_parallel(
        self,
        tool_uses: List[Dict[str, Any]],
        on_event: Callable[[AgentEvent], Awaitable[None]],
        request_approval: Callable[[str, str, Dict], Awaitable[bool]],
    ) -> List[Dict[str, Any]]:
        """
        Execute a batch of tool calls, running safe (read-only) tools in parallel
        and dangerous (write) tools after collecting approvals.

        Returns a list of tool_result dicts ready for the conversation history.
        """
        loop = asyncio.get_event_loop()

        # Partition into safe and dangerous
        safe_calls = []
        dangerous_calls = []
        for tu in tool_uses:
            name = tu["name"]
            if name in SAFE_TOOLS:
                safe_calls.append(tu)
            else:
                dangerous_calls.append(tu)

        # Pre-allocate results keyed by tool_use id to maintain order
        results_by_id: Dict[str, Dict[str, Any]] = {}

        # ---- 1. Run all safe tools concurrently ----
        # NOTE: tool_call events are already emitted by the streaming loop
        # in _agent_loop, so we skip emitting them here to avoid duplicates.
        if safe_calls:
            # Dedup: if multiple reads target the same file (no offset), share the result
            _dedup_reads: Dict[str, asyncio.Future] = {}

            async def _run_safe(tu: Dict[str, Any]) -> tuple:
                name = tu["name"]
                inp = tu["input"]

                # File cache: return cached content for read_file if file hasn't been modified
                if name == "read_file" and not inp.get("offset") and not inp.get("limit"):
                    path = inp.get("path", "")
                    abs_p = os.path.abspath(os.path.join(self.working_directory, path))

                    # Dedup within the same batch
                    if abs_p in _dedup_reads:
                        cached_result = await _dedup_reads[abs_p]
                        return tu, cached_result

                    # Check file cache
                    if abs_p in self._file_cache:
                        cached_content, _ = self._file_cache[abs_p]
                        # Only use cache if file hasn't been modified by us
                        if abs_p not in self._file_snapshots:
                            return tu, ToolResult(success=True, output=cached_content)

                result = await loop.run_in_executor(
                    None, lambda _tu=tu: execute_tool(_tu["name"], _tu["input"], self.working_directory, backend=self.backend)
                )

                # Cache successful full-file reads
                if name == "read_file" and result.success and not inp.get("offset") and not inp.get("limit"):
                    path = inp.get("path", "")
                    abs_p = os.path.abspath(os.path.join(self.working_directory, path))
                    self._file_cache[abs_p] = (result.output, time.time())

                return tu, result

            safe_results = await asyncio.gather(*[_run_safe(tu) for tu in safe_calls])

            for tu, result in safe_results:
                result_text = result.output if result.success else (result.error or "Unknown error")
                await on_event(AgentEvent(
                    type="tool_result",
                    content=result_text,
                    data={
                        "tool_name": tu["name"],
                        "tool_use_id": tu["id"],
                        "success": result.success,
                    },
                ))
                results_by_id[tu["id"]] = {
                    "type": "tool_result",
                    "tool_use_id": tu["id"],
                    "content": result_text,
                    "is_error": not result.success,
                }

        # ---- 2. Handle dangerous tools ----
        #   File writes: auto-approved (user reviews via keep/revert at end)
        #   Commands: require explicit approval (they cannot be reverted)
        if dangerous_calls:
            file_write_calls = [
                tu for tu in dangerous_calls
                if tu["name"] in ("write_file", "edit_file")
            ]
            command_calls = [
                tu for tu in dangerous_calls
                if tu["name"] not in ("write_file", "edit_file")
            ]

            # --- Phase A: file writes (auto-approved, revertible) ---
            if file_write_calls:
                # Snapshot all files BEFORE any writes
                for tu in file_write_calls:
                    self._snapshot_file(tu["name"], tu["input"])

                # Group by absolute path so same-file edits serialize
                file_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
                for tu in file_write_calls:
                    path = tu["input"].get("path", "")
                    abs_path = os.path.abspath(
                        os.path.join(self.working_directory, path)
                    )
                    file_groups[abs_path].append(tu)

                async def _run_file_group(
                    calls: List[Dict[str, Any]],
                ) -> List[tuple]:
                    """Serial writes to one file; stop on first failure.
                    Includes auto-lint after edits and auto-retry on edit failure."""
                    results = []
                    for tu in calls:
                        result = await loop.run_in_executor(
                            None, lambda _tu=tu: execute_tool(_tu["name"], _tu["input"], self.working_directory, backend=self.backend)
                        )

                        # ── Auto-retry on edit failure ──
                        # If edit_file failed with "not found" or "multiple occurrences",
                        # re-read the file and include content in the error for immediate retry
                        if not result.success and tu["name"] == "edit_file":
                            err = result.error or ""
                            if "not found" in err.lower() or "occurrences" in err.lower():
                                path = tu["input"].get("path", "")
                                try:
                                    fresh = await loop.run_in_executor(
                                        None, lambda: execute_tool(
                                            "read_file", {"path": path},
                                            self.working_directory, backend=self.backend,
                                        )
                                    )
                                    if fresh.success:
                                        # Cap to avoid blowing context
                                        content = fresh.output
                                        if len(content) > 8000:
                                            lines = content.split("\n")
                                            content = "\n".join(lines[:150]) + f"\n... ({len(lines) - 150} lines omitted)"
                                        result = ToolResult(
                                            success=False,
                                            output="",
                                            error=(
                                                f"{err}\n\n"
                                                f"[Auto-read] Current file content:\n{content}\n\n"
                                                "Retry with the correct old_string from the content above."
                                            ),
                                        )
                                except Exception:
                                    pass  # fall through with original error

                        # ── Auto-lint after successful edit ──
                        if result.success and tu["name"] in ("edit_file", "write_file"):
                            path = tu["input"].get("path", "")
                            try:
                                lint_result = await loop.run_in_executor(
                                    None, lambda: execute_tool(
                                        "lint_file", {"path": path},
                                        self.working_directory, backend=self.backend,
                                    )
                                )
                                if lint_result.success and lint_result.output:
                                    lint_out = lint_result.output.strip()
                                    # Only append if there are actual errors (not "no issues")
                                    if lint_out and "no issues" not in lint_out.lower() and "no errors" not in lint_out.lower() and "looks good" not in lint_out.lower():
                                        result = ToolResult(
                                            success=True,
                                            output=(
                                                f"{result.output}\n\n"
                                                f"[Auto-lint] Errors detected:\n{lint_out}\n"
                                                "Fix these lint errors."
                                            ),
                                        )
                            except Exception:
                                pass  # lint failure is non-fatal

                            # Invalidate file cache after successful write
                            abs_p = os.path.abspath(os.path.join(self.working_directory, path))
                            self._file_cache.pop(abs_p, None)

                        results.append((tu, result))
                        if not result.success:
                            # Abort remaining edits to this file — they
                            # rely on content that didn't change as expected.
                            for remaining in calls[calls.index(tu) + 1:]:
                                results.append((remaining, ToolResult(
                                    success=False,
                                    output="",
                                    error="Skipped: earlier edit to same file failed.",
                                )))
                            break
                    return results

                # Different files in parallel; return_exceptions so one
                # group's failure doesn't swallow results from others.
                group_results = await asyncio.gather(
                    *[_run_file_group(g) for g in file_groups.values()],
                    return_exceptions=True,
                )

                for group in group_results:
                    if isinstance(group, BaseException):
                        logger.error(f"File group error: {group}")
                        continue
                    for tu, result in group:
                        result_text = result.output if result.success else (
                            result.error or "Unknown error"
                        )
                        await on_event(AgentEvent(
                            type="tool_result",
                            content=result_text,
                            data={
                                "tool_name": tu["name"],
                                "tool_use_id": tu["id"],
                                "success": result.success,
                            },
                        ))
                        results_by_id[tu["id"]] = {
                            "type": "tool_result",
                            "tool_use_id": tu["id"],
                            "content": result_text,
                            "is_error": not result.success,
                        }

            # --- Phase B: commands — require approval (irreversible) ---
            # In YOLO mode, auto-approve all commands
            for tu in command_calls:
                tool_name = tu["name"]
                tool_input = tu["input"]
                tool_id = tu["id"]

                # Check YOLO mode, approval memory, or ask for approval
                if app_config.auto_approve_commands:
                    await on_event(AgentEvent(
                        type="auto_approved",
                        content=tool_name,
                        data={"tool_input": tool_input, "yolo": True},
                    ))
                elif self.was_previously_approved(tool_name, tool_input):
                    await on_event(AgentEvent(
                        type="auto_approved",
                        content=tool_name,
                        data={"tool_input": tool_input},
                    ))
                else:
                    description = self._format_tool_description(tool_name, tool_input)
                    approved = await request_approval(tool_name, description, tool_input)

                    if not approved:
                        results_by_id[tool_id] = {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": "User rejected this operation.",
                            "is_error": True,
                        }
                        await on_event(AgentEvent(
                            type="tool_rejected",
                            content=tool_name,
                            data={"tool_use_id": tool_id},
                        ))
                        continue

                    self.remember_approval(tool_name, tool_input)

                # Emit command_start for run_command so the UI shows "running"
                if tool_name == "run_command":
                    await on_event(AgentEvent(
                        type="command_start",
                        content=tool_input.get("command", "?"),
                        data={"tool_use_id": tool_id},
                    ))

                cmd_start = time.time()
                result = await loop.run_in_executor(
                    None, lambda: execute_tool(tool_name, tool_input, self.working_directory, backend=self.backend)
                )
                cmd_duration = round(time.time() - cmd_start, 1)

                result_text = result.output if result.success else (
                    result.error or "Unknown error"
                )

                # Extract exit code from run_command output
                exit_code = None
                if tool_name == "run_command":
                    ec_match = re.search(r"\[exit code: (\d+)\]", result_text)
                    if ec_match:
                        exit_code = int(ec_match.group(1))
                    elif result.success:
                        exit_code = 0

                event_data: Dict[str, Any] = {
                    "tool_name": tool_name,
                    "tool_use_id": tool_id,
                    "success": result.success,
                    "duration": cmd_duration,
                }
                if exit_code is not None:
                    event_data["exit_code"] = exit_code

                await on_event(AgentEvent(
                    type="tool_result",
                    content=result_text,
                    data=event_data,
                ))
                results_by_id[tool_id] = {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": result_text,
                    "is_error": not result.success,
                }

        # Return results in the original tool_use order
        return [results_by_id[tu["id"]] for tu in tool_uses if tu["id"] in results_by_id]

    def _format_tool_description(self, name: str, inputs: Dict) -> str:
        """Format a human-readable description of a tool call for approval"""
        if name == "write_file":
            content = inputs.get("content", "")
            line_count = content.count("\n") + 1
            return f"Write {line_count} lines to {inputs.get('path', '?')}"
        elif name == "edit_file":
            return f"Edit {inputs.get('path', '?')}: replace string"
        elif name == "run_command":
            return f"Run: {inputs.get('command', '?')}"
        return f"{name}({json.dumps(inputs)[:200]})"
