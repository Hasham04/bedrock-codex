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
from dataclasses import dataclass

from bedrock_service import BedrockService, GenerationConfig, BedrockError
from tools import (
    TOOL_DEFINITIONS,
    SCOUT_TOOL_DEFINITIONS,
    SAFE_TOOLS,
    execute_tool,
    needs_approval,
    ToolResult,
    ASK_USER_QUESTION_DEFINITION,
)
from backend import Backend, LocalBackend
from config import model_config, supports_thinking, app_config, get_context_window

logger = logging.getLogger(__name__)


# ============================================================
# System Prompts
# ============================================================

SCOUT_SYSTEM_PROMPT = """You are a senior engineer doing a deep code review before implementation begins. Your reconnaissance directly determines whether the implementation succeeds or fails.

You are not skimming. You are building a mental model of this codebase — how it thinks, how it's structured, what patterns it follows.

<constraints>
- READ-ONLY tools. You cannot modify anything.
- Batch reads: request multiple files in a single turn — they run in parallel.
- Do NOT stop after listing directories. Read the actual source files.
</constraints>

<strategy>
1. Start smart: List root. Read manifest files (package.json, pyproject.toml, etc.). Know the stack.
2. Read with purpose: Every batch of file reads should answer a specific question.
3. Batch aggressively: Read 3-8 files per turn.
4. Know when to stop: You have enough when you can answer: What's the stack? What files need changing? What patterns must I follow?
</strategy>

<output_format>
### Stack
### Architecture
### Files Relevant to This Task
### Conventions & Patterns
### Build / Test / Lint
### Risks & Concerns
</output_format>

<working_directory>{working_directory}</working_directory>

Use relative paths. Be thorough."""

PLAN_SYSTEM_PROMPT = """You are a principal engineer designing an implementation plan. Your plan will be handed to an implementation agent. Every ambiguity you leave will become a bug.

You have READ-ONLY tools. Use them to understand the codebase before writing the plan.

<planning_process>
Phase 1 — UNDERSTAND: Read the source files relevant to this task. Batch reads, follow imports, read related tests.
Phase 2 — THINK: What are the constraints? What patterns does this codebase use? What could go wrong?
Phase 3 — WRITE: Produce the complete plan document. Every step must be specific enough that an implementer can execute it without asking a single clarifying question.

IMPORTANT: When you feel you've gathered sufficient context, STOP calling tools and write the plan.
If the task is ambiguous, use the ask_user_question tool to ask the user before finalizing. Incorporate their answer into your plan.
</planning_process>

<plan_document_format>
# Plan: {{concise title}}

## Why
What problem does this solve? Current state vs desired state.

## Approach
High-level design. What existing code/patterns to reuse.

## Affected Files
| File | Action | What Changes |

## Steps
Each step must name the exact file, exact function/class, and describe the specific change. Use numbered list:
1. **[EDIT]** `path/file.py` → `function_name()` — Specific change description
2. **[CREATE]** `path/new.py` — What it contains
3. **[RUN]** `exact command` — What it verifies

## Edge Cases & Risks
## Verification
Exact commands and checks.
</plan_document_format>

<working_directory>{working_directory}</working_directory>

Output a clear numbered checklist of steps — not narrative prose. Each step = one actionable item."""

BUILD_SYSTEM_PROMPT = """You are a senior engineer implementing an approved plan. Write code the way a craftsman builds furniture — every joint matters.

<execution_principles>
1. Follow the plan, think for yourself. Execute steps in order. If you see something the plan missed, adapt. State what you changed and why.
2. Read before write. Never modify a file you haven't read in this session.
3. Surgical precision: Use edit_file with enough context (3-5 lines) to match exactly one location. Use write_file only for new files or complete rewrites.
4. Verify everything: After every file modification, re-read the changed section, run lint_file, run tests if they exist.
5. When things go wrong, diagnose. Don't retry blindly.
6. Write code that belongs: Match existing conventions exactly.
7. Work through the plan as a checklist: Before each step, state "Step N of M: ...". Complete and verify one step before moving to the next.
</execution_principles>

<plan_next_move>
Before every batch of tool calls, output 1-2 sentences stating what you will do next and why. Then call the tools.
</plan_next_move>

<tool_strategy>
Search before you read. Use `search` to locate the exact function/class, then `read_file` with `offset` and `limit` to read just that section. Batch tool calls when possible.
</tool_strategy>

<tool_usage>
- read_file: Use offset + limit for specific sections of large files.
- edit_file: old_string must match EXACTLY one location. Include surrounding lines. If it fails, re-read the file.
- write_file: Overwrites entirely. Only for new files or >50% changes. Prefer edit_file.
- run_command: Check stdout and stderr. Non-zero exit = failure.
- search: Regex search. Returns matching lines with paths and line numbers.
- glob_find: Find files matching a pattern.
- lint_file: Use after every edit.
</tool_usage>

<working_directory>{working_directory}</working_directory>

Work through the plan as a checklist. State which step you're on. No preambles. Implement with precision."""

AGENT_SYSTEM_PROMPT = """You are an expert software engineer and a thoughtful problem solver. You combine deep technical skill with good judgment.

<how_you_work>
1. Understand first: Before writing any code, understand the request and the existing codebase. Read the relevant files.
2. Think, then act: Consider the approach. What's the simplest correct solution? Edge cases?
3. Write code that belongs: Your changes should match the best code already in the project.
4. Verify your work: Re-read changed code, run the linter, think "would I approve this PR?"
5. When the user gives multiple distinct tasks or a list (bullets, "and...", "then..."), first output a short numbered checklist of what you will do, then work through each item in order, stating which item you're on (e.g. "Item 1 of 3: ...").
</how_you_work>

<principles>
- Read before write. Never modify a file you haven't read in this session.
- Minimal, complete changes. Handle edge cases that matter.
- Security matters: No injection. Sanitize at boundaries.
- When things break, diagnose. Don't retry blindly.
- Batch when possible.
</principles>

<plan_next_move>
Before every batch of tool calls, output 1-2 sentences stating your next move(s) and why. Then call the tools.
</plan_next_move>

<tool_strategy>
Search before you read. Use search to find what you need, then read_file with offset/limit for that section. Batch reads when you need multiple files.
</tool_strategy>

<tool_usage>
- read_file: Use offset + limit for large files.
- edit_file: old_string must match exactly one location. Include 3-5 surrounding lines.
- write_file: Overwrites entirely. Prefer edit_file.
- run_command: Check stdout and stderr.
- search: Regex search. Use include to filter by file type.
- glob_find: Find files by pattern.
- lint_file: Use after every edit.
</tool_usage>

<working_directory>{working_directory}</working_directory>

When the user gives a multi-item request, list your todos clearly then work through them as a checklist. Match the energy of the request."""


# ============================================================
# Agent Event Types
# ============================================================

@dataclass
class AgentEvent:
    """Event emitted by the agent during execution"""
    type: str
    content: str = ""
    data: Optional[Dict[str, Any]] = None


# ============================================================
# Intent classification
# ============================================================

_CLASSIFY_SYSTEM = """You are a task classifier for a coding agent. Given a user message, decide:
1. Does the agent need to SCOUT the codebase first?
2. Does the agent need to PLAN before executing? (multi-step, multi-file, or architectural work)
3. How complex is this task? (trivial / simple / complex)

Return ONLY a JSON object: {"scout": true/false, "plan": true/false, "complexity": "trivial"|"simple"|"complex"}

- Greetings, small talk → {"scout": false, "plan": false, "complexity": "trivial"}
- Simple questions, single-file edits → {"scout": true, "plan": false, "complexity": "simple"}
- Multi-file changes, refactoring, new features → {"scout": true, "plan": true, "complexity": "complex"}
- If unsure, scout=true, plan=false. Plan only for tasks that need multi-step coordination."""

_classify_cache: Dict[str, Dict[str, Any]] = {}


def classify_intent(task: str, service=None) -> Dict[str, Any]:
    """Classify whether task needs scout/plan and complexity for model routing."""
    stripped = task.strip()
    if not stripped:
        return {"scout": False, "plan": False, "complexity": "trivial"}
    cache_key = stripped[:200].lower()
    if cache_key in _classify_cache:
        return _classify_cache[cache_key]
    if service is None:
        result = {"scout": True, "plan": False, "complexity": "simple"}
        if len(stripped.split()) <= 2:
            result = {"scout": False, "plan": False, "complexity": "trivial"}
        _classify_cache[cache_key] = result
        return result
    try:
        cfg = GenerationConfig(max_tokens=80, enable_thinking=False, throughput_mode="cross-region")
        resp = service.generate_response(
            messages=[{"role": "user", "content": stripped}],
            system_prompt=_CLASSIFY_SYSTEM,
            model_id=app_config.scout_model,
            config=cfg,
        )
        text = resp.content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        result = json.loads(text)
        complexity = result.get("complexity", "simple")
        if complexity not in ("trivial", "simple", "complex"):
            complexity = "simple"
        result = {
            "scout": bool(result.get("scout", True)),
            "plan": bool(result.get("plan", False)),
            "complexity": complexity,
        }
        logger.info(f"Intent: {result}")
    except Exception as e:
        logger.warning(f"Intent classification failed: {e}")
        result = {"scout": True, "plan": False, "complexity": "simple"}
    _classify_cache[cache_key] = result
    return result


# ============================================================
# Coding Agent
# ============================================================

class CodingAgent:
    """Core coding agent: prompt -> think -> tools -> respond. Supports scout, plan, build, and direct run."""

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
        self._current_plan: Optional[List[str]] = None
        self._scout_context: Optional[str] = None
        self._plan_step_index: int = 0
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._cache_read_tokens = 0
        self._cache_write_tokens = 0
        self._approved_commands: set = set()
        self._file_snapshots: Dict[str, Optional[str]] = {}
        self._history_len_at_last_call = 0
        self._running_summary: str = ""
        self._file_cache: Dict[str, tuple] = {}
        self._step_checkpoints: Dict[int, Dict[str, Optional[str]]] = {}

    @property
    def total_tokens(self) -> int:
        return self._total_input_tokens + self._total_output_tokens

    @property
    def modified_files(self) -> Dict[str, Optional[str]]:
        return dict(self._file_snapshots)

    def cancel(self):
        self._cancelled = True
        if self.backend:
            try:
                self.backend.cancel_running_command()
            except Exception:
                pass

    def reset(self):
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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "history": self.history,
            "token_usage": {
                "input": self._total_input_tokens,
                "output": self._total_output_tokens,
                "cache_read": self._cache_read_tokens,
                "cache_write": self._cache_write_tokens,
            },
            "approved_commands": list(self._approved_commands),
            "running_summary": self._running_summary,
            "current_plan": self._current_plan,
            "scout_context": self._scout_context,
            "file_snapshots": self._file_snapshots,
            "plan_step_index": self._plan_step_index,
        }

    def from_dict(self, data: Dict[str, Any]):
        self.history = data.get("history", [])
        tu = data.get("token_usage", {})
        if isinstance(tu, dict):
            self._total_input_tokens = tu.get("input", 0)
            self._total_output_tokens = tu.get("output", 0)
            self._cache_read_tokens = tu.get("cache_read", 0)
            self._cache_write_tokens = tu.get("cache_write", 0)
        self._approved_commands = set(data.get("approved_commands", []))
        self._running_summary = data.get("running_summary", "")
        self._current_plan = data.get("current_plan")
        self._scout_context = data.get("scout_context")
        self._file_snapshots = data.get("file_snapshots", {})
        self._plan_step_index = data.get("plan_step_index", 0)

    def clear_snapshots(self):
        self._file_snapshots = {}

    def revert_all(self) -> List[str]:
        reverted = []
        for abs_path, content in self._file_snapshots.items():
            try:
                if content is not None:
                    self.backend.write_file(abs_path, content)
                    reverted.append(abs_path)
                else:
                    self.backend.remove_file(abs_path)
                    reverted.append(abs_path)
            except Exception as e:
                logger.warning(f"Revert failed for {abs_path}: {e}")
        self._file_snapshots = {}
        return reverted

    def revert_to_step(self, step_num: int) -> List[str]:
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
                logger.warning(f"Revert to step failed for {abs_path}: {e}")
        for s in list(self._step_checkpoints.keys()):
            if s > step_num:
                del self._step_checkpoints[s]
        self._plan_step_index = step_num
        return reverted

    def was_previously_approved(self, tool_name: str, tool_input: Dict) -> bool:
        key = (tool_name, json.dumps(tool_input, sort_keys=True))
        return key in self._approved_commands

    def remember_approval(self, tool_name: str, tool_input: Dict):
        self._approved_commands.add((tool_name, json.dumps(tool_input, sort_keys=True)))

    # ------------------------------------------------------------------
    # Project rules and docs (Cursor-style)
    # ------------------------------------------------------------------

    _PROJECT_RULES_MAX_CHARS = 8000
    _PROJECT_DOCS_MAX_CHARS = 6000

    def _load_project_rules(self) -> str:
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
                chunk = f"--- {label} ---\n{content}"
                take = min(len(chunk), self._PROJECT_RULES_MAX_CHARS - total)
                if take > 0:
                    parts.append(chunk[:take])
                    total += take
            except Exception as e:
                logger.debug(f"Could not load rule {path}: {e}")
        _add(".cursorrules", "cursorrules")
        _add("RULE.md", "RULE.md")
        _add(".cursor/RULE.md", ".cursor/RULE.md")
        try:
            if self.backend.file_exists(".cursor/rules") and self.backend.is_dir(".cursor/rules"):
                for ent in sorted(self.backend.list_dir(".cursor/rules"), key=lambda e: e.get("name", "")):
                    if ent.get("type") != "file":
                        continue
                    name = ent.get("name", "")
                    if name.endswith((".mdc", ".md")):
                        _add(f".cursor/rules/{name}", f".cursor/rules/{name}")
        except Exception:
            pass
        return "\n\n".join(parts) if parts else ""

    def _load_project_docs(self) -> str:
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
            except Exception:
                pass
        for name in ["overview.md", "tech-specs.md", "requirements.md", "index.md", "README.md"]:
            _add(f"project-docs/{name}", f"project-docs/{name}")
        _add("README.md", "README.md")
        _add("CONTRIBUTING.md", "CONTRIBUTING.md")
        return "\n\n".join(parts) if parts else ""

    def _effective_system_prompt(self, base: str) -> str:
        rules = self._load_project_rules()
        if not rules:
            return base
        return base + "\n\n<project_rules>\nThese project-specific rules MUST be followed:\n\n" + rules + "\n</project_rules>"

    def _current_token_estimate(self) -> int:
        if self._total_input_tokens > 0:
            return self._total_input_tokens
        total = 0
        for msg in self.history:
            c = msg.get("content", "")
            if isinstance(c, str):
                total += len(c) // 4
            elif isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and "text" in b:
                        total += len(b["text"]) // 4
                    elif isinstance(b, dict) and "input" in b:
                        total += len(json.dumps(b.get("input", {}))) // 4
        return total

    def _snapshot_file(self, tool_name: str, tool_input: Dict[str, Any]) -> None:
        if tool_name not in ("write_file", "edit_file"):
            return
        rel_path = tool_input.get("path", "")
        abs_path = self.backend.resolve_path(rel_path)
        if abs_path in self._file_snapshots:
            return
        try:
            if self.backend.file_exists(rel_path) or self.backend.file_exists(abs_path):
                self._file_snapshots[abs_path] = self.backend.read_file(rel_path if self.backend.file_exists(rel_path) else abs_path)
            else:
                self._file_snapshots[abs_path] = None
        except Exception:
            self._file_snapshots[abs_path] = None

    def _format_tool_description(self, name: str, inputs: Dict) -> str:
        if name == "write_file":
            return f"Write {len(inputs.get('content', '').splitlines())} lines to {inputs.get('path', '?')}"
        if name == "edit_file":
            return f"Edit {inputs.get('path', '?')}: replace string"
        if name == "run_command":
            return f"Run: {inputs.get('command', '?')}"
        return f"{name}({json.dumps(inputs)[:200]})"

    _STEP_RE = re.compile(r"(?:step|item)\s*#?\s*(\d+)|(?:^|\s)(\d+)\.\s+", re.IGNORECASE)

    def _detect_plan_step(self, text: str) -> Optional[int]:
        if not self._current_plan:
            return None
        matches = self._STEP_RE.findall(text[:500])
        nums = []
        for m in matches:
            if isinstance(m, tuple):
                for g in m:
                    if g and g.isdigit():
                        nums.append(int(g))
                        break
            elif isinstance(m, str) and m.isdigit():
                nums.append(int(m))
        for num in reversed(nums):
            try:
                if 1 <= num <= len(self._current_plan):
                    old = self._plan_step_index
                    self._plan_step_index = num
                    if num != old:
                        if old > 0:
                            checkpoint = {}
                            for abs_path in self._file_snapshots:
                                try:
                                    checkpoint[abs_path] = self.backend.read_file(abs_path)
                                except Exception:
                                    checkpoint[abs_path] = None
                            self._step_checkpoints[old] = checkpoint
                        return num
            except (ValueError, IndexError, TypeError):
                pass
        return None

    # ------------------------------------------------------------------
    # Scout
    # ------------------------------------------------------------------

    async def _run_scout(
        self,
        task: str,
        on_event: Callable[[AgentEvent], Awaitable[None]],
    ) -> Optional[str]:
        if not app_config.scout_enabled:
            return None
        await on_event(AgentEvent(type="scout_start", content="Scouting codebase..."))
        scout_system = SCOUT_SYSTEM_PROMPT.format(working_directory=self.working_directory)
        project_docs = self._load_project_docs()
        scout_user = f"Explore this codebase and gather context for the following task:\n\n{task}"
        if project_docs:
            scout_user = f"<project_context>\n{project_docs}\n</project_context>\n\n" + scout_user
        scout_messages: List[Dict[str, Any]] = [{"role": "user", "content": scout_user}]
        loop = asyncio.get_event_loop()
        cfg = GenerationConfig(max_tokens=8192, enable_thinking=False, throughput_mode="cross-region")
        try:
            for _ in range(app_config.scout_max_iterations):
                if self._cancelled:
                    return None
                result = await loop.run_in_executor(
                    None,
                    lambda: self.service.generate_response(
                        messages=scout_messages,
                        system_prompt=self._effective_system_prompt(scout_system),
                        model_id=app_config.scout_model,
                        config=cfg,
                        tools=SCOUT_TOOL_DEFINITIONS,
                    ),
                )
                self._total_input_tokens += result.input_tokens
                self._total_output_tokens += result.output_tokens
                assistant_content = []
                if result.content:
                    assistant_content.append({"type": "text", "text": result.content})
                for tu in result.tool_uses:
                    assistant_content.append({"type": "tool_use", "id": tu.id, "name": tu.name, "input": tu.input})
                scout_messages.append({"role": "assistant", "content": assistant_content})
                if not result.tool_uses:
                    for msg in reversed(scout_messages):
                        if msg.get("role") == "assistant":
                            c = msg.get("content", [])
                            if isinstance(c, list):
                                for b in c:
                                    if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                                        await on_event(AgentEvent(type="scout_end", content="Scout complete"))
                                        return b["text"].strip()
                            elif isinstance(c, str):
                                await on_event(AgentEvent(type="scout_end", content="Scout complete"))
                                return c.strip()
                    return None
                tool_results = []
                for tu in result.tool_uses:
                    tr = await loop.run_in_executor(
                        None,
                        lambda tu=tu: execute_tool(tu.name, tu.input, self.working_directory, backend=self.backend),
                    )
                    text = tr.output if tr.success else (tr.error or "Unknown error")
                    if len(text) > 8000:
                        text = "\n".join(text.split("\n")[:60]) + "\n... (omitted)"
                    tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": text, "is_error": not tr.success})
                    await on_event(AgentEvent(type="scout_progress", content=f"{tu.name}: {tu.input.get('path', '?')}"))
                scout_messages.append({"role": "user", "content": tool_results})
        except Exception as e:
            logger.warning(f"Scout failed: {e}")
            await on_event(AgentEvent(type="scout_end", content=str(e)))
        return None

    # ------------------------------------------------------------------
    # Plan phase
    # ------------------------------------------------------------------

    _PLAN_RE = re.compile(r"<plan>\s*(.*?)\s*</plan>", re.DOTALL)

    def _parse_plan_steps(self, plan_text: str) -> List[str]:
        steps = []
        for line in plan_text.split("\n"):
            line = line.strip()
            m = re.match(r"^(?:\d+\.|\*\*?\d+\*?\*?|\[EDIT\]|\[CREATE\]|\[RUN\])\s*(.+)", line, re.IGNORECASE)
            if m and len(m.group(1).strip()) > 5:
                steps.append(line)
            elif re.match(r"^\d+\.", line) and len(line) > 10:
                steps.append(line)
        return steps[:50] if steps else [plan_text[:2000]]

    async def run_plan(
        self,
        task: str,
        on_event: Callable[[AgentEvent], Awaitable[None]],
        request_question_answer: Optional[Callable[[str, Optional[str], str], Awaitable[str]]] = None,
    ) -> Optional[List[str]]:
        self._cancelled = False
        self._current_plan = None
        scout_context = None
        if app_config.scout_enabled and len(self.history) == 0:
            scout_context = await self._run_scout(task, on_event)
            self._scout_context = scout_context
        await on_event(AgentEvent(type="phase_start", content="plan"))
        task_for_plan = task
        if app_config.task_refinement_enabled:
            try:
                refine_cfg = GenerationConfig(max_tokens=500, enable_thinking=False, throughput_mode="cross-region")
                refine_sys = "Refine the user task into: ## Output specification (1-2 sentences), ## Constraints (2-3 bullets), ## Task (original or clarified). Keep under 300 words."
                refined = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self.service.generate_response(
                        messages=[{"role": "user", "content": task}],
                        system_prompt=refine_sys,
                        model_id=app_config.scout_model,
                        config=refine_cfg,
                    ),
                )
                if refined.content and refined.content.strip():
                    task_for_plan = refined.content.strip()
            except Exception:
                pass
        plan_system = PLAN_SYSTEM_PROMPT.format(working_directory=self.working_directory)
        plan_user = task_for_plan
        if scout_context:
            plan_user = f"<codebase_context>\n{scout_context}\n</codebase_context>\n\n{plan_user}"
        if self._load_project_docs():
            plan_user = f"<project_context>\n{self._load_project_docs()}\n</project_context>\n\n" + plan_user
        plan_messages: List[Dict[str, Any]] = [{"role": "user", "content": plan_user}]
        plan_config = GenerationConfig(
            max_tokens=model_config.max_tokens,
            enable_thinking=model_config.enable_thinking and supports_thinking(self.service.model_id),
            thinking_budget=model_config.thinking_budget if supports_thinking(self.service.model_id) else 0,
            throughput_mode=model_config.throughput_mode,
        )
        plan_tools = (SCOUT_TOOL_DEFINITIONS + [ASK_USER_QUESTION_DEFINITION]) if request_question_answer else SCOUT_TOOL_DEFINITIONS
        max_plan_iters = 25
        plan_text = ""
        loop = asyncio.get_event_loop()

        for plan_iter in range(max_plan_iters):
            if self._cancelled:
                return None
            await on_event(AgentEvent(type="scout_progress", content=f"Planning: iteration {plan_iter + 1}..."))
            iter_tools = plan_tools if plan_iter < max_plan_iters - 1 else None
            cq: queue.Queue = queue.Queue()

            def producer():
                try:
                    for ch in self.service.generate_response_stream(
                        messages=plan_messages,
                        system_prompt=self._effective_system_prompt(plan_system),
                        model_id=None,
                        config=plan_config,
                        tools=iter_tools,
                    ):
                        cq.put(ch)
                    cq.put(None)
                except Exception as ex:
                    cq.put(ex)

            t = threading.Thread(target=producer, daemon=True)
            t.start()
            a_content = []
            c_text = ""
            t_uses = []
            c_tool = None
            t_json = []
            while True:
                chunk = await loop.run_in_executor(None, cq.get)
                if chunk is None:
                    break
                if isinstance(chunk, Exception):
                    raise chunk
                ct = chunk.get("type", "")
                if ct == "thinking_start":
                    pass
                elif ct == "thinking":
                    pass
                elif ct == "thinking_end":
                    a_content.append({"type": "thinking", "thinking": ""})
                elif ct == "text_start":
                    c_text = ""
                elif ct == "text":
                    c_text += chunk.get("content", "")
                elif ct == "text_end":
                    if c_text:
                        a_content.append({"type": "text", "text": c_text})
                elif ct == "tool_use_start":
                    c_tool = chunk.get("data", {})
                    t_json = []
                elif ct == "tool_use_delta":
                    t_json.append(chunk.get("content", ""))
                elif ct == "tool_use_end":
                    if c_tool:
                        try:
                            inp = json.loads("".join(t_json))
                        except json.JSONDecodeError:
                            inp = {}
                        tb = {"type": "tool_use", "id": c_tool.get("id", ""), "name": c_tool.get("name", ""), "input": inp}
                        a_content.append(tb)
                        t_uses.append(tb)
                        await on_event(AgentEvent(type="tool_call", content=c_tool.get("name", ""), data={"id": c_tool.get("id"), "name": c_tool.get("name"), "input": inp}))
                elif ct == "usage_start":
                    self._total_input_tokens += chunk.get("usage", {}).get("input_tokens", 0)
                    self._cache_read_tokens += chunk.get("usage", {}).get("cache_read_input_tokens", 0)
                elif ct == "message_end":
                    self._total_output_tokens += chunk.get("usage", {}).get("output_tokens", 0)
            t.join(timeout=5)
            plan_messages.append({"role": "assistant", "content": a_content})
            if not t_uses:
                plan_text = c_text.strip()
                break
            tool_results = []
            for tu in t_uses:
                if tu.get("name") == "ask_user_question" and request_question_answer:
                    q = tu.get("input", {}).get("question", "")
                    ctx = tu.get("input", {}).get("context") or ""
                    try:
                        ans = await request_question_answer(q, ctx, tu["id"])
                        tool_results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": f"User answered: {ans}", "is_error": False})
                    except Exception as e:
                        tool_results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": str(e), "is_error": True})
                else:
                    tr = await loop.run_in_executor(None, lambda tu=tu: execute_tool(tu["name"], tu["input"], self.working_directory, backend=self.backend))
                    text = tr.output if tr.success else (tr.error or "Unknown error")
                    if len(text) > 10000:
                        text = "\n".join(text.split("\n")[:80]) + "\n... (omitted)"
                    tool_results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": text, "is_error": not tr.success})
            plan_messages.append({"role": "user", "content": tool_results})
        if not plan_text and plan_messages:
            plan_messages.append({"role": "user", "content": "Produce the COMPLETE implementation plan now. Use the plan document format. Numbered steps only."})
            cq2 = queue.Queue()
            def prod2():
                try:
                    for ch in self.service.generate_response_stream(messages=plan_messages, system_prompt=self._effective_system_prompt(plan_system), model_id=None, config=plan_config, tools=None):
                        cq2.put(ch)
                    cq2.put(None)
                except Exception as ex:
                    cq2.put(ex)
            t2 = threading.Thread(target=prod2, daemon=True)
            t2.start()
            while True:
                ch = await loop.run_in_executor(None, cq2.get)
                if ch is None:
                    break
                if ch.get("type") == "text":
                    plan_text += ch.get("content", "")
                elif ch.get("type") == "text_end":
                    break
            t2.join(timeout=5)
        if not plan_text:
            await on_event(AgentEvent(type="error", content="Planning produced no output."))
            return None
        steps = self._parse_plan_steps(plan_text)
        self._current_plan = steps
        await on_event(AgentEvent(type="phase_end", content="plan"))
        return steps

    # ------------------------------------------------------------------
    # Build phase
    # ------------------------------------------------------------------

    async def run_build(
        self,
        task: str,
        plan_steps: List[str],
        on_event: Callable[[AgentEvent], Awaitable[None]],
        request_approval: Callable[[str, str, Dict], Awaitable[bool]],
        config: Optional[GenerationConfig] = None,
    ):
        self._cancelled = False
        self._file_snapshots = {}
        await on_event(AgentEvent(type="phase_start", content="build"))
        saved_prompt = self.system_prompt
        self.system_prompt = BUILD_SYSTEM_PROMPT.format(working_directory=self.working_directory)
        plan_block = "\n".join(plan_steps)
        parts = []
        if self._scout_context:
            parts.append(f"<codebase_context>\n{self._scout_context}\n</codebase_context>")
        parts.append(f"<approved_plan>\n{plan_block}\n</approved_plan>")
        parts.append(task)
        parts.append("Execute this plan step by step. State which step you are working on (e.g. Step 1 of N). Read before edit. Verify each step before moving on.")
        self.history.append({"role": "user", "content": "\n\n".join(parts)})
        await self._agent_loop(on_event, request_approval, config)
        self.system_prompt = saved_prompt
        await on_event(AgentEvent(type="phase_end", content="build"))

    # ------------------------------------------------------------------
    # Direct run
    # ------------------------------------------------------------------

    async def run(
        self,
        task: str,
        on_event: Callable[[AgentEvent], Awaitable[None]],
        request_approval: Callable[[str, str, Dict], Awaitable[bool]],
        config: Optional[GenerationConfig] = None,
        enable_scout: bool = True,
    ):
        self._cancelled = False
        self._file_snapshots = {}
        scout_context = None
        if enable_scout and app_config.scout_enabled and len(self.history) == 0:
            scout_context = await self._run_scout(task, on_event)
        project_docs = self._load_project_docs() if len(self.history) == 0 else ""
        if scout_context:
            user_content = f"<codebase_context>\n{scout_context}\n</codebase_context>\n\n{task}"
        else:
            user_content = task
        if project_docs:
            user_content = f"<project_context>\n{project_docs}\n</project_context>\n\n" + user_content
        self.history.append({"role": "user", "content": user_content})
        await self._agent_loop(on_event, request_approval, config)

    # ------------------------------------------------------------------
    # Agent loop (streaming + tools)
    # ------------------------------------------------------------------

    async def _agent_loop(
        self,
        on_event: Callable[[AgentEvent], Awaitable[None]],
        request_approval: Callable[[str, str, Dict], Awaitable[bool]],
        config: Optional[GenerationConfig] = None,
    ):
        loop = asyncio.get_event_loop()
        gen_config = config or GenerationConfig(
            max_tokens=model_config.max_tokens,
            enable_thinking=model_config.enable_thinking and supports_thinking(self.service.model_id),
            thinking_budget=model_config.thinking_budget if supports_thinking(self.service.model_id) else 0,
            throughput_mode=model_config.throughput_mode,
        )
        for _ in range(self.max_iterations):
            if self._cancelled:
                return
            cq = queue.Queue()

            def producer():
                try:
                    for ch in self.service.generate_response_stream(
                        messages=self.history,
                        system_prompt=self._effective_system_prompt(self.system_prompt),
                        model_id=None,
                        config=gen_config,
                        tools=TOOL_DEFINITIONS,
                    ):
                        cq.put(ch)
                    cq.put(None)
                except Exception as ex:
                    cq.put(ex)

            t = threading.Thread(target=producer, daemon=True)
            t.start()
            a_content = []
            c_text = ""
            c_thinking = ""
            t_uses = []
            c_tool = None
            t_json = []
            while True:
                if self._cancelled:
                    break
                chunk = await loop.run_in_executor(None, cq.get)
                if chunk is None:
                    break
                if isinstance(chunk, Exception):
                    raise chunk
                ct = chunk.get("type", "")
                if ct == "thinking_start":
                    c_thinking = ""
                    await on_event(AgentEvent(type="thinking_start"))
                elif ct == "thinking":
                    c_thinking += chunk.get("content", "")
                    await on_event(AgentEvent(type="thinking", content=chunk.get("content", "")))
                elif ct == "thinking_end":
                    a_content.append({"type": "thinking", "thinking": c_thinking})
                    await on_event(AgentEvent(type="thinking_end"))
                elif ct == "text_start":
                    c_text = ""
                    await on_event(AgentEvent(type="text_start"))
                elif ct == "text":
                    c_text += chunk.get("content", "")
                    await on_event(AgentEvent(type="text", content=chunk.get("content", "")))
                elif ct == "text_end":
                    if c_text:
                        a_content.append({"type": "text", "text": c_text})
                    await on_event(AgentEvent(type="text_end"))
                    new_step = self._detect_plan_step(c_text)
                    if new_step is not None:
                        await on_event(AgentEvent(type="plan_step_progress", content=str(new_step), data={"step": new_step, "total": len(self._current_plan) or 0}))
                elif ct == "tool_use_start":
                    c_tool = chunk.get("data", {})
                    t_json = []
                elif ct == "tool_use_delta":
                    t_json.append(chunk.get("content", ""))
                elif ct == "tool_use_end":
                    if c_tool:
                        try:
                            inp = json.loads("".join(t_json))
                        except json.JSONDecodeError:
                            inp = {}
                        tb = {"type": "tool_use", "id": c_tool.get("id", ""), "name": c_tool.get("name", ""), "input": inp}
                        a_content.append(tb)
                        t_uses.append(tb)
                        await on_event(AgentEvent(type="tool_call", content=c_tool.get("name", ""), data={"id": c_tool.get("id"), "name": c_tool.get("name"), "input": inp}))
                elif ct == "usage_start":
                    self._total_input_tokens += chunk.get("usage", {}).get("input_tokens", 0)
                    self._cache_read_tokens += chunk.get("usage", {}).get("cache_read_input_tokens", 0)
                elif ct == "message_end":
                    self._total_output_tokens += chunk.get("usage", {}).get("output_tokens", 0)
            t.join(timeout=5)
            self.history.append({"role": "assistant", "content": a_content})
            if not t_uses:
                ctx_est = self._current_token_estimate()
                ctx_window = get_context_window(self.service.model_id)
                await on_event(AgentEvent(type="done", data={
                    "input_tokens": self._total_input_tokens,
                    "output_tokens": self._total_output_tokens,
                    "cache_read_tokens": self._cache_read_tokens,
                    "context_usage_pct": round(ctx_est / ctx_window * 100) if ctx_window else 0,
                }))
                return
            tool_results = await self._execute_tools_parallel(t_uses, on_event, request_approval)
            self.history.append({"role": "user", "content": tool_results})

    async def _execute_tools_parallel(
        self,
        tool_uses: List[Dict[str, Any]],
        on_event: Callable[[AgentEvent], Awaitable[None]],
        request_approval: Callable[[str, str, Dict], Awaitable[bool]],
    ) -> List[Dict[str, Any]]:
        loop = asyncio.get_event_loop()
        results_by_id: Dict[str, Dict[str, Any]] = {}
        safe_calls = [tu for tu in tool_uses if tu["name"] in SAFE_TOOLS]
        dangerous_calls = [tu for tu in tool_uses if tu["name"] not in SAFE_TOOLS]
        if safe_calls:
            async def run_safe(tu):
                if tu["name"] == "read_file" and not tu.get("input", {}).get("offset") and not tu.get("input", {}).get("limit"):
                    path = tu["input"].get("path", "")
                    abs_p = os.path.abspath(os.path.join(self.working_directory, path))
                    if abs_p in self._file_cache and abs_p not in self._file_snapshots:
                        return tu, ToolResult(success=True, output=self._file_cache[abs_p][0])
                r = await loop.run_in_executor(None, lambda tu=tu: execute_tool(tu["name"], tu["input"], self.working_directory, backend=self.backend))
                if tu["name"] == "read_file" and r.success and not tu.get("input", {}).get("offset") and not tu.get("input", {}).get("limit"):
                    abs_p = os.path.abspath(os.path.join(self.working_directory, tu["input"].get("path", "")))
                    self._file_cache[abs_p] = (r.output, time.time())
                return tu, r
            for tu, result in await asyncio.gather(*[run_safe(tu) for tu in safe_calls]):
                text = result.output if result.success else (result.error or "Unknown error")
                await on_event(AgentEvent(type="tool_result", content=text, data={"tool_name": tu["name"], "tool_use_id": tu["id"], "success": result.success}))
                results_by_id[tu["id"]] = {"type": "tool_result", "tool_use_id": tu["id"], "content": text, "is_error": not result.success}
        if dangerous_calls:
            file_calls = [tu for tu in dangerous_calls if tu["name"] in ("write_file", "edit_file")]
            command_calls = [tu for tu in dangerous_calls if tu["name"] not in ("write_file", "edit_file")]
            for tu in file_calls:
                self._snapshot_file(tu["name"], tu["input"])
            file_groups = defaultdict(list)
            for tu in file_calls:
                abs_p = os.path.abspath(os.path.join(self.working_directory, tu["input"].get("path", "")))
                file_groups[abs_p].append(tu)
            for group in file_groups.values():
                for tu in group:
                    result = await loop.run_in_executor(None, lambda tu=tu: execute_tool(tu["name"], tu["input"], self.working_directory, backend=self.backend))
                    if result.success:
                        abs_p = os.path.abspath(os.path.join(self.working_directory, tu["input"].get("path", "")))
                        self._file_cache.pop(abs_p, None)
                    text = result.output if result.success else (result.error or "Unknown error")
                    await on_event(AgentEvent(type="tool_result", content=text, data={"tool_name": tu["name"], "tool_use_id": tu["id"], "success": result.success}))
                    results_by_id[tu["id"]] = {"type": "tool_result", "tool_use_id": tu["id"], "content": text, "is_error": not result.success}
                    if not result.success:
                        break
            for tu in command_calls:
                if app_config.auto_approve_commands or self.was_previously_approved(tu["name"], tu["input"]):
                    await on_event(AgentEvent(type="auto_approved", content=tu["name"], data={"tool_input": tu["input"]}))
                else:
                    approved = await request_approval(tu["name"], self._format_tool_description(tu["name"], tu["input"]), tu["input"])
                    if not approved:
                        results_by_id[tu["id"]] = {"type": "tool_result", "tool_use_id": tu["id"], "content": "User rejected this operation.", "is_error": True}
                        await on_event(AgentEvent(type="tool_rejected", content=tu["name"], data={"tool_use_id": tu["id"]}))
                        continue
                    self.remember_approval(tu["name"], tu["input"])
                if tu["name"] == "run_command":
                    await on_event(AgentEvent(type="command_start", content=tu["input"].get("command", "?"), data={"tool_use_id": tu["id"]}))
                result = await loop.run_in_executor(None, lambda tu=tu: execute_tool(tu["name"], tu["input"], self.working_directory, backend=self.backend))
                text = result.output if result.success else (result.error or "Unknown error")
                await on_event(AgentEvent(type="tool_result", content=text, data={"tool_name": tu["name"], "tool_use_id": tu["id"], "success": result.success}))
                results_by_id[tu["id"]] = {"type": "tool_result", "tool_use_id": tu["id"], "content": text, "is_error": not result.success}
        return [results_by_id[tu["id"]] for tu in tool_uses if tu["id"] in results_by_id]
