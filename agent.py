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
import shlex
import threading
import time
import hashlib
from collections import defaultdict
from typing import List, Dict, Any, Optional, Callable, Awaitable
from dataclasses import dataclass, field

from bedrock_service import BedrockService, GenerationConfig, BedrockError
from tools import TOOL_DEFINITIONS, SCOUT_TOOL_DEFINITIONS, SAFE_TOOLS, execute_tool, needs_approval, ToolResult, ASK_USER_QUESTION_DEFINITION

# Tool names for system prompt so the agent always knows what it can call
AVAILABLE_TOOL_NAMES = ", ".join(t["name"] for t in TOOL_DEFINITIONS)
SCOUT_TOOL_NAMES = ", ".join(t["name"] for t in SCOUT_TOOL_DEFINITIONS)

# Display names for scout-phase progress
SCOUT_TOOL_DISPLAY_NAMES = {
    "list_directory": "Directory",
    "project_tree": "Project tree",
    "search": "Search",
    "find_symbol": "Symbols",
    "semantic_retrieve": "Code search",
    "WebFetch": "Fetch",
    "WebSearch": "Search web",
    "lint_file": "Lint",
    "TodoWrite": "Planning",
    "TodoRead": "Read todos",
}

from config import (
    model_config, supports_thinking, app_config, get_context_window,
    get_max_output_tokens, get_default_max_tokens, supports_adaptive_thinking,
    get_thinking_max_budget,
)

logger = logging.getLogger(__name__)

# ============================================================
# Modular Prompt Architecture
# ============================================================
# Each module is a focused, independently testable prompt fragment.
# The _compose_system_prompt() function assembles them based on
# current phase, detected language, and available context.
# ============================================================

# --- Core Modules (always included) ---

_MOD_IDENTITY = """You are an expert software engineer operating inside a coding IDE connected to a real codebase on the user's machine (or a remote server via SSH). You have direct access to files, a terminal, and the project's full structure.

You approach every task with the rigor of someone whose code ships to production and whose mistakes affect real users. You have deep expertise across languages, frameworks, and systems. You are methodical: you investigate before acting, you verify after changing, and you never guess when you can check.

The user trusts you to make changes to their codebase. That trust requires you to: read before editing, lint after editing, explain non-obvious decisions, and ask when genuinely uncertain rather than guessing. You are not an assistant that suggests — you are an engineer that executes."""

_MOD_DOING_TASKS = """<doing_tasks>
- NEVER propose changes to code you haven't read. Read files first, understand existing code, then modify.
- Be careful not to introduce security vulnerabilities (command injection, XSS, SQL injection, path traversal). If you notice insecure code you wrote, fix it immediately.
- Avoid over-engineering. Only make changes that are directly requested or clearly necessary. Keep solutions simple and focused.
  - Don't add features, refactor code, or make "improvements" beyond what was asked. A bug fix doesn't need surrounding code cleaned up. A simple feature doesn't need extra configurability.
  - Don't add docstrings, comments, or type annotations to code you didn't change. Only add comments where the logic isn't self-evident.
  - Don't add error handling, fallbacks, or validation for scenarios that can't happen. Trust internal code and framework guarantees. Only validate at system boundaries (user input, external APIs).
  - Don't create helpers, utilities, or abstractions for one-time operations. Don't design for hypothetical future requirements. Three similar lines of code is better than a premature abstraction.
- Avoid backwards-compatibility hacks like renaming unused `_vars`, re-exporting types, adding `// removed` comments. If something is unused, delete it completely.
- After every edit: re-read the changed section and run lint_file to verify correctness.
</doing_tasks>"""

_MOD_CAREFUL_EXECUTION = """<careful_execution>
Consider the reversibility and blast radius of actions. Local file edits are safe and reversible. But for actions that are hard to reverse, affect shared systems, or could be destructive, confirm with the user first.

Destructive or risky actions that need confirmation:
- Deleting files or branches, force-pushing, git reset --hard, overwriting uncommitted changes
- Pushing code, creating/closing PRs or issues, modifying shared infrastructure
- Running commands that could have side effects beyond the local workspace

Don't use destructive shortcuts (--no-verify, rm -rf) to bypass obstacles. If you encounter unexpected state like unfamiliar files, branches, or config, investigate before overwriting -- it may be the user's in-progress work. Resolve merge conflicts rather than discarding changes. Match the scope of your actions to what was actually requested.
</careful_execution>"""

_MOD_TONE_AND_STYLE = """<tone_and_style>
- Be concise and direct. No emoji unless the user requests it.
- Prioritize technical accuracy over validating the user's beliefs. If the approach is wrong, say so respectfully. Objective guidance is more valuable than false agreement.
- Never give time estimates or predictions for how long tasks will take. Focus on what needs to be done.
- Never refer to tool names when speaking to the user. Say "I'll read the file" not "I'll use the Read tool."
- When uncertain, investigate rather than guessing.
- Don't apologize repeatedly. If something unexpected happens, explain and proceed.
</tone_and_style>"""

_MOD_TOOL_POLICY = """<tool_policy>
PARALLELIZATION:
- Call multiple tools in a single response when they are independent. They execute in parallel.
- Example: reading 8 files = 1 round-trip, not 8. Batch aggressively (5-12 file reads per turn).
- Example: after editing 3 files, call lint_file on all 3 in one response.
- Example: search + find_symbol + project_tree can all run in the same turn.
- When editing multiple independent files, make all Edit calls in one response.

TOOL SELECTION (use specialized tools, not Bash):
- Read files: Read (NEVER cat, head, tail)
- Edit files: Edit (NEVER sed, awk, perl -i)
- Create files: Write (NEVER echo, heredoc, tee)
- Search code: search (NEVER grep, rg via terminal)
- Find files: Glob (NEVER find via terminal)
- Reserve Bash for: running tests, installing packages, git operations, builds, and system commands.

DISCOVERY STRATEGY:
- New codebase? project_tree first for structure, then Read manifest files (package.json, pyproject.toml, pom.xml).
- "Where/how" questions? semantic_retrieve first, then targeted Read with offset/limit on results.
- Exact string or regex? search. Specific identifier? find_symbol.

FILE OPERATIONS:
- ALWAYS read a file before editing it. No exceptions.
- For large files (>500 lines): use offset/limit for targeted reads. Don't read the whole thing.
- After every edit: re-read the changed section and run lint_file.
- Prefer Edit over Write for modifications. Write only for new files or >50% rewrites.

INDEPENDENCE:
- Find answers yourself before asking the user. Use tools to investigate.
- When uncertain about implementation details, check the existing code for patterns.
</tool_policy>"""

_MOD_GIT_WORKFLOW = """<git_workflow>
SAFETY RULES (never violate):
- NEVER update git config (user.name, user.email, etc.)
- NEVER force push (--force, -f) to main or master
- NEVER skip hooks (--no-verify, --no-gpg-sign) unless the user explicitly asks
- NEVER run destructive git commands (reset --hard, clean -fd, checkout -- .) without user approval
- NEVER commit files that contain secrets (.env, credentials.json, tokens, private keys)

COMMITS:
- Only create commits when the user explicitly asks. If unclear, ask first.
- Before committing: run git status and git diff to review all changes.
- Write concise commit messages (1-2 sentences) focused on WHY, not WHAT.
- Always pass commit messages via heredoc for correct formatting:
  git commit -m "$(cat <<'EOF'
  Your commit message here.
  EOF
  )"
- If a pre-commit hook modifies files, stage the modifications and amend (only if the commit was yours, in this session, and not yet pushed).
- If a commit is REJECTED by a hook, NEVER amend — fix the issue and create a NEW commit.

AMEND RULES (all conditions must be true):
- The HEAD commit was created by you in this conversation
- The commit has NOT been pushed to remote (check: git status shows "Your branch is ahead")
- The user explicitly requested amend, OR a pre-commit hook auto-modified files

PULL REQUESTS:
- Before creating a PR: git status, git diff, git log to understand all changes.
- Use gh CLI for GitHub operations (gh pr create, gh pr list, etc.).
- PR body format: ## Summary (1-3 bullets) + ## Test plan (checklist).
- Push with -u flag: git push -u origin HEAD.
- Return the PR URL when done.

BRANCHES:
- Don't create branches unless the user asks or the workflow requires it.
- When switching branches, check for uncommitted changes first.
</git_workflow>"""

_MOD_TASK_MANAGEMENT = """<task_management>
Use TodoWrite for any multi-step task. Create the full checklist at the start, mark items in_progress as you begin each one, and mark them completed immediately when done -- don't batch completions. Add discovered work as new items so nothing drops.

Example pattern:
- User asks to fix 5 type errors -> Create 5 todo items -> Work through each, marking complete as you go
- User asks for a new feature -> Break into subtasks (research, implement, test) -> Track progress through each
</task_management>"""

# --- Phase-Specific Modules (one per phase) ---

_MOD_PHASE_SCOUT = """<mission>
Build a complete mental model of this codebase: architecture, patterns, conventions, and risks. You're preparing for implementation, not skimming. Your output directly determines the quality of the plan that follows.
</mission>

<strategy>
1. Foundation first: project_tree for structure, then Read manifest files (package.json, pyproject.toml, requirements.txt, pom.xml, build.gradle). Understand the stack, dependencies, and build system.
2. Smart discovery: Use semantic_retrieve for "where/how" questions, then Read the returned chunks with offset/limit. Use search only for exact strings or identifiers.
3. Batch aggressively: Read 5-12 files per turn. Each batch should answer a specific question about the codebase: "What's the entry point?", "How does auth work?", "What patterns do tests follow?"
4. Follow relevance: Read files that will be touched, their imports, related tests, and shared utilities. Understand the data flow for THIS specific task.
5. Trace dependencies: For every file you plan to change, identify its importers and imports. Use find_symbol for key symbols to understand usage breadth.
6. Know when done: Can you confidently answer ALL of these?
   - Stack, framework, key dependencies?
   - Architecture: how modules communicate, where state lives?
   - Exact files to change, and what each change involves?
   - Patterns to follow: naming, error handling, logging, testing conventions?
   - Build/test/lint commands?
   - What could go wrong? Edge cases, tech debt, tight coupling?
</strategy>

<anti_patterns>
- Don't read files that won't be relevant to the task. Every read should have a purpose.
- Don't stop at surface level. If a function delegates to another module, follow the call chain.
- Don't assume — if you're unsure whether a pattern exists, search for it.
- Don't read entire large files. Use offset/limit after checking the structural overview.
</anti_patterns>

<output_format>
Provide your findings as:
## Stack — Language, framework, key dependencies, versions if visible
## Architecture — How the app is organized, module boundaries, data flow, where state lives
## Files Relevant to This Task — path, role, key functions/classes, line ranges of interest
## Patterns and Conventions — Code style, error handling, logging, DI, existing utilities to reuse (cite specific examples)
## Build/Test/Lint — Exact commands the project uses
## Risks and Gotchas — Edge cases, tech debt, coupling issues, breaking change potential
</output_format>"""

_MOD_PHASE_PLAN = """<mission>
Create a precise, executable implementation plan. Every ambiguity you leave becomes a bug. Think like a senior engineer who has shipped production software, debugged production incidents, and learned from past mistakes.

The plan must be precise enough that another engineer could execute it step-by-step without asking you a single question.
</mission>

<process>
1. UNDERSTAND: Read relevant source files if not already in scout context. Batch reads, follow imports, check tests. Each batch answers a specific question. Stop when you have enough context — don't over-research.
2. DESIGN: Find the simplest approach that fully solves the problem. Reuse existing code and patterns. Consider 2-3 alternatives and explain why your choice is best. Identify reusable utilities, existing test helpers, and shared patterns.
3. VALIDATE: Trace each step mentally. Do they connect? Are there circular dependencies, missing imports, or ordering constraints? Could any step break existing functionality?
4. DOCUMENT: Write a plan precise enough for step-by-step execution:
   - Problem statement: what's broken or missing, and why
   - Approach with reasoning: what you'll do and why this approach over alternatives
   - Files to change: exact paths, with summary of changes per file
   - Numbered implementation steps: each step is one atomic change
   - Verification commands: exact test/lint/build commands to run
   - Risks: what could go wrong, and how to mitigate
</process>

<quality_checks>
Before finalizing the plan, verify:
- Would a junior engineer execute this without asking clarifying questions?
- Are there missing dependencies between steps (e.g. import needed before use)?
- Are there side effects on other files, tests, or modules not mentioned?
- Is there existing code that does something similar that you should reuse?
- Is there a simpler approach you haven't considered?
</quality_checks>

<thinking>
Use your thinking time to: evaluate multiple approaches before committing, trace dependencies for circular refs, imagine failure modes (what happens if the file has changed? what if the test fails?), check for existing code to reuse instead of reinventing, and mentally execute each step to verify ordering.
</thinking>"""

_MOD_PHASE_BUILD = """<execution>
For each implementation step:
1. Read the relevant file if not read this session (NEVER edit blind)
2. Make a precise, minimal change — one logical change per Edit call
3. Re-read the changed section to verify correctness
4. Run lint_file immediately — if errors were introduced, fix them before proceeding
5. If the step involves multiple independent files, batch all Edit calls in one response
6. Mark the TodoWrite item as completed, then move to the next step

Before every edit: understand the current code, trace dependencies, consider impact on callers/imports/tests.
After every edit: verify the change compiles, passes lint, and hasn't broken the surrounding code.
</execution>

<error_recovery>
If something breaks:
1. STOP — don't try random fixes
2. Read the full error message carefully
3. Think about root cause, not symptoms (use your thinking — it's free, shipped bugs are not)
4. If the error is in code you just changed, re-read the file and fix systematically
5. If the error is in code you didn't change, investigate: is it a pre-existing issue or did your change expose it?
6. Verify each fix independently before proceeding
7. If stuck after 2 attempts, step back and reconsider the approach
</error_recovery>

<verification>
- After every edit: re-read changed section, lint_file must pass
- Match existing conventions exactly (naming, error handling, patterns, import style)
- No new dependencies, patterns, or abstractions unless the plan specifies them
- Security: sanitize inputs, no injection vulnerabilities, no hardcoded secrets
- Don't leave dead code, commented-out code, or TODO comments unless explicitly part of the task
- Final check: does this fully solve the original problem? Would a senior reviewer approve this? Are there any loose ends?
</verification>

<anti_patterns>
- Don't make changes beyond what the plan specifies. Resist the urge to "clean up" nearby code.
- Don't add error handling for impossible cases. Don't add comments for obvious code.
- Don't create new utility functions for one-time operations.
- Don't skip lint_file because "it's a small change." Small changes cause big bugs.
- Don't proceed past a failing lint — fix it first.
</anti_patterns>"""

_MOD_PHASE_DIRECT = """<workflow>
Combine understanding, planning, and execution in one seamless flow:
1. Read relevant files — understand constraints, existing patterns, and the surrounding code
2. Think through the approach: reuse existing code, consider edge cases, trace dependencies
3. Make precise changes that fit naturally with the existing codebase — same style, same patterns
4. Re-read the changed section, run lint_file, verify correctness
5. If the task involves multiple files, batch independent edits in one response for efficiency

Your changes should be indistinguishable from the best existing code in the project. Same conventions, same patterns, same quality level. If the codebase is messy, match its style anyway — consistency beats personal preference.
</workflow>

<multi_step>
For tasks with 3+ distinct steps, use TodoWrite to create a checklist at the start. Track progress by marking items in_progress and completed as you go. This keeps both you and the user oriented on complex tasks.
</multi_step>

<guardrails>
- Read before editing. Always.
- Lint after editing. Always.
- Don't add code that wasn't requested (extra features, cleanup, docs).
- Don't use Bash for file operations — use specialized tools.
- If uncertain about the user's intent, ask via AskUserQuestion with structured options.
</guardrails>

<analytical_reasoning>
When the user asks you to analyze, review, investigate, or assess something (not just write code):

APPROACH:
- Be exhaustive. Enumerate ALL findings, not just the first or most obvious one.
- Complete the analysis BEFORE proposing action items. Diagnosis comes before prescription.
- State what IS there, what IS NOT there, and what SHOULD be there. Gaps are as important as findings.

STRUCTURE:
- Use tables to compare items, map requirements to implementations, or show coverage gaps.
- Use numbered lists for sequential findings or prioritized recommendations.
- Categorize findings into clear sections (e.g. "Current State", "Gaps", "Recommendations").
- When comparing approaches, list trade-offs explicitly — not just pros, but cons and constraints.

TEST COVERAGE AND AUDITS:
- Map each requirement or decision to the specific test/code that validates it.
- Explicitly call out requirements with ZERO coverage — these are the most important findings.
- Distinguish between "tested", "partially tested", and "not tested at all".

COMPLETENESS:
- If a thread or discussion raises N distinct concerns, your analysis MUST address every single one. Number them and check them off. Missing even one means the analysis is incomplete.
- When the user shares a conversation with multiple participants, each person's concern is a separate item to address. Trace each concern to whether it's covered.
- For multi-step workflows (e.g. import -> modify -> export), analyze EACH transition, not just the first step. Round-trip behavior and intermediate mutations are where bugs hide.

QUALITY BAR:
- Your analysis should be thorough enough that someone could act on it without asking follow-up questions.
- If you find related issues the user didn't ask about, mention them briefly at the end.
</analytical_reasoning>"""

# --- Language-Specific Modules ---

_MOD_LANG_PYTHON = """<language_conventions lang="python">
- Type hints on all new functions and methods. Match existing style (simple built-in types vs typing module).
- PEP 8 naming: snake_case for functions/variables, PascalCase for classes, UPPER_CASE for constants.
- Prefer pathlib over os.path. Prefer f-strings over .format() or %.
- Use dataclasses or Pydantic for data structures -- match whichever the project already uses.
- In async codebases: use async/await consistently. Never mix sync blocking calls in async code.
- Docstrings on new public functions only. Match existing style (Google, NumPy, or reST).
- Use context managers (with statements) for resource cleanup.
- Standard import order: stdlib, third-party, local. Match existing tooling (isort/black config).
</language_conventions>"""

_MOD_LANG_JAVA = """<language_conventions lang="java">
- camelCase for methods/variables, PascalCase for classes/interfaces, UPPER_SNAKE_CASE for constants.
- Use Optional<T> instead of returning null. Never pass null intentionally.
- Use the project's existing DI framework (Spring @Autowired/@Inject, Guice, etc.).
- Prefer immutable data: final fields, Collections.unmodifiableList(), records where available.
- Use try-with-resources for AutoCloseable. Follow the project's existing exception hierarchy.
- Don't create checked exceptions for internal logic. Use the project's error pattern.
- Follow Maven/Gradle standard layout: src/main/java, src/test/java.
- Match existing logging pattern (SLF4J, Log4j2, java.util.logging). Use appropriate log levels.
</language_conventions>"""

_MOD_LANG_JAVASCRIPT = """<language_conventions lang="javascript/typescript">
- camelCase for variables/functions, PascalCase for classes/components/types, UPPER_SNAKE_CASE for constants.
- Prefer const over let. Never use var.
- Use async/await over raw Promises. Handle errors with try/catch, not .catch() chains.
- In TypeScript: explicit return types on exported functions. Use interfaces over type aliases for object shapes.
- Match existing patterns: functional vs class components, state management library, error boundaries.
- Use optional chaining (?.) and nullish coalescing (??) over manual null checks.
</language_conventions>"""

LANG_MODULES = {
    "python": _MOD_LANG_PYTHON,
    "java": _MOD_LANG_JAVA,
    "javascript": _MOD_LANG_JAVASCRIPT,
    "typescript": _MOD_LANG_JAVASCRIPT,
}

PHASE_MODULES = {
    "scout": _MOD_PHASE_SCOUT,
    "plan": _MOD_PHASE_PLAN,
    "build": _MOD_PHASE_BUILD,
    "direct": _MOD_PHASE_DIRECT,
}


def _detect_project_language(working_directory: str) -> Optional[str]:
    """Detect the primary language of a project from manifest files."""
    checks = [
        ("pom.xml", "java"),
        ("build.gradle", "java"),
        ("build.gradle.kts", "java"),
        ("pyproject.toml", "python"),
        ("requirements.txt", "python"),
        ("setup.py", "python"),
        ("Pipfile", "python"),
        ("package.json", "javascript"),
        ("tsconfig.json", "typescript"),
    ]
    for filename, lang in checks:
        if os.path.exists(os.path.join(working_directory, filename)):
            return lang
    return None


def _compose_system_prompt(
    phase: str,
    working_directory: str,
    tool_names: str,
    language: Optional[str] = None,
) -> str:
    """Assemble the system prompt from modules based on current phase and context."""
    parts = [
        _MOD_IDENTITY,
        _MOD_DOING_TASKS,
        _MOD_CAREFUL_EXECUTION,
        _MOD_TONE_AND_STYLE,
        _MOD_TOOL_POLICY,
        _MOD_GIT_WORKFLOW,
        _MOD_TASK_MANAGEMENT,
    ]

    # Phase-specific module
    phase_mod = PHASE_MODULES.get(phase)
    if phase_mod:
        parts.append(phase_mod)

    # Language-specific module (if detected)
    if language and language in LANG_MODULES:
        parts.append(LANG_MODULES[language])

    # Working directory and available tools (always last)
    parts.append(f"<working_directory>{working_directory}</working_directory>")
    parts.append(f"<tools_available>{tool_names}</tools_available>")

    return "\n\n".join(parts)

# ============================================================
# Intent Classification
# ============================================================

CLASSIFY_SYSTEM = """You are a task classifier for a coding agent. Analyze the user's message and return ONLY this JSON:
{"scout": true/false, "plan": true/false, "question": true/false, "complexity": "trivial"|"simple"|"complex"}

**Guidelines**:
- **Trivial**: Greetings, yes/no, single commands, short confirmations
- **Simple**: Single-file edits, questions about specific code, explanations
- **Complex**: Multi-file changes, architecture work, new features, refactors

**question** = true when the user is asking a question, requesting an explanation, or having a conversation (NOT requesting code changes). Questions should be answered directly and quickly.

**Scout needed when**: Need to understand codebase structure or find existing code to answer
**Plan needed when**: Multi-step coordination across multiple files required (NOT for questions)

**Examples**:
- "Fix the bug in auth.py" → {"scout": true, "plan": false, "question": false, "complexity": "simple"}
- "Add user authentication system" → {"scout": true, "plan": true, "question": false, "complexity": "complex"}
- "What does this function do?" → {"scout": true, "plan": false, "question": true, "complexity": "simple"}
- "Run the tests" → {"scout": false, "plan": false, "question": false, "complexity": "trivial"}
- "Explain how recursion works" → {"scout": false, "plan": false, "question": true, "complexity": "simple"}
- "Hi, how are you?" → {"scout": false, "plan": false, "question": true, "complexity": "trivial"}
- "What's the difference between REST and GraphQL?" → {"scout": false, "plan": false, "question": true, "complexity": "simple"}
- "Can you explain what this error means?" → {"scout": true, "plan": false, "question": true, "complexity": "simple"}
- "Refactor the database layer to use connection pooling" → {"scout": true, "plan": true, "question": false, "complexity": "complex"}

When uncertain: scout=true (cheap), plan=false, question=false."""

# ============================================================
# Agent Events & Data Types  
# ============================================================

@dataclass
class AgentEvent:
    """Event emitted during agent execution"""
    type: str  # phase_start, tool_call, tool_result, text, thinking, error, done, etc.
    content: str = ""
    data: Optional[Dict[str, Any]] = None

@dataclass 
class PolicyDecision:
    """Policy engine decision for requested operation"""
    require_approval: bool = False
    blocked: bool = False
    reason: str = ""

# ============================================================
# Utilities
# ============================================================

_PLAN_RE = re.compile(r"<plan>\s*(.*?)\s*</plan>", re.DOTALL)

def _extract_plan(text: str) -> Optional[str]:
    """Extract content between <plan>...</plan> tags."""
    m = _PLAN_RE.search(text)
    return m.group(1).strip() if m else None

def _format_build_system_prompt(working_directory: str, language: Optional[str] = None) -> str:
    return _compose_system_prompt("build", working_directory, AVAILABLE_TOOL_NAMES, language=language)
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
            system_prompt=CLASSIFY_SYSTEM,
            model_id=app_config.scout_model,
            config=config,
        )
        # Parse the JSON from the response
        text = resp.content.strip()
        # Handle possible markdown wrapping
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        import json as _json
        # Extract first JSON object if LLM returned extra text
        brace_start = text.find("{")
        if brace_start >= 0:
            depth, end = 0, brace_start
            for i in range(brace_start, len(text)):
                if text[i] == "{": depth += 1
                elif text[i] == "}": depth -= 1
                if depth == 0:
                    end = i + 1
                    break
            text = text[brace_start:end]
        result = _json.loads(text)
        complexity = result.get("complexity", "simple")
        if complexity not in ("trivial", "simple", "complex"):
            complexity = "simple"
        result = {
            "scout": bool(result.get("scout", True)),
            "plan": bool(result.get("plan", False)),
            "question": bool(result.get("question", False)),
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
        return {"scout": False, "plan": False, "question": False, "complexity": "trivial"}
    # Detect questions by common patterns
    question_starters = ("what", "why", "how", "explain", "can you explain",
                         "tell me", "describe", "is it", "are there", "do you",
                         "could you", "would you", "hi", "hello", "hey")
    is_question = (stripped.endswith("?") or
                   any(stripped.startswith(q) for q in question_starters))
    if is_question:
        return {"scout": True, "plan": False, "question": True, "complexity": "simple"}
    # Default: scout yes (cheap and helpful), plan no
    return {"scout": True, "plan": False, "question": False, "complexity": "simple"}


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
        max_iterations: int = 100,
        backend: Optional["Backend"] = None,
    ):
        self.service = bedrock_service
        self.backend: Backend = backend or LocalBackend(os.path.abspath(working_directory))
        is_ssh = getattr(self.backend, "_host", None) is not None
        # For SSH, keep working_directory as the remote path (no local abspath to avoid cache collision).
        self.working_directory = (working_directory if is_ssh else os.path.abspath(working_directory))
        self._backend_id = (f"ssh:{getattr(self.backend, '_host', '')}:{self.working_directory}" if is_ssh else "local")
        self.max_iterations = max_iterations
        self.history: List[Dict[str, Any]] = []
        self._detected_language = _detect_project_language(self.working_directory)
        self.system_prompt = _compose_system_prompt("direct", self.working_directory, AVAILABLE_TOOL_NAMES, language=self._detected_language)
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
        # Session checkpoints for rewind across risky batches
        self._session_checkpoints: List[Dict[str, Any]] = []
        self._checkpoint_counter: int = 0
        # Deterministic verification gate status for current run/build
        self._deterministic_verification_done: bool = False
        # Task decomposition of current plan (execution batches)
        self._current_plan_decomposition: List[Dict[str, Any]] = []
        # Plan file path and full text — persisted so "Open in Editor" survives reload
        self._plan_file_path: Optional[str] = None
        self._plan_text: str = ""
        # In-memory cache of learned failure patterns
        self._failure_pattern_cache: Optional[List[Dict[str, Any]]] = None
        self._todos: List[Dict[str, Any]] = []
        self._memory: Dict[str, str] = {}  # key -> value for MemoryWrite/MemoryRead
        
        # Enhanced caching and state management inspired by modern build systems
        self._verification_cache: Dict[str, Dict[str, Any]] = {}  # file_hash -> verification_results
        self._dependency_graph: Dict[str, List[str]] = {}  # file -> dependent_files
        self._last_verification_hashes: Dict[str, str] = {}  # abs_path -> last_verified_hash
        self._incremental_state: Dict[str, Any] = {}  # persistent state across sessions

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
        self._session_checkpoints = []
        self._checkpoint_counter = 0
        self._deterministic_verification_done = False
        self._verification_gate_attempts = 0
        self._current_plan_decomposition = []
        self._plan_file_path = None
        self._plan_text = ""
        self._failure_pattern_cache = None
        self._todos = []
        self._memory = {}

    def _file_cache_key(self, path: str) -> str:
        """Return cache key for path (backend_id + resolved path so SSH and local never collide)."""
        resolved = self.backend.resolve_path(path)
        return f"{self._backend_id}\x00{resolved}"

    def _path_from_cache_key(self, key: str) -> str:
        """Extract resolved path from a cache key for backend calls."""
        if "\x00" in key:
            return key.split("\x00", 1)[1]
        return key

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
        Returns list of reverted file paths. If checkpoint content is None (file
        was missing or unreadable at capture), removes the file if it exists now."""
        if step_num not in self._step_checkpoints:
            return []
        checkpoint = self._step_checkpoints[step_num]
        reverted = []
        for abs_path, content in checkpoint.items():
            try:
                if content is not None:
                    self.backend.write_file(abs_path, content)
                    reverted.append(abs_path)
                else:
                    # File was missing/unreadable at capture — remove if present (restore "did not exist")
                    if self.backend.file_exists(abs_path):
                        self.backend.remove_file(abs_path)
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
    # Session checkpoints + rewind
    # ------------------------------------------------------------------

    def _create_session_checkpoint(self, label: str, target_paths: Optional[List[str]] = None) -> Optional[str]:
        """Capture a checkpoint of current file states before risky operations."""
        if not app_config.session_checkpoints_enabled:
            return None

        paths: List[str] = []
        if target_paths:
            paths.extend([p for p in target_paths if p])
        if not paths:
            paths.extend(list(self._file_snapshots.keys()))
        if not paths:
            paths.extend([self._path_from_cache_key(k) for k in self._file_cache.keys()])
        if not paths:
            return None

        files: Dict[str, Optional[str]] = {}
        for abs_path in sorted(set(paths)):
            try:
                if self.backend.file_exists(abs_path):
                    files[abs_path] = self.backend.read_file(abs_path)
                else:
                    files[abs_path] = None
            except Exception:
                files[abs_path] = None
        if not files:
            return None

        self._checkpoint_counter += 1
        checkpoint_id = f"cp-{int(time.time())}-{self._checkpoint_counter}"
        self._session_checkpoints.append({
            "id": checkpoint_id,
            "label": label[:120],
            "created_at": int(time.time()),
            "files": files,
        })
        # Keep only recent checkpoints
        self._session_checkpoints = self._session_checkpoints[-25:]
        return checkpoint_id

    def list_session_checkpoints(self) -> List[Dict[str, Any]]:
        """List checkpoints without embedding full file payloads."""
        out = []
        for cp in self._session_checkpoints:
            out.append({
                "id": cp.get("id"),
                "label": cp.get("label", ""),
                "created_at": cp.get("created_at", 0),
                "file_count": len(cp.get("files", {}) or {}),
            })
        return out

    def rewind_to_checkpoint(self, checkpoint_id: str = "latest") -> List[str]:
        """Restore files from a session checkpoint id (or latest)."""
        if not self._session_checkpoints:
            return []
        checkpoint = None
        if checkpoint_id == "latest":
            checkpoint = self._session_checkpoints[-1]
        else:
            for cp in self._session_checkpoints:
                if cp.get("id") == checkpoint_id:
                    checkpoint = cp
                    break
        if not checkpoint:
            return []

        reverted: List[str] = []
        files = checkpoint.get("files", {}) or {}
        for abs_path, content in files.items():
            try:
                if content is None:
                    if self.backend.file_exists(abs_path):
                        self.backend.remove_file(abs_path)
                        reverted.append(abs_path)
                else:
                    self.backend.write_file(abs_path, content)
                    reverted.append(abs_path)
                self._file_cache.pop(f"{self._backend_id}\x00{abs_path}", None)
            except Exception as e:
                logger.warning(f"Failed to rewind {abs_path} from checkpoint {checkpoint.get('id')}: {e}")
        return reverted

    # ------------------------------------------------------------------
    # Project rules (.cursor/rules, .cursorrules, RULE.md, CLAUDE.md)
    # ------------------------------------------------------------------

    _PROJECT_RULES_MAX_CHARS = 8000

    def _load_project_rules(self) -> str:
        """Load project rule files and return concatenated content for system prompt.
        Tries: .cursorrules, RULE.md, CLAUDE.md, .claude/CLAUDE.md,
        .cursor/RULE.md, .cursor/rules/*.mdc, .cursor/rules/*.md.
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
        _add("CLAUDE.md", "CLAUDE.md")
        _add(".claude/CLAUDE.md", ".claude/CLAUDE.md")
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

    def _generate_context_metadata(self, project_docs: str) -> str:
        """
        Generate strategic metadata about the provided project context.
        Helps Claude understand what information is available and its scope.
        """
        import re
        
        lines = project_docs.split('\n')
        total_lines = len(lines)
        
        # Count different types of documentation
        readme_count = len(re.findall(r'--- README.*---', project_docs, re.IGNORECASE))
        contributing_count = len(re.findall(r'--- CONTRIBUTING.*---', project_docs, re.IGNORECASE))  
        doc_sections = len(re.findall(r'--- .*\.md.*---', project_docs))
        code_snippets = len(re.findall(r'```', project_docs))
        
        # Estimate completeness based on truncation markers
        is_truncated = "..." in project_docs or "[truncated]" in project_docs.lower()
        max_chars_reached = len(project_docs) >= (self._PROJECT_DOCS_MAX_CHARS * 0.9)
        
        metadata_parts = [
            "📋 **CONTEXT METADATA**:",
            f"- **Scope**: {total_lines:,} lines across {doc_sections} documentation files",
        ]
        
        if readme_count > 0:
            metadata_parts.append("- **Architecture**: README with project overview available")
        if contributing_count > 0:
            metadata_parts.append("- **Process**: Contributing guidelines included")
        if code_snippets > 0:
            metadata_parts.append(f"- **Examples**: {code_snippets // 2} code examples/snippets")
        
        # Completeness assessment
        if is_truncated or max_chars_reached:
            metadata_parts.append("⚠️  **Completeness**: PARTIAL - Some docs may be truncated due to size limits")
            metadata_parts.append("💡 **Strategy**: Use tools to read specific files for complete details")
        else:
            metadata_parts.append("✅ **Completeness**: FULL - Complete project documentation loaded")
        
        # Usage guidance
        metadata_parts.extend([
            "",
            "🎯 **How to Use This Context**:",
            "- This context provides project overview and patterns",
            "- For implementation details, read specific source files using tools",
            "- Look for existing patterns and utilities to reuse",
            "- Pay attention to architectural decisions and constraints",
        ])
        
        return "\n".join(metadata_parts)

    def _parse_structured_response(self, response_text: str) -> Dict[str, Any]:
        """
        Parse structured responses that follow the expected output format.
        Extracts key sections for better workflow integration.
        """
        import re
        
        structured = {
            "overview": None,
            "analysis": None,
            "implementation": None,
            "verification": None,
            "confidence": None,
            "has_structure": False
        }
        
        # Look for structured sections
        sections = [
            (r"🎯\s*IMPLEMENTATION OVERVIEW\*\*(.*?)(?=\*\*|$)", "overview"),
            (r"🔍\s*ANALYSIS\*\*(.*?)(?=\*\*|$)", "analysis"),
            (r"⚙️\s*IMPLEMENTATION\*\*(.*?)(?=\*\*|$)", "implementation"),
            (r"✅\s*VERIFICATION\*\*(.*?)(?=\*\*|$)", "verification"),
        ]
        
        for pattern, key in sections:
            match = re.search(pattern, response_text, re.DOTALL | re.IGNORECASE)
            if match:
                structured[key] = match.group(1).strip()
                structured["has_structure"] = True
        
        # Extract confidence indicators
        confidence_match = re.search(r"(🟢|🟡|🔴)", response_text)
        if confidence_match:
            emoji = confidence_match.group(1)
            if emoji == "🟢":
                structured["confidence"] = "high"
            elif emoji == "🟡":
                structured["confidence"] = "medium"  
            elif emoji == "🔴":
                structured["confidence"] = "low"
        
        return structured

    def _effective_system_prompt(self, base: str) -> str:
        """Return system prompt with dynamic context sections appended.

        Adds (in order):
        1. Project rules (from .bedrock-codex/rules or AGENTS.md)
        2. Learned failure patterns
        3. Current todo checklist
        4. Dynamic system reminders (contextual nudges)
        """
        prompt = base

        # --- Project rules ---
        rules = self._load_project_rules()
        if rules:
            prompt += "\n\n<project_rules>\nThese project-specific rules MUST be followed:\n\n" + rules + "\n</project_rules>"

        # --- Learned failure patterns ---
        learned = self._failure_patterns_prompt()
        if learned:
            prompt += "\n\n<known_failure_patterns>\n" + learned + "\n</known_failure_patterns>"

        # --- Current todos ---
        if self._todos:
            lines = ["<current_todos>", "Your task checklist (update with TodoWrite as you progress):"]
            for t in self._todos:
                s = t.get("status", "pending")
                c = (t.get("content") or "").strip()
                lines.append(f"  [{s}] {c}")
            lines.append("</current_todos>")
            prompt += "\n\n" + "\n".join(lines)

        # --- Dynamic system reminders ---
        reminders = self._gather_system_reminders()
        if reminders:
            prompt += "\n\n<system_reminders>\n" + "\n".join(f"- {r}" for r in reminders) + "\n</system_reminders>"

        return prompt

    def _gather_system_reminders(self) -> List[str]:
        """Collect contextual reminders based on current agent state.

        These are injected into the system prompt to nudge the model
        toward better behavior, similar to Claude Code's 40+ reminders.
        """
        reminders: List[str] = []

        # Remind about pending plan
        if self._current_plan:
            reminders.append("There is an active implementation plan. Follow the plan steps in order.")

        # Remind about todo tracking on multi-step tasks
        if not self._todos and self._current_plan and len(self._current_plan) > 2:
            reminders.append("You have a multi-step plan but no todos. Use TodoWrite to create a checklist for tracking progress.")

        # Remind about file snapshots (modified files exist)
        if self._file_snapshots:
            n = len(self._file_snapshots)
            reminders.append(f"You have {n} file(s) with pending modifications. The user can keep or revert these changes.")

        return reminders

    # ------------------------------------------------------------------
    # Learning loop from failures
    # ------------------------------------------------------------------

    def _failure_memory_path(self) -> str:
        return os.path.join(self.working_directory, ".bedrock-codex", "learning", "failure_patterns.json")

    def _load_failure_patterns(self) -> List[Dict[str, Any]]:
        if self._failure_pattern_cache is not None:
            return self._failure_pattern_cache
        path = self._failure_memory_path()
        try:
            if not os.path.exists(path):
                self._failure_pattern_cache = []
                return []
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self._failure_pattern_cache = data
                return data
        except Exception as e:
            logger.debug(f"Could not load failure patterns: {e}")
        self._failure_pattern_cache = []
        return []

    def _save_failure_patterns(self, rows: List[Dict[str, Any]]) -> None:
        path = self._failure_memory_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(rows, f, indent=2)
            self._failure_pattern_cache = rows
        except Exception as e:
            logger.debug(f"Could not save failure patterns: {e}")

    def _record_failure_pattern(self, kind: str, detail: str, context: Optional[Dict[str, Any]] = None) -> None:
        """Persist recurring failure signatures to improve future runs."""
        if not app_config.learning_loop_enabled:
            return
        detail = (detail or "").strip()
        if not detail:
            return
        sig_src = f"{kind}:{detail[:400]}"
        signature = hashlib.sha1(sig_src.encode("utf-8")).hexdigest()[:16]
        rows = self._load_failure_patterns()
        now = int(time.time())
        found = False
        for row in rows:
            if row.get("signature") == signature:
                row["count"] = int(row.get("count", 1)) + 1
                row["last_seen"] = now
                if context:
                    row["last_context"] = context
                found = True
                break
        if not found:
            rows.append({
                "signature": signature,
                "kind": kind,
                "detail": detail[:1200],
                "count": 1,
                "first_seen": now,
                "last_seen": now,
                "last_context": context or {},
            })
        rows = sorted(rows, key=lambda r: (int(r.get("count", 1)), int(r.get("last_seen", 0))), reverse=True)[:200]
        self._save_failure_patterns(rows)

    def _failure_patterns_prompt(self) -> str:
        rows = self._load_failure_patterns()
        if not rows:
            return ""
        lines = []
        for row in rows[:8]:
            lines.append(
                f"- [{row.get('kind','failure')}] x{row.get('count',1)}: {str(row.get('detail',''))[:180]}"
            )
        if not lines:
            return ""
        return "Avoid repeating these known failure patterns:\n" + "\n".join(lines)

    # ------------------------------------------------------------------
    # Policy engine for risky operations
    # ------------------------------------------------------------------

    def _policy_decision(self, tool_name: str, tool_input: Dict[str, Any]) -> PolicyDecision:
        if not app_config.policy_engine_enabled:
            return PolicyDecision()

        # File path protections
        if tool_name in ("Write", "Edit", "symbol_edit"):
            path = (tool_input.get("path", "") or "").lower()
            protected = (".env", "credentials", "secret", "id_rsa", ".pem", "token")
            if any(tok in path for tok in protected):
                return PolicyDecision(require_approval=True, reason="Sensitive file path requires explicit approval.")

        # Command protections
        if tool_name == "Bash":
            cmd = (tool_input.get("command", "") or "").strip().lower()
            destructive_patterns = [
                "rm -rf",
                "git reset --hard",
                "git checkout --",
                "git clean -fd",
                "drop table",
                "truncate table",
                "sudo rm",
            ]
            shared_impact_patterns = [
                "git push --force",
                "git push -f",
                "gh pr merge",
                "terraform apply",
                "kubectl delete",
                "helm uninstall",
            ]
            if any(p in cmd for p in destructive_patterns):
                if app_config.block_destructive_commands:
                    return PolicyDecision(blocked=True, reason="Blocked destructive command by policy engine.")
                return PolicyDecision(require_approval=True, reason="Destructive command requires explicit approval.")
            if any(p in cmd for p in shared_impact_patterns):
                return PolicyDecision(require_approval=True, reason="Shared-impact command requires explicit approval.")

        return PolicyDecision()

    # ------------------------------------------------------------------
    # Task decomposition executor metadata
    # ------------------------------------------------------------------

    def _extract_step_targets(self, step: str) -> List[str]:
        """Extract likely file paths from a plan step line."""
        if not step:
            return []
        quoted = re.findall(r"`([^`]+)`", step)
        path_like = [q for q in quoted if "/" in q or "." in os.path.basename(q)]
        if path_like:
            return path_like[:3]
        # Fallback: rough path-like tokens
        toks = re.findall(r"[A-Za-z0-9_\-./]+\.[A-Za-z0-9]+", step)
        return toks[:3]

    def _decompose_plan_steps(self, steps: List[str]) -> List[Dict[str, Any]]:
        """Create execution batches: file-work batches then command/verification batches."""
        batches: List[Dict[str, Any]] = []
        current: Dict[str, Any] = {"type": "file_batch", "steps": [], "targets": []}
        for idx, step in enumerate(steps, start=1):
            s = step.strip()
            is_run = bool(re.search(r"\b\[run\]\b|\brun\b|\bverify\b|\btest\b|\blint\b", s, flags=re.IGNORECASE))
            targets = self._extract_step_targets(s)
            item = {"index": idx, "step": s, "targets": targets}
            if is_run:
                if current["steps"]:
                    batches.append(current)
                    current = {"type": "file_batch", "steps": [], "targets": []}
                batches.append({"type": "command_batch", "steps": [item], "targets": targets})
                continue
            current["steps"].append(item)
            current["targets"].extend(targets)
        if current["steps"]:
            batches.append(current)
        for b_idx, b in enumerate(batches, start=1):
            b["batch"] = b_idx
            b["targets"] = sorted(list(dict.fromkeys(b.get("targets", []))))[:20]
        return batches

    async def _run_parallel_manager_workers(self, task: str, decomposition: List[Dict[str, Any]]) -> str:
        """Run lightweight parallel worker analyses and merge into manager insights."""
        if not app_config.parallel_subagents_enabled:
            return ""
        lanes = [b for b in decomposition if b.get("type") == "file_batch" and b.get("steps")]
        if len(lanes) < 2:
            return ""

        max_workers = max(1, min(app_config.parallel_subagents_max_workers, len(lanes), 4))
        selected = lanes[:max_workers]
        loop = asyncio.get_event_loop()
        cfg = GenerationConfig(
            max_tokens=1800,
            enable_thinking=False,
            throughput_mode=model_config.throughput_mode,
        )

        def _worker_prompt(batch: Dict[str, Any]) -> str:
            steps = "\n".join(f"- {s.get('step','')}" for s in batch.get("steps", [])[:8])
            targets = ", ".join(batch.get("targets", [])[:12]) or "n/a"
            return (
                "You are a worker agent for one execution lane.\n"
                "Return concise actionable guidance with this exact format:\n"
                "Edits:\n- ...\nRisks:\n- ...\nVerification:\n- ...\n\n"
                f"Task:\n{task[:2000]}\n\n"
                f"Lane batch #{batch.get('batch')} targets: {targets}\n"
                f"Lane steps:\n{steps}\n"
            )

        async def _run_worker(batch: Dict[str, Any]) -> str:
            prompt = _worker_prompt(batch)
            def _call():
                res = self.service.generate_response(
                    messages=[{"role": "user", "content": prompt}],
                    system_prompt="You produce terse worker execution guidance for a coding manager.",
                    model_id=app_config.fast_model or self.service.model_id,
                    config=cfg,
                )
                return (res.content or "").strip()
            try:
                out = await loop.run_in_executor(None, _call)
                return out
            except Exception as e:
                return f"Worker failed: {e}"

        worker_outputs = await asyncio.gather(*[_run_worker(b) for b in selected])
        merged_lines = []
        for idx, txt in enumerate(worker_outputs, start=1):
            if not txt:
                continue
            merged_lines.append(f"### Worker lane {idx}\n{txt[:2000]}")
        if not merged_lines:
            return ""
        return "\n\n".join(merged_lines)

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

    def _select_impacted_tests(self, modified_paths: List[str]) -> List[str]:
        """Select likely impacted tests before any full-suite run."""
        impacted = []
        seen = set()

        # 1) Structural adjacency heuristics
        for tf in self._discover_test_files(modified_paths):
            if tf not in seen:
                impacted.append(tf)
                seen.add(tf)

        if not app_config.test_impact_selection_enabled:
            return impacted

        # 2) Lightweight symbol/name matching in known test dirs
        candidate_roots = ["tests", "test", "__tests__"]
        for abs_path in modified_paths[:30]:
            base = os.path.basename(abs_path)
            name, _ext = os.path.splitext(base)
            if not name:
                continue
            pattern = re.escape(name)
            for root in candidate_roots:
                try:
                    if not self.backend.file_exists(root) or not self.backend.is_dir(root):
                        continue
                    raw = self.backend.search(pattern, root, include="*.py", cwd=".")
                    if not raw:
                        continue
                    for line in raw.split("\n")[:60]:
                        m = re.match(r"^(.+?):\d+:", line.strip())
                        if not m:
                            continue
                        p = m.group(1).strip()
                        rel = os.path.relpath(p, self.working_directory) if os.path.isabs(p) else p
                        if rel not in seen:
                            impacted.append(rel)
                            seen.add(rel)
                except Exception:
                    continue

        return impacted[:40]

    def _snapshot_file(self, tool_name: str, tool_input: Dict[str, Any]) -> None:
        """Capture the original content of a file before it's modified.
        Only snapshots once per file per build run — first write wins."""
        if tool_name not in ("Write", "Edit", "symbol_edit"):
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
        - Created file (snapshot None or {created, content}): remove if present; if already
          deleted by agent, restore the created content so 'Revert' brings the file back.
        - Modified file (snapshot str): write back original content.
        Returns a list of reverted file paths."""
        reverted = []
        for abs_path, original in self._file_snapshots.items():
            try:
                created_with_content = isinstance(original, dict) and original.get("created") and "content" in original
                if original is None:
                    # Legacy: file was created, we don't have content — just delete if present
                    if self.backend.file_exists(abs_path):
                        self.backend.remove_file(abs_path)
                        reverted.append(abs_path)
                elif created_with_content:
                    # Created file with stored content: if still exists, delete; else restore
                    content = original["content"]
                    if self.backend.file_exists(abs_path):
                        self.backend.remove_file(abs_path)
                    else:
                        self.backend.write_file(abs_path, content)
                    reverted.append(abs_path)
                else:
                    # Modified file — restore original content
                    if isinstance(original, str):
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
        if tool_name == "Bash":
            return f"cmd:{tool_input.get('command', '')}"
        elif tool_name in ("Write", "Edit", "symbol_edit"):
            path = tool_input.get("path", "")
            resolved = self.backend.resolve_path(path)
            return f"{tool_name}:{self._backend_id}:{resolved}"
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
            elif isinstance(content, dict) and content.get("created") and "content" in content:
                # Created file with content (so Revert can restore if deleted)
                raw = content["content"]
                if isinstance(raw, str) and len(raw) < 1_000_000:
                    try:
                        raw.encode("utf-8")
                        snapshots[path] = content
                    except (UnicodeDecodeError, UnicodeEncodeError):
                        pass
            elif isinstance(content, str) and len(content) < 1_000_000:
                try:
                    content.encode("utf-8")  # verify it's text
                    snapshots[path] = content
                except (UnicodeDecodeError, UnicodeEncodeError):
                    pass  # skip binary files

        checkpoints: List[Dict[str, Any]] = []
        for cp in self._session_checkpoints[-10:]:
            files = {}
            for p, c in (cp.get("files", {}) or {}).items():
                if c is None:
                    files[p] = None
                elif isinstance(c, str) and len(c) < 1_000_000:
                    files[p] = c
            checkpoints.append({
                "id": cp.get("id"),
                "label": cp.get("label", ""),
                "created_at": cp.get("created_at", 0),
                "files": files,
            })

        # Step checkpoints for "Revert to here" — same size rules, cap to last 15 steps
        step_checkpoints_ser: Dict[str, Dict[str, Optional[str]]] = {}
        for step_num, cp_files in sorted(self._step_checkpoints.items(), reverse=True)[:15]:
            files_ser: Dict[str, Optional[str]] = {}
            for path, content in (cp_files or {}).items():
                if content is None:
                    files_ser[path] = None
                elif len(content) < 1_000_000:
                    try:
                        content.encode("utf-8")
                        files_ser[path] = content
                    except (UnicodeDecodeError, UnicodeEncodeError):
                        pass
            step_checkpoints_ser[str(step_num)] = files_ser

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
            "current_plan_decomposition": self._current_plan_decomposition,
            "plan_file_path": self._plan_file_path,
            "plan_text": self._plan_text,
            "scout_context": self._scout_context,
            "file_snapshots": snapshots,
            "session_checkpoints": checkpoints,
            "checkpoint_counter": self._checkpoint_counter,
            "step_checkpoints": step_checkpoints_ser,
            "plan_step_index": self._plan_step_index,
            "deterministic_verification_done": self._deterministic_verification_done,
            "todos": list(self._todos),
            "memory": dict(self._memory),
        }

    def from_dict(self, data: Dict[str, Any]) -> None:
        """Restore agent state from a persisted session. Unknown keys are ignored."""
        known = {
            "history", "token_usage", "approved_commands", "running_summary", "current_plan",
            "current_plan_decomposition", "plan_file_path", "plan_text", "scout_context",
            "file_snapshots", "session_checkpoints", "checkpoint_counter", "step_checkpoints",
            "plan_step_index", "deterministic_verification_done", "todos", "memory",
        }
        unknown = set(data) - known
        if unknown:
            logger.debug("Agent from_dict: ignoring unknown keys %s", sorted(unknown))
        self.history = data.get("history", [])
        usage = data.get("token_usage", {})
        self._total_input_tokens = usage.get("input_tokens", 0)
        self._total_output_tokens = usage.get("output_tokens", 0)
        self._cache_read_tokens = usage.get("cache_read_tokens", 0)
        self._cache_write_tokens = usage.get("cache_write_tokens", 0)
        self._approved_commands = set(data.get("approved_commands", []))
        self._running_summary = data.get("running_summary", "")
        self._current_plan = data.get("current_plan")
        self._current_plan_decomposition = data.get("current_plan_decomposition", [])
        self._plan_file_path = data.get("plan_file_path")
        self._plan_text = data.get("plan_text", "") or ""
        self._scout_context = data.get("scout_context")
        self._plan_step_index = data.get("plan_step_index", 0)
        self._deterministic_verification_done = data.get("deterministic_verification_done", False)
        self._todos = list(data.get("todos", []))
        raw_memory = data.get("memory", {})
        self._memory = dict(raw_memory) if isinstance(raw_memory, dict) else {}
        self._cancelled = False
        # Restore file snapshots
        raw_snapshots = data.get("file_snapshots", {})
        if isinstance(raw_snapshots, dict):
            self._file_snapshots = raw_snapshots
        else:
            self._file_snapshots = {}
        cps = data.get("session_checkpoints", [])
        if isinstance(cps, list):
            normalized = []
            for cp in cps:
                if not isinstance(cp, dict):
                    continue
                files = cp.get("files", {})
                if not isinstance(files, dict):
                    files = {}
                normalized.append({
                    "id": cp.get("id"),
                    "label": cp.get("label", ""),
                    "created_at": cp.get("created_at", 0),
                    "files": files,
                })
            self._session_checkpoints = normalized
        else:
            self._session_checkpoints = []
        self._checkpoint_counter = int(data.get("checkpoint_counter", 0) or 0)
        # Restore step checkpoints for "Revert to here" after reconnect
        raw_step_cps = data.get("step_checkpoints", {})
        if isinstance(raw_step_cps, dict):
            self._step_checkpoints = {}
            for k, v in raw_step_cps.items():
                try:
                    step_num = int(k)
                    if step_num >= 1 and isinstance(v, dict):
                        self._step_checkpoints[step_num] = dict(v)
                except (ValueError, TypeError):
                    pass
        else:
            self._step_checkpoints = {}

    def _default_config(self) -> GenerationConfig:
        """Create default generation config. Use at least the model's default max_tokens so we never hit 'ran out of tokens'."""
        model_id = self.service.model_id
        default_max = get_default_max_tokens(model_id)
        max_tok = max(model_config.max_tokens, default_max)
        return GenerationConfig(
            max_tokens=max_tok,
            enable_thinking=model_config.enable_thinking and supports_thinking(model_id),
            thinking_budget=model_config.thinking_budget if supports_thinking(model_id) else 0,
            use_adaptive_thinking=model_config.use_adaptive_thinking,
            adaptive_thinking_effort=model_config.adaptive_thinking_effort,
            throughput_mode=model_config.throughput_mode,
        )

    def _get_generation_config_for_phase(self, phase: str, base_config: Optional[GenerationConfig] = None) -> GenerationConfig:
        """
        Create phase-specific generation config with optimized sampling parameters.
        
        Scout: Fast exploration (balanced sampling for discovery)
        Plan: Careful reasoning (deterministic + deep thinking for rigor)
        Build: Precision execution (very deterministic for consistent, correct code)
        Verify: Deep analysis (deterministic + maximum thinking for thoroughness)
        """
        if base_config is None:
            base_config = self._default_config()
            
        config = GenerationConfig(
            max_tokens=base_config.max_tokens,
            enable_thinking=base_config.enable_thinking,
            thinking_budget=base_config.thinking_budget,
            use_adaptive_thinking=base_config.use_adaptive_thinking,
            adaptive_thinking_effort=base_config.adaptive_thinking_effort,
            throughput_mode=base_config.throughput_mode,
        )
        
        model_id = self.service.model_id
        supports_thinking_model = supports_thinking(model_id)
        
        if phase == "scout":
            # Fast exploration: balanced sampling for discovery
            config.temperature = 0.8  # Some variety for creative discovery
            config.top_p = 0.9
            # Scout uses fast model, typically no thinking
            config.enable_thinking = False
            config.thinking_budget = 0
        elif phase == "plan":
            # Careful reasoning: more deterministic + thinking for rigor
            config.temperature = 0.3  # Lower temp for consistent reasoning
            config.top_p = 0.9
            config.enable_thinking = model_config.enable_thinking and supports_thinking_model
            config.thinking_budget = model_config.thinking_budget if supports_thinking_model else 0
            # Use adaptive thinking effort "high" for planning
            if supports_adaptive_thinking(model_id):
                config.adaptive_thinking_effort = "high"
        elif phase == "build":
            # Precision execution: very deterministic for consistent, correct code
            config.temperature = 0.1  # Very low temp for precise, consistent output
            config.top_p = 0.95
            config.enable_thinking = model_config.enable_thinking and supports_thinking_model
            config.thinking_budget = model_config.thinking_budget if supports_thinking_model else 0
            # Use adaptive thinking effort "high" for complex implementation
            if supports_adaptive_thinking(model_id):
                config.adaptive_thinking_effort = "high"
        elif phase == "verify":
            # Deep analysis: deterministic + maximum thinking for thoroughness
            config.temperature = 0.1  # Very deterministic for consistent verification
            config.top_p = 0.95
            config.enable_thinking = model_config.enable_thinking and supports_thinking_model
            # Use maximum thinking budget for verification
            config.thinking_budget = min(model_config.thinking_budget * 1.2, 
                                       get_thinking_max_budget(model_id)) if supports_thinking_model else 0
            # Use adaptive thinking effort "max" for thorough verification
            if supports_adaptive_thinking(model_id):
                config.adaptive_thinking_effort = "max"
                
        return config

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

    def _parse_confidence_indicators(self, text: str) -> Dict[str, Any]:
        """
        Parse confidence indicators and uncertainty markers from model response.
        Helps with quality assurance and risk assessment.
        """
        import re
        
        confidence_info = {
            "confidence_level": None,
            "uncertainty_flags": [],
            "risk_indicators": [],
            "needs_review": False
        }
        
        # Look for explicit confidence markers
        confidence_patterns = [
            r"🟢.*[Hh]igh [Cc]onfidence",
            r"🟡.*[Mm]edium [Cc]onfidence", 
            r"🔴.*[Ll]ow [Cc]onfidence"
        ]
        
        for pattern in confidence_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                if "🟢" in pattern:
                    confidence_info["confidence_level"] = "high"
                elif "🟡" in pattern:
                    confidence_info["confidence_level"] = "medium"
                elif "🔴" in pattern:
                    confidence_info["confidence_level"] = "low"
                    confidence_info["needs_review"] = True
                break
        
        # Look for uncertainty flags
        uncertainty_phrases = [
            r"not (sure|certain|confident)",
            r"uncertain about",
            r"might need", r"should (probably|likely)",
            r"unsure (about|if|whether)",
            r"unclear (if|whether|how)",
            r"may need.*review",
            r"flag.*concern"
        ]
        
        for phrase in uncertainty_phrases:
            matches = re.findall(phrase, text, re.IGNORECASE)
            confidence_info["uncertainty_flags"].extend(matches)
        
        # Look for risk indicators
        risk_phrases = [
            r"could break", r"might fail", r"potential.*issue",
            r"breaking change", r"backward compatibility",
            r"security.*concern", r"edge case",
            r"needs.*testing", r"haven't.*tested"
        ]
        
        for phrase in risk_phrases:
            matches = re.findall(phrase, text, re.IGNORECASE)
            confidence_info["risk_indicators"].extend(matches)
        
        # Determine overall review need
        if (len(confidence_info["uncertainty_flags"]) > 2 or 
            len(confidence_info["risk_indicators"]) > 1 or
            confidence_info["confidence_level"] == "low"):
            confidence_info["needs_review"] = True
        
        return confidence_info

    def _compute_file_hash(self, abs_path: str) -> Optional[str]:
        """Compute hash of file for caching purposes"""
        try:
            with open(abs_path, 'rb') as f:
                return hashlib.sha256(f.read()).hexdigest()[:16]  # Short hash for efficiency
        except Exception as e:
            logger.debug(f"Failed to hash {abs_path}: {e}")
            return None

    def _get_cached_verification_result(self, abs_path: str) -> Optional[Dict[str, Any]]:
        """Get cached verification result if file unchanged since last verification"""
        current_hash = self._compute_file_hash(abs_path)
        if not current_hash:
            return None
            
        last_verified_hash = self._last_verification_hashes.get(abs_path)
        if current_hash == last_verified_hash and current_hash in self._verification_cache:
            cached_result = self._verification_cache[current_hash]
            logger.debug(f"Using cached verification result for {abs_path}")
            return cached_result
            
        return None

    def _cache_verification_result(self, abs_path: str, result: Dict[str, Any]) -> None:
        """Cache verification result for future use"""
        file_hash = self._compute_file_hash(abs_path)
        if file_hash:
            self._verification_cache[file_hash] = result
            self._last_verification_hashes[abs_path] = file_hash
            
            # Keep cache size reasonable
            if len(self._verification_cache) > 1000:
                # Remove oldest entries (simple FIFO)
                oldest_keys = list(self._verification_cache.keys())[:100]
                for key in oldest_keys:
                    del self._verification_cache[key]

    def _get_incremental_verification_plan(self, modified_abs: List[str]) -> Dict[str, Any]:
        """
        Create smart verification plan based on caches and dependencies.
        Inspired by incremental build systems like Bazel and Nx.
        """
        plan = {
            "files_to_verify": [],
            "cached_results": {},
            "verification_strategy": "full"  # full, incremental, minimal
        }
        
        files_needing_verification = []
        cached_count = 0
        
        # Check what can be cached
        for abs_path in modified_abs:
            cached_result = self._get_cached_verification_result(abs_path)
            if cached_result and cached_result.get("success", False):
                plan["cached_results"][abs_path] = cached_result
                cached_count += 1
            else:
                files_needing_verification.append(abs_path)
        
        # Determine verification strategy
        if cached_count == len(modified_abs):
            plan["verification_strategy"] = "minimal"  # Everything cached
        elif cached_count > len(modified_abs) * 0.5:
            plan["verification_strategy"] = "incremental"  # Mix of cached and new
        else:
            plan["verification_strategy"] = "full"  # Most files need verification
            
        plan["files_to_verify"] = files_needing_verification
        
        return plan

    def _handle_uncertain_response(self, response_text: str, confidence_info: Dict[str, Any]) -> str:
        """
        Generate follow-up guidance when the model expresses uncertainty.
        Helps improve quality by encouraging deeper analysis.
        """
        if not confidence_info["needs_review"]:
            return response_text
        
        uncertainty_guidance = []
        
        if confidence_info["confidence_level"] == "low":
            uncertainty_guidance.append(
                "⚠️  **Low Confidence Detected**: Please think more deeply about this approach. "
                "Consider alternative solutions or seek validation for uncertain aspects."
            )
        
        if confidence_info["uncertainty_flags"]:
            uncertainty_guidance.append(
                f"🤔 **Uncertainty Flags Found**: {len(confidence_info['uncertainty_flags'])} uncertain aspects detected. "
                "Please elaborate on what you're unsure about and how to mitigate risks."
            )
        
        if confidence_info["risk_indicators"]:
            uncertainty_guidance.append(
                f"⚠️  **Risk Indicators Found**: {len(confidence_info['risk_indicators'])} potential risks identified. "
                "Please provide specific mitigation strategies for each risk."
            )
        
        if uncertainty_guidance:
            guidance_text = "\n\n---\n**CONFIDENCE ASSESSMENT**:\n" + "\n".join(uncertainty_guidance)
            guidance_text += "\n\nPlease address these concerns before proceeding to ensure high-quality implementation."
            return response_text + guidance_text
        
        return response_text

    def _generate_contextual_guidance(self, phase: str, context: Dict[str, Any]) -> str:
        """
        Generate adaptive, contextual guidance based on current phase and context.
        Inspired by GitHub Copilot, Cursor, and modern AI coding assistants.
        """
        guidance_parts = []
        
        # Phase-specific guidance
        if phase == "build":
            if context.get("complexity_high", False):
                guidance_parts.append(
                    "🧠 **High Complexity Detected**: Consider breaking this into smaller, "
                    "testable components. Use thinking time to plan the approach carefully."
                )
            
            if context.get("verification_failures", 0) > 2:
                guidance_parts.append(
                    "⚠️ **Multiple Verification Failures**: Take a step back. Read error "
                    "messages carefully and fix systematically rather than making multiple changes."
                )
                
            if context.get("files_modified", 0) > 5:
                guidance_parts.append(
                    "📁 **Large Change Set**: Consider creating a checkpoint before proceeding. "
                    "Verify changes incrementally to isolate any issues."
                )
        
        elif phase == "plan":
            if context.get("unclear_requirements", False):
                guidance_parts.append(
                    "❓ **Ambiguous Requirements**: Ask clarifying questions before implementing. "
                    "It's better to get clarity now than to build the wrong thing."
                )
                
            if context.get("existing_code_unknown", False):
                guidance_parts.append(
                    "🔍 **Unknown Codebase**: Read key files first to understand patterns, "
                    "conventions, and existing utilities you can reuse."
                )
                
        elif phase == "verify":
            if context.get("test_coverage_low", False):
                guidance_parts.append(
                    "🧪 **Low Test Coverage**: Consider adding basic tests for critical paths "
                    "before considering this feature complete."
                )
        
        # Adaptive guidance based on historical patterns
        failure_patterns = self._failure_pattern_cache or []
        if len(failure_patterns) > 0:
            recent_failures = [p for p in failure_patterns if p.get("timestamp", 0) > time.time() - 3600]
            if recent_failures:
                common_patterns = {}
                for failure in recent_failures:
                    pattern = failure.get("pattern", "")
                    common_patterns[pattern] = common_patterns.get(pattern, 0) + 1
                
                most_common = max(common_patterns, key=common_patterns.get) if common_patterns else None
                if most_common and common_patterns[most_common] >= 2:
                    guidance_parts.append(
                        f"🔄 **Learned Pattern**: Recent issues with '{most_common}' - "
                        "double-check this area carefully."
                    )
        
        # Context-aware suggestions
        if context.get("working_late", False):
            guidance_parts.append(
                "🌙 **Late Hour Detected**: Take extra care with verification. "
                "Consider smaller changes and thorough testing when tired."
            )
            
        if context.get("large_diff", False):
            guidance_parts.append(
                "📊 **Large Diff**: Review changes section by section. "
                "Consider if this should be broken into multiple commits."
            )
        
        if guidance_parts:
            return "\n\n💡 **ADAPTIVE GUIDANCE**:\n" + "\n".join(guidance_parts) + "\n"
        else:
            return ""

    def _assess_context_for_guidance(self, modified_abs: List[str]) -> Dict[str, Any]:
        """Assess current context to determine what guidance to provide"""
        context = {}
        
        # Analyze change complexity
        total_lines_changed = 0
        files_modified = len(modified_abs)
        
        for abs_path in modified_abs:
            try:
                with open(abs_path, 'r', encoding='utf-8') as f:
                    lines = len(f.readlines())
                    total_lines_changed += lines
                    if lines > 200:
                        context["complexity_high"] = True
            except:
                pass
        
        context["files_modified"] = files_modified
        context["large_diff"] = total_lines_changed > 500
        
        # Check time of day (simple heuristic)
        current_hour = time.localtime().tm_hour
        context["working_late"] = current_hour < 6 or current_hour > 22
        
        # Check recent verification failures
        context["verification_failures"] = len([
            p for p in (self._failure_pattern_cache or []) 
            if p.get("timestamp", 0) > time.time() - 1800  # Last 30 minutes
        ])
        
        return context

    async def _handle_verification_failure_with_recovery(
        self, 
        failures: List[str], 
        modified_abs: List[str],
        on_event: Callable[[AgentEvent], Awaitable[None]]
    ) -> Dict[str, Any]:
        """
        Intelligent error recovery inspired by resilient systems.
        Attempts multiple recovery strategies based on failure patterns.
        """
        recovery_result = {
            "recovered": False,
            "recovery_strategy": None,
            "remaining_failures": failures.copy(),
            "recovery_actions": []
        }
        
        await on_event(AgentEvent(
            type="error_recovery",
            content=f"🔄 **Error Recovery Initiated** - Analyzing {len(failures)} failures...",
            data={"failure_count": len(failures)}
        ))
        
        # Strategy 1: Syntax Error Auto-Fix
        syntax_failures = [f for f in failures if any(
            term in f.lower() for term in ["syntax error", "invalid syntax", "indentation error"]
        )]
        
        if syntax_failures:
            recovery_result["recovery_strategy"] = "syntax_auto_fix"
            for failure in syntax_failures:
                # Extract filename from failure message
                for abs_path in modified_abs:
                    rel_path = os.path.relpath(abs_path, self.working_directory)
                    if rel_path in failure:
                        success = await self._attempt_syntax_fix(abs_path, on_event)
                        if success:
                            recovery_result["remaining_failures"].remove(failure)
                            recovery_result["recovery_actions"].append(f"Auto-fixed syntax in {rel_path}")
                        break
        
        # Strategy 2: Import Error Resolution
        import_failures = [f for f in failures if "import" in f.lower() or "module" in f.lower()]
        if import_failures:
            if not recovery_result["recovery_strategy"]:
                recovery_result["recovery_strategy"] = "import_resolution"
            
            for failure in import_failures:
                for abs_path in modified_abs:
                    rel_path = os.path.relpath(abs_path, self.working_directory)
                    if rel_path in failure:
                        success = await self._attempt_import_fix(abs_path, failure, on_event)
                        if success and failure in recovery_result["remaining_failures"]:
                            recovery_result["remaining_failures"].remove(failure)
                            recovery_result["recovery_actions"].append(f"Resolved imports in {rel_path}")
                        break
        
        # Strategy 3: Test Failure Analysis and Guided Recovery
        test_failures = [f for f in failures if "test" in f.lower() or "assert" in f.lower()]
        if test_failures:
            if not recovery_result["recovery_strategy"]:
                recovery_result["recovery_strategy"] = "test_guidance"
            
            await self._provide_test_failure_guidance(test_failures, on_event)
            recovery_result["recovery_actions"].append("Provided test failure analysis")
        
        # Determine overall recovery success
        recovery_result["recovered"] = len(recovery_result["remaining_failures"]) < len(failures)
        
        if recovery_result["recovered"]:
            await on_event(AgentEvent(
                type="error_recovery_success",
                content=f"✅ **Recovery Successful** - {len(recovery_result['recovery_actions'])} fixes applied",
                data=recovery_result
            ))
        else:
            await on_event(AgentEvent(
                type="error_recovery_partial",
                content=f"⚠️ **Partial Recovery** - {len(failures) - len(recovery_result['remaining_failures'])} issues resolved",
                data=recovery_result
            ))
        
        return recovery_result

    async def _attempt_syntax_fix(self, abs_path: str, on_event: Callable[[AgentEvent], Awaitable[None]]) -> bool:
        """Attempt basic syntax error fixes"""
        try:
            with open(abs_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            original_content = content
            fixes_applied = []
            
            # Common syntax fixes
            # Fix missing colons
            lines = content.split('\n')
            for i, line in enumerate(lines):
                stripped = line.strip()
                if (stripped.startswith(('if ', 'elif ', 'else', 'for ', 'while ', 'def ', 'class ', 'try', 'except', 'finally', 'with ')) 
                    and not stripped.endswith(':') and not stripped.endswith(':\\')):
                    lines[i] = line + ':'
                    fixes_applied.append(f"Added missing colon at line {i+1}")
            
            if fixes_applied:
                fixed_content = '\n'.join(lines)
                with open(abs_path, 'w', encoding='utf-8') as f:
                    f.write(fixed_content)
                
                # Test if fix worked
                try:
                    compile(fixed_content, abs_path, 'exec')
                    await on_event(AgentEvent(
                        type="auto_fix_success",
                        content=f"🔧 **Auto-Fixed Syntax**: {os.path.relpath(abs_path, self.working_directory)} - {', '.join(fixes_applied)}",
                        data={"fixes": fixes_applied, "file": abs_path}
                    ))
                    return True
                except SyntaxError:
                    # Revert if fix didn't work
                    with open(abs_path, 'w', encoding='utf-8') as f:
                        f.write(original_content)
            
        except Exception as e:
            logger.debug(f"Syntax fix failed for {abs_path}: {e}")
        
        return False

    async def _attempt_import_fix(self, abs_path: str, failure: str, on_event: Callable[[AgentEvent], Awaitable[None]]) -> bool:
        """Attempt to fix common import errors by adding missing stdlib/same-dir imports."""
        if not abs_path.endswith(".py"):
            return False
        try:
            rel_path = os.path.relpath(abs_path, self.working_directory)
            content = self.backend.read_file(rel_path)
            if not content:
                return False
            tree = ast.parse(content)
            defined: set = set()
            used: set = set()

            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in (node.names if hasattr(node, "names") else []):
                        name = alias.asname or alias.name
                        defined.add(name.split(".", 1)[0])
                elif isinstance(node, ast.FunctionDef):
                    defined.add(node.name)
                    for a in node.args.args:
                        defined.add(a.arg)
                elif isinstance(node, ast.ClassDef):
                    defined.add(node.name)
                elif isinstance(node, ast.Name):
                    if isinstance(node.ctx, ast.Load):
                        used.add(node.id)
                elif isinstance(node, ast.Attribute):
                    if isinstance(node.ctx, ast.Load) and isinstance(node.value, ast.Name):
                        used.add(node.value.id)

            missing = used - defined - {"__name__", "__file__", "self", "True", "False", "None"}
            if not missing:
                return False

            # Common stdlib and third-party modules we can safely add
            stdlib_known = {
                "os", "re", "sys", "json", "time", "pathlib", "logging", "asyncio",
                "dataclasses", "typing", "collections", "functools", "itertools",
                "subprocess", "shutil", "tempfile", "io", "codecs", "hashlib",
                "uuid", "random", "math", "decimal", "datetime", "argparse",
            }
            to_add = [n for n in sorted(missing) if n in stdlib_known][:5]
            if not to_add:
                await on_event(AgentEvent(
                    type="import_analysis",
                    content=f"🔍 **Import Analysis**: {os.path.relpath(abs_path, self.working_directory)} - Could not auto-add imports for {list(missing)[:5]}",
                    data={"file": abs_path, "failure": failure, "missing": list(missing)[:10]},
                ))
                return False

            lines = content.split("\n")
            insert_idx = 0
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith(("import ", "from ")) or (stripped and not stripped.startswith("#")):
                    insert_idx = i
                    if stripped.startswith(("import ", "from ")):
                        while insert_idx + 1 < len(lines) and lines[insert_idx + 1].strip().startswith(("import ", "from ")):
                            insert_idx += 1
                        insert_idx += 1
                    break

            new_imports = "\n".join(f"import {m}" for m in to_add)
            new_content = "\n".join(lines[:insert_idx]) + "\n" + new_imports + "\n" + "\n".join(lines[insert_idx:])
            try:
                ast.parse(new_content)
            except SyntaxError:
                return False

            self.backend.write_file(rel_path, new_content)
            await on_event(AgentEvent(
                type="import_analysis",
                content=f"🔧 **Auto-added imports**: {os.path.relpath(abs_path, self.working_directory)} - added {', '.join(to_add)}",
                data={"file": abs_path, "added": to_add},
            ))
            return True
        except Exception as e:
            logger.debug(f"Import fix failed for {abs_path}: {e}")
            await on_event(AgentEvent(
                type="import_analysis",
                content=f"🔍 **Import Analysis**: {os.path.relpath(abs_path, self.working_directory)} - Manual review recommended",
                data={"file": abs_path, "failure": failure},
            ))
            return False

    async def _provide_test_failure_guidance(self, test_failures: List[str], on_event: Callable[[AgentEvent], Awaitable[None]]):
        """Provide intelligent guidance for test failures"""
        guidance_parts = []
        
        for failure in test_failures:
            if "assertion" in failure.lower():
                guidance_parts.append("🧪 **Assertion Failure**: Check expected vs actual values")
            elif "timeout" in failure.lower():
                guidance_parts.append("⏱️ **Timeout**: Consider async issues or performance problems")
            elif "fixture" in failure.lower():
                guidance_parts.append("🔧 **Fixture Issue**: Verify test setup and dependencies")
            elif "import" in failure.lower():
                guidance_parts.append("📦 **Import Issue**: Check module paths and dependencies")
        
        if guidance_parts:
            guidance_text = "\n".join(f"- {part}" for part in guidance_parts)
            await on_event(AgentEvent(
                type="test_failure_guidance",
                content=f"🎯 **Test Failure Analysis**:\n{guidance_text}",
                data={"guidance": guidance_parts}
            ))

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

        if tool_name == "Read":
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
        if tool_name == "Bash":
            if len(lines) > 30:
                return "\n".join(
                    lines[:12]
                    + [f"  ... ({len(lines) - 17} lines omitted) ..."]
                    + lines[-5:]
                )

        # For directory listings: keep entries
        if tool_name in ("list_directory", "Glob"):
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
                    "COMPACTION CONTRACT: This summary must allow the agent to continue the task without re-reading everything.\n"
                    "Include exactly:\n"
                    "1. **Task**: What the user asked for (exact goal).\n"
                    "2. **Files touched**: Paths read, edited, or created (with one-line reason each).\n"
                    "3. **Decisions**: Key design/implementation choices and why.\n"
                    "4. **Current state**: What is done, what remains, any errors or blockers.\n"
                    "5. **Next steps**: What the agent should do next (concrete).\n"
                    "Be concise but reconstruction-grade — the agent will continue from this summary. Use bullet points. Max 500 words."
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
                    if name == "Read":
                        files_read.append(inp.get("path", "?"))
                    elif name in ("Write", "Edit", "symbol_edit"):
                        files_edited.append(inp.get("path", "?"))
                    elif name == "Bash":
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

        Tier 1 (>60% full): Gentle — compress bulky tool results/text first.
        Tier 2 (>78% full): Aggressive — summarize old messages and trim old thinking.
        Tier 3 (>90% full): Emergency — drop to summary + recent messages only.

        Because tool results are already capped at ingestion (_cap_tool_results),
        Tier 1 is usually sufficient. Tiers 2-3 are safety nets.
        """
        context_window = get_context_window(self.service.model_id)
        # Reserve generous output headroom so the model NEVER runs out of space (Cursor/Claude-style)
        reserved_output = min(80_000, get_max_output_tokens(self.service.model_id) // 2)
        usable = max(1, context_window - reserved_output)
        # More aggressive thresholds - start trimming much earlier to NEVER hit walls
        tier1_limit = int(usable * 0.40)  # Start trimming at 40% (was 52%)
        tier2_limit = int(usable * 0.55)  # Aggressive at 55% (was 70%)
        tier3_limit = int(usable * 0.70)  # Emergency at 70% (was 85%)

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

                # Tier 1 keeps thinking blocks intact to preserve transparent reasoning.
                # Compress tool results first (cold files aggressively, hot files gently).
                if btype == "tool_result":
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

        # ── Tier 2: Aggressive — summarize old messages (>78%) ────────
        logger.info(f"Context tier 2: ~{current:,} tokens > {tier2_limit:,}. Summarizing.")

        # Before summarizing, trim old thinking blocks to placeholders.
        for i in range(max(0, len(self.history) - safe_tail)):
            msg = self.history[i]
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for j, block in enumerate(content):
                if isinstance(block, dict) and block.get("type") == "thinking":
                    content[j] = {"type": "thinking", "thinking": "..."}
                    if block.get("signature"):
                        content[j]["signature"] = block["signature"]

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


            missing = tool_use_ids - result_ids
            if missing:
                if next_msg.get("role") == "user":
                    if not isinstance(next_content, list):
                        next_content = []
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
                else:
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
        """Microcompaction: cap tool result content at ingestion so context stays manageable.
        Enterprise-grade: large outputs are head/tail + explicit instruction to use Read with offset/limit for full content."""
        cap = self._adaptive_result_cap()
        capped = []
        for result in tool_results:
            text = result.get("content", "")
            if isinstance(text, str) and len(text) > cap:
                lines = text.split("\n")
                if len(lines) > 50:
                    head_n = max(20, cap // 400)
                    tail_n = max(10, cap // 800)
                    head = "\n".join(lines[:head_n])
                    tail = "\n".join(lines[-tail_n:])
                    text = (
                        "[Large output — excerpt below. Use Read with offset/limit for full content.]\n\n"
                        + head
                        + f"\n\n... ({len(lines) - head_n - tail_n} lines omitted) ...\n\n"
                        + tail
                    )
                else:
                    text = text[:cap - 200] + "\n... (truncated; use Read with offset/limit for full content) ..."
                if len(text) > cap:
                    text = text[:cap] + "\n... (excerpt capped) ..."
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

        scout_system = _compose_system_prompt("scout", self.working_directory, SCOUT_TOOL_NAMES, language=self._detected_language)
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
                        None, lambda: execute_tool(tu.name, tu.input, self.working_directory, backend=self.backend, extra_context={"todos": self._todos})
                    )
                    return tu, r

                tool_results_raw = await asyncio.gather(
                    *[_exec_scout_tool(tu) for tu in result.tool_uses]
                )

                tool_results = []
                for tu, tr in tool_results_raw:
                    text = tr.output if tr.success else (tr.error or "Unknown error")
                    # Smart tool-aware compression for scout (Haiku context is limited)
                    if isinstance(text, str) and len(text) > 8000:
                        is_hot = hasattr(self, 'modified_files') and tu.input and tu.input.get("path") in (self.modified_files or set())
                        text = self._compress_tool_result(text, tu.name, is_hot)
                        # Final safety cap at 12K chars for scout
                        if len(text) > 12000:
                            lines = text.split("\n")
                            head_n = max(40, len(lines) // 3)
                            tail_n = 10
                            text = "\n".join(lines[:head_n]) + f"\n... ({len(lines) - head_n - tail_n} lines omitted) ...\n" + "\n".join(lines[-tail_n:])
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": text,
                        "is_error": not tr.success,
                    })

                    display_name = SCOUT_TOOL_DISPLAY_NAMES.get(tu.name, tu.name)
                    inp = tu.input or {}
                    if "todos" in inp and isinstance(inp["todos"], list):
                        n = len(inp["todos"])
                        detail = f"{n} item{'s' if n != 1 else ''}"
                    else:
                        detail = (
                            inp.get("path")
                            or inp.get("pattern")
                            or inp.get("query")
                            or inp.get("symbol")
                            or inp.get("focus_path")
                            or inp.get("command")
                            or inp.get("content")
                            or inp.get("regex")
                            or ""
                        )
                        if not detail:
                            detail = "project root" if tu.name == "project_tree" else "…"
                    await on_event(AgentEvent(
                        type="scout_progress",
                        content=f"{display_name}: {detail}",
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
        user_images: Optional[List[Dict[str, Any]]] = None,
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
        plan_system = _compose_system_prompt("plan", self.working_directory, SCOUT_TOOL_NAMES, language=self._detected_language)
        plan_user = task_for_plan
        if scout_context:
            plan_user = (
                f"<codebase_context>\n{scout_context}\n</codebase_context>\n\n"
                f"{plan_user}"
            )
        project_docs = self._load_project_docs()
        if project_docs:
            context_metadata = self._generate_context_metadata(project_docs)
            plan_user = f"<project_context>\n{context_metadata}\n\n{project_docs}\n</project_context>\n\n" + plan_user

        # Agentic loop with STREAMING + read-only tools so the user
        # sees thinking/text in real time during plan generation
        loop = asyncio.get_event_loop()
        plan_config = self._get_generation_config_for_phase("plan")

        plan_messages: List[Dict[str, Any]] = [
            {"role": "user", "content": self._compose_user_content(plan_user, user_images)}
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
                question_calls = [tu for tu in tool_uses if tu.get("name") == "AskUserQuestion"]
                other_calls = [tu for tu in tool_uses if tu.get("name") != "AskUserQuestion"]

                tool_results = []

                # Handle AskUserQuestion via callback (Cursor-style clarifying questions)
                for tu in question_calls:
                    inp = tu.get("input", {})
                    question = inp.get("question", "")
                    context = inp.get("context") or ""
                    options = inp.get("options")
                    if isinstance(options, list):
                        options = [str(o) for o in options]
                    else:
                        options = None
                    if request_question_answer and question:
                        try:
                            answer = await request_question_answer(question, context, tu["id"], options=options)
                            text_r = f"User answered: {answer}"
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tu["id"],
                                "content": text_r,
                                "is_error": False,
                            })
                            await on_event(AgentEvent(type="tool_result", content=text_r[:200], data={"id": tu["id"], "name": "AskUserQuestion", "success": True}))
                        except Exception as e:
                            text_r = f"Clarification failed or skipped: {e}"
                            tool_results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": text_r, "is_error": True})
                            await on_event(AgentEvent(type="tool_result", content=text_r, data={"id": tu["id"], "name": "AskUserQuestion", "success": False}))
                    else:
                        text_r = "Clarification not available; proceed with your best assumption."
                        tool_results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": text_r, "is_error": False})
                        await on_event(AgentEvent(type="tool_result", content=text_r, data={"id": tu["id"], "name": "AskUserQuestion", "success": True}))

                # Execute read-only tools in parallel
                async def _exec_plan_tool(tu):
                    r = await loop.run_in_executor(
                        None, lambda tu=tu: execute_tool(tu["name"], tu["input"], self.working_directory, backend=self.backend, extra_context={"todos": self._todos})
                    )
                    return tu, r

                if other_calls:
                    tool_results_raw = await asyncio.gather(
                        *[_exec_plan_tool(tu) for tu in other_calls]
                    )
                    for tu, tr in tool_results_raw:
                        text_r = tr.output if tr.success else (tr.error or "Unknown error")
                        if isinstance(text_r, str) and len(text_r) > 10000:
                            tool_name = tu.get("name", "")
                            is_hot = hasattr(self, 'modified_files') and tu.get("input", {}).get("path") in (self.modified_files or set())
                            text_r = self._compress_tool_result(text_r, tool_name, is_hot)
                            if len(text_r) > 15000:
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
            self._current_plan_decomposition = self._decompose_plan_steps(steps)

            # Write plan to a markdown file on disk (persisted for "Open in Editor" on reload)
            plan_file_path = self._write_plan_file(task, plan_text)
            self._plan_file_path = plan_file_path
            self._plan_text = plan_text or ""

            # Emit plan steps + full plan text + file path
            await on_event(AgentEvent(
                type="phase_plan",
                content="\n".join(steps),
                data={
                    "steps": steps,
                    "plan_text": plan_text,
                    "plan_file": plan_file_path,
                    "decomposition": self._current_plan_decomposition,
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

    @staticmethod
    def _compose_user_content(text: str, user_images: Optional[List[Dict[str, Any]]] = None) -> Any:
        """Return either plain text or multimodal blocks with image attachments."""
        if not user_images:
            return text
        blocks: List[Dict[str, Any]] = [{"type": "text", "text": text}]
        for img in user_images:
            if not isinstance(img, dict) or img.get("type") != "image":
                continue
            src = img.get("source", {})
            if not isinstance(src, dict):
                continue
            if src.get("type") != "base64":
                continue
            media_type = src.get("media_type")
            data = src.get("data")
            if not media_type or not data:
                continue
            blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": data,
                },
            })
        return blocks if len(blocks) > 1 else text

    async def run_build(
        self,
        task: str,
        plan_steps: List[str],
        on_event: Callable[[AgentEvent], Awaitable[None]],
        request_approval: Callable[[str, str, Dict], Awaitable[bool]],
        config: Optional[GenerationConfig] = None,
        user_images: Optional[List[Dict[str, Any]]] = None,
        request_question_answer: Optional[Callable[..., Awaitable[str]]] = None,
    ):
        """
        Execute a previously approved plan. This is the build phase.
        The plan is injected into the conversation so the model follows it.
        """
        self._cancelled = False
        # Preserve existing snapshots so diff/revert stay cumulative across prompts
        if not self._file_snapshots:
            self._file_snapshots = {}  # fresh snapshot tracking per build
        self._deterministic_verification_done = False
        self._verification_gate_attempts = 0

        await on_event(AgentEvent(type="phase_start", content="build"))

        # Switch to the build-specific system prompt for plan execution
        saved_prompt = self.system_prompt
        self.system_prompt = _format_build_system_prompt(self.working_directory, language=self._detected_language)

        # Build the user message with the approved plan and scout context
        plan_block = "\n".join(plan_steps)
        decomposition = self._decompose_plan_steps(plan_steps)
        self._current_plan_decomposition = decomposition
        worker_insights = await self._run_parallel_manager_workers(task, decomposition)
        decomp_lines = []
        for batch in decomposition:
            step_ids = [str(s.get("index")) for s in batch.get("steps", [])]
            targets = ", ".join(batch.get("targets", [])[:5]) if batch.get("targets") else "n/a"
            decomp_lines.append(
                f"- Batch {batch.get('batch')} [{batch.get('type')}]: steps {', '.join(step_ids)} | targets: {targets}"
            )
        decomp_text = "\n".join(decomp_lines) if decomp_lines else "- Single batch"
        parts = []
        if self._scout_context:
            parts.append(f"<codebase_context>\n{self._scout_context}\n</codebase_context>")
        parts.append(f"<approved_plan>\n{plan_block}\n</approved_plan>")
        parts.append(f"<plan_decomposition>\n{decomp_text}\n</plan_decomposition>")
        if worker_insights:
            parts.append(f"<manager_worker_insights>\n{worker_insights}\n</manager_worker_insights>")
        parts.append(task)
        parts.append(
            "Execute this plan step by step.\n\n"
            "Before touching files, call TodoWrite with a full list of plan items (status pending), then set the first to in_progress. You can call TodoRead anytime to see the current task list.\n"
            "Work through them in order; set each to completed and the next to in_progress as you go.\n\n"
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
        user_content = self._compose_user_content(user_content, user_images)

        # Human-in-the-loop review gate (optional, policy mode)
        if app_config.human_review_mode:
            review_desc = (
                "Human review required before build execution.\n\n"
                f"Task: {task[:300]}\n\n"
                "Plan decomposition:\n"
                f"{decomp_text}\n\n"
                "Approve to proceed with implementation."
            )
            approved = await request_approval(
                "plan_review",
                review_desc,
                {"task": task, "plan_steps": plan_steps, "decomposition": decomposition},
            )
            if not approved:
                await on_event(AgentEvent(
                    type="cancelled",
                    content="Build cancelled: plan review was not approved.",
                ))
                self.system_prompt = saved_prompt
                await on_event(AgentEvent(type="phase_end", content="build"))
                return

        # Add to history
        self.history.append({"role": "user", "content": user_content})

        # Run the main agent loop with build-optimized configuration
        build_config = self._get_generation_config_for_phase("build", config)
        await self._agent_loop(on_event, request_approval, build_config, request_question_answer=request_question_answer)

        # Post-build verification pass with verification-optimized configuration
        verify_config = self._get_generation_config_for_phase("verify", config)
        await self._run_post_build_verification(on_event, request_approval, verify_config, request_question_answer=request_question_answer)

        # Restore the general-purpose system prompt
        self.system_prompt = saved_prompt

        await on_event(AgentEvent(type="phase_end", content="build"))

    async def _run_post_build_verification(
        self,
        on_event: Callable[[AgentEvent], Awaitable[None]],
        request_approval: Callable[[str, str, Dict], Awaitable[bool]],
        config: Optional[GenerationConfig] = None,
        request_question_answer: Optional[Callable[..., Awaitable[str]]] = None,
    ):
        """Run a final verification pass after the build loop completes.
        Injects a verification reminder and runs one more loop iteration
        so the model can re-read files, run lints, and fix issues."""
        if self._cancelled:
            return
        if self._deterministic_verification_done:
            return
        # Only verify if there are modified files that still exist to check
        if not self._file_snapshots:
            return

        modified = [f for f in self._file_snapshots.keys() if os.path.isfile(f)]
        if not modified:
            # All modified files were deleted (intentionally) — nothing to verify
            self._deterministic_verification_done = True
            return

        # Skip heavy verification for trivial changes (1-2 small files)
        def _snap_len(v):
            if isinstance(v, str): return len(v)
            if isinstance(v, dict) and "content" in v: return len(v["content"])
            return 0
        total_snapshot_size = sum(_snap_len(self._file_snapshots.get(f)) for f in modified)
        is_trivial = len(modified) <= 2 and total_snapshot_size < 500

        files_str = ", ".join(os.path.basename(f) for f in modified[:10])
        if len(modified) > 10:
            files_str += f" (+{len(modified) - 10} more)"

        if is_trivial:
            verify_msg = (
                f"Quick verification — Modified files: {files_str}\n\n"
                "Run lint_file on changed files. If clean, confirm the task is complete. "
                "Do NOT re-implement or re-do anything — the task is done. "
                "Just verify and report briefly."
            )
            max_extra_iters = 3
        else:
            # ── Test impact selection for modified files ──
            test_files_found = self._select_impacted_tests(modified)
            test_section = ""
            if test_files_found:
                test_section = (
                    f"\n\nImpacted tests selected:\n"
                    + "\n".join(f"  - {tf}" for tf in test_files_found[:10])
                    + "\nRun these impacted tests first, then run broader suite if needed."
                )

            verify_msg = (
                f"Verification pass — Modified files: {files_str}\n\n"
                "1. Re-read each modified file and check for typos, missing imports, logic errors\n"
                "2. Run lint_file on each changed file and fix any errors\n"
                f"3. Run relevant tests if applicable{test_section}\n"
                "4. Briefly confirm the task is complete or flag concerns\n\n"
                "IMPORTANT: Do NOT re-implement anything. The task is done. "
                "This is only a lint-and-review pass. If everything looks good, just say so and stop."
            )
            max_extra_iters = 8

        self.history.append({"role": "user", "content": verify_msg})
        self._deterministic_verification_done = True

        saved_max = self.max_iterations
        self.max_iterations = saved_max + max_extra_iters
        await self._agent_loop(on_event, request_approval, config, request_question_answer=request_question_answer)
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
        user_images: Optional[List[Dict[str, Any]]] = None,
        preserve_snapshots: bool = False,
        request_question_answer: Optional[Callable[..., Awaitable[str]]] = None,
    ):
        """
        Run the agent on a task. If plan phase is enabled, this is called
        by the TUI which handles the plan->approve->build flow.
        When plan phase is disabled, this runs everything directly.

        enable_scout: Whether to run the scout phase. Set by intent classification.
        preserve_snapshots: If True, keep existing _file_snapshots so revert/diff stay cumulative.
        """
        self._cancelled = False
        if not preserve_snapshots:
            self._file_snapshots = {}  # fresh snapshot tracking per run
        self._deterministic_verification_done = False
        self._verification_gate_attempts = 0

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
        self.history.append({"role": "user", "content": self._compose_user_content(user_content, user_images)})

        await self._agent_loop(on_event, request_approval, config, request_question_answer=request_question_answer)

    def _extract_assistant_text(self, assistant_content: List[Dict[str, Any]]) -> str:
        """Concatenate assistant text blocks for lightweight output validation."""
        parts: List[str] = []
        for block in assistant_content:
            if isinstance(block, dict) and block.get("type") == "text":
                txt = block.get("text", "")
                if isinstance(txt, str) and txt.strip():
                    parts.append(txt.strip())
        return "\n\n".join(parts)

    def _last_user_message_has_tool_results(self) -> bool:
        """True if the latest user message is a tool_result payload."""
        if not self.history:
            return False
        last = self.history[-1]
        if last.get("role") != "user":
            return False
        content = last.get("content", [])
        if not isinstance(content, list):
            return False
        return any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)

    def _has_structured_reasoning_trace(self, text: str) -> bool:
        """Require visible reasoning-trace headings in final user-visible explanations."""
        if not text or len(text.strip()) < 40:
            return False
        patterns = [
            r"what\s+i\s+learned",
            r"why\s+it\s+matters",
            r"\bdecision\b",
            r"next\s+actions?",
            r"verification\s+status",
        ]
        hits = sum(1 for pat in patterns if re.search(pat, text, flags=re.IGNORECASE))
        return hits >= 4

    def _verification_profiles(self, modified_abs: List[str]) -> Dict[str, Any]:
        """Detect language/framework verification profiles from modified files and repo markers."""
        exts = set()
        rel_files: List[str] = []
        for p in modified_abs:
            rel = os.path.relpath(p, self.working_directory)
            rel_files.append(rel)
            _, ext = os.path.splitext(rel.lower())
            if ext:
                exts.add(ext)
        profile = {
            "python": any(e in exts for e in (".py", ".pyi")),
            "javascript": any(e in exts for e in (".js", ".jsx", ".mjs")),
            "typescript": any(e in exts for e in (".ts", ".tsx")),
            "go": ".go" in exts,
            "rust": ".rs" in exts,
            "rel_files": rel_files,
        }
        return profile

    def _verification_orchestrator_commands(self, modified_abs: List[str]) -> List[str]:
        """Build language/framework-aware verification commands."""
        prof = self._verification_profiles(modified_abs)
        cmds: List[str] = []
        rel_files = prof["rel_files"][:50]

        # Python stack
        if prof["python"]:
            py_files = [shlex.quote(f) for f in rel_files if f.endswith((".py", ".pyi"))][:40]
            if py_files:
                cmds.append("python -m py_compile " + " ".join(py_files))
            if self.backend.file_exists("pyproject.toml") or self.backend.file_exists("ruff.toml") or self.backend.file_exists(".ruff.toml"):
                cmds.append("ruff check " + " ".join(py_files or ["."]))
            elif self.backend.file_exists(".flake8") or self.backend.file_exists("setup.cfg"):
                cmds.append("flake8 " + " ".join(py_files or ["."]))

        # TS/JS stack
        if prof["typescript"] and self.backend.file_exists("tsconfig.json"):
            cmds.append("npx tsc --noEmit")
        if (prof["javascript"] or prof["typescript"]) and (
            self.backend.file_exists(".eslintrc.js")
            or self.backend.file_exists(".eslintrc.json")
            or self.backend.file_exists("eslint.config.js")
        ):
            js_files = [shlex.quote(f) for f in rel_files if f.endswith((".js", ".jsx", ".mjs", ".ts", ".tsx"))][:80]
            if js_files:
                cmds.append("npx eslint " + " ".join(js_files))

        # Go/Rust
        if prof["go"]:
            cmds.append("go test ./...")
        if prof["rust"] and self.backend.file_exists("Cargo.toml"):
            cmds.append("cargo test -q")

        # De-dup while preserving order
        seen = set()
        dedup = []
        for c in cmds:
            if c not in seen:
                seen.add(c)
                dedup.append(c)
        return dedup[:8]

    async def _run_progressive_verification(
        self, 
        modified_abs: List[str], 
        on_event: Callable[[AgentEvent], Awaitable[None]]
    ) -> Dict[str, Any]:
        """
        Enhanced multi-stage verification pipeline inspired by modern DevOps practices.
        
        Stages:
        1. Static Analysis - Fast syntax, import, and style checks
        2. Semantic Validation - Logic patterns, code quality, security
        3. Dynamic Testing - Unit tests, integration tests with impact analysis
        4. Quality Assessment - Coverage, complexity, maintainability scores
        5. Confidence Scoring - Risk assessment and adaptive thresholds
        """
        verification_result = {
            "success": True,
            "confidence_score": 0.0,
            "progressive_enabled": True,
            "stage_results": {},
            "recommendations": [],
            "failures": []
        }
        
        try:
            # Get incremental verification plan
            verification_plan = self._get_incremental_verification_plan(modified_abs)
            
            await on_event(AgentEvent(
                type="verification_plan",
                content=f"📋 **Verification Plan**: {verification_plan['verification_strategy'].title()} strategy - "
                       f"{len(verification_plan['files_to_verify'])} files to verify, "
                       f"{len(verification_plan['cached_results'])} cached",
                data=verification_plan
            ))
            
            # Use cached results for files that haven't changed
            for abs_path, cached_result in verification_plan["cached_results"].items():
                verification_result["stage_results"][f"cached_{abs_path}"] = cached_result
            
            # Only verify files that need it
            files_to_verify = verification_plan["files_to_verify"]
            if not files_to_verify:
                # Everything is cached and successful
                verification_result["success"] = True
                verification_result["confidence_score"] = 0.95  # High confidence for cached results
                verification_result["recommendations"].append("✅ All files passed cached verification")
                return verification_result
            
            # Stage 1: Static Analysis (Fast) - Only for files needing verification
            static_result = await self._run_static_analysis_stage(files_to_verify, on_event)
            verification_result["stage_results"]["static"] = static_result
            
            # Early exit if critical static failures
            if not static_result["success"] and static_result.get("critical", False):
                verification_result["success"] = False
                verification_result["failures"].extend(static_result.get("failures", []))
                return verification_result
            
            # Stage 2: Semantic Validation
            semantic_result = await self._run_semantic_validation_stage(modified_abs, on_event)
            verification_result["stage_results"]["semantic"] = semantic_result
            
            # Stage 3: Dynamic Testing (with impact analysis)
            testing_result = await self._run_testing_stage(modified_abs, on_event)
            verification_result["stage_results"]["testing"] = testing_result
            
            # Stage 4: Quality Assessment
            quality_result = await self._run_quality_assessment_stage(modified_abs, on_event)
            verification_result["stage_results"]["quality"] = quality_result
            
            # Stage 5: Confidence Scoring and Final Assessment
            confidence_result = self._calculate_verification_confidence(verification_result)
            verification_result.update(confidence_result)
            
            # Cache successful results for future use
            if verification_result["success"]:
                for abs_path in files_to_verify:
                    file_result = {
                        "success": True,
                        "timestamp": time.time(),
                        "confidence_score": verification_result["confidence_score"],
                        "stage": "progressive_verification"
                    }
                    self._cache_verification_result(abs_path, file_result)
            
            return verification_result
            
        except Exception as e:
            logger.warning(f"Progressive verification failed, falling back to legacy: {e}")
            verification_result["progressive_enabled"] = False
            return verification_result

    async def _run_static_analysis_stage(
        self, 
        modified_abs: List[str], 
        on_event: Callable[[AgentEvent], Awaitable[None]]
    ) -> Dict[str, Any]:
        """Stage 1: Fast static analysis - syntax, imports, basic linting"""
        loop = asyncio.get_event_loop()
        stage_result = {
            "success": True,
            "critical": False,
            "failures": [],
            "warnings": [],
            "files_checked": len(modified_abs)
        }
        
        await on_event(AgentEvent(
            type="verification_stage",
            content="🔍 **STAGE 1: Static Analysis** - Checking syntax, imports, and code style...",
            data={"stage": "static", "total_files": len(modified_abs)}
        ))
        
        for abs_path in modified_abs:
            rel_path = os.path.relpath(abs_path, self.working_directory)
            
            # Run enhanced linting with additional checks
            lint_result = await loop.run_in_executor(
                None,
                lambda rp=rel_path: execute_tool(
                    "lint_file",
                    {"path": rp},
                    self.working_directory,
                    backend=self.backend,
                    extra_context={"todos": self._todos},
                ),
            )
            
            if not lint_result.success:
                failure_msg = f"lint_file {rel_path}: {lint_result.output[:800]}"
                stage_result["failures"].append(failure_msg)
                
                # Check if this is a critical syntax error
                if any(term in lint_result.output.lower() for term in ["syntax error", "invalid syntax", "indentation error"]):
                    stage_result["critical"] = True
            
            await on_event(AgentEvent(
                type="tool_result",
                content=lint_result.output if lint_result.success else f"❌ {lint_result.output}",
                data={
                    "tool_name": "lint_file",
                    "tool_use_id": f"static-{rel_path}",
                    "success": lint_result.success,
                    "verification_stage": "static"
                }
            ))
        
        stage_result["success"] = len(stage_result["failures"]) == 0
        return stage_result

    async def _run_semantic_validation_stage(
        self,
        modified_abs: List[str],
        on_event: Callable[[AgentEvent], Awaitable[None]],
    ) -> Dict[str, Any]:
        """Stage 2: Semantic validation - security patterns and code-quality checks."""
        stage_result = {
            "success": True,
            "failures": [],
            "warnings": [],
            "files_checked": len(modified_abs),
        }
        await on_event(AgentEvent(
            type="verification_stage",
            content="🔎 **STAGE 2: Semantic Validation** - Checking logic and security patterns...",
            data={"stage": "semantic", "total_files": len(modified_abs)},
        ))
        loop = asyncio.get_event_loop()
        py_files = [p for p in modified_abs if str(p).lower().endswith(".py")]
        for abs_path in py_files:
            rel_path = os.path.relpath(abs_path, self.working_directory)
            try:
                content = self.backend.read_file(rel_path)
            except Exception:
                continue
            # Pattern-based security / quality checks (no extra deps)
            patterns = [
                (r"\beval\s*\(", "eval() use - security risk"),
                (r"\bexec\s*\(", "exec() use - security risk"),
                (r"subprocess\.(call|run|Popen)\s*\([^)]*shell\s*=\s*True", "subprocess with shell=True - prefer list args"),
                (r"os\.system\s*\(", "os.system() - prefer subprocess with list args"),
                (r"pickle\.loads?\s*\(", "pickle.loads - avoid unpickling untrusted data"),
                (r"__import__\s*\(", "__import__() - prefer import statement"),
            ]
            for pat, msg in patterns:
                if re.search(pat, content):
                    stage_result["warnings"].append(f"{rel_path}: {msg}")
            # Optional: run bandit if available
            try:
                bandit_result = await loop.run_in_executor(
                    None,
                    lambda rp=rel_path: execute_tool(
                        "Bash",
                        {"command": f"bandit -q -ll {shlex.quote(rp)} 2>/dev/null || true"},
                        self.working_directory,
                        backend=self.backend,
                        extra_context={"todos": self._todos},
                    ),
                )
                if not bandit_result.success or (bandit_result.output and "Issue" in bandit_result.output):
                    out = (bandit_result.output or "")[:500]
                    if out:
                        stage_result["warnings"].append(f"{rel_path}: bandit findings - {out.strip()[:200]}")
            except Exception:
                pass
        stage_result["success"] = len(stage_result["failures"]) == 0
        return stage_result

    async def _run_testing_stage(
        self, 
        modified_abs: List[str], 
        on_event: Callable[[AgentEvent], Awaitable[None]]
    ) -> Dict[str, Any]:
        """Stage 3: Dynamic testing with impact analysis using existing test discovery"""
        stage_result = {
            "success": True,
            "failures": [],
            "tests_run": 0,
            "coverage_impact": None
        }
        
        await on_event(AgentEvent(
            type="verification_stage",
            content="🧪 **STAGE 3: Dynamic Testing** - Running impacted tests...",
            data={"stage": "testing", "total_files": len(modified_abs)}
        ))
        
        try:
            # Use existing orchestrator commands for testing
            test_cmds = self._verification_orchestrator_commands(modified_abs)
            test_cmds = [cmd for cmd in test_cmds if "pytest" in cmd or "test" in cmd]
            
            if test_cmds:
                loop = asyncio.get_event_loop()
                for idx, cmd in enumerate(test_cmds[:3], 1):  # Limit to 3 test commands for performance
                    test_result = await loop.run_in_executor(
                        None,
                        lambda c=cmd: execute_tool(
                            "Bash",
                            {"command": c},
                            self.working_directory,
                            backend=self.backend,
                            extra_context={"todos": self._todos},
                        ),
                    )
                    
                    await on_event(AgentEvent(
                        type="tool_result",
                        content=test_result.output if test_result.success else f"❌ {test_result.output}",
                        data={
                            "tool_name": "Bash",
                            "tool_use_id": f"testing-{idx}",
                            "success": test_result.success,
                            "verification_stage": "testing",
                            "command": cmd
                        }
                    ))
                    
                    if not test_result.success:
                        stage_result["failures"].append(f"{cmd}: {test_result.output[:800]}")
                    
                    stage_result["tests_run"] += 1
            else:
                # No test commands found - use legacy test discovery
                rel_files = [os.path.relpath(p, self.working_directory) for p in modified_abs]
                test_files = self._select_impacted_tests(rel_files)
                
                if test_files:
                    loop = asyncio.get_event_loop()
                    test_files_quoted = [shlex.quote(f) for f in test_files[:10]]
                    test_cmd = f"pytest -q {' '.join(test_files_quoted)}"
                    
                    test_result = await loop.run_in_executor(
                        None,
                        lambda: execute_tool(
                            "Bash",
                            {"command": test_cmd},
                            self.working_directory,
                            backend=self.backend,
                            extra_context={"todos": self._todos},
                        ),
                    )
                    
                    await on_event(AgentEvent(
                        type="tool_result",
                        content=test_result.output if test_result.success else f"❌ {test_result.output}",
                        data={
                            "tool_name": "pytest",
                            "tool_use_id": "legacy-testing",
                            "success": test_result.success,
                            "verification_stage": "testing"
                        }
                    ))
                    
                    if not test_result.success:
                        stage_result["failures"].append(f"{test_cmd}: {test_result.output[:800]}")
                    
                    stage_result["tests_run"] = len(test_files)
                        
        except Exception as e:
            stage_result["failures"].append(f"Testing stage error: {str(e)}")
            logger.debug(f"Testing stage exception: {e}")
        
        stage_result["success"] = len(stage_result["failures"]) == 0
        return stage_result

    async def _run_quality_assessment_stage(
        self, 
        modified_abs: List[str], 
        on_event: Callable[[AgentEvent], Awaitable[None]]
    ) -> Dict[str, Any]:
        """Stage 4: Quality assessment - complexity, maintainability, coverage (future expansion)"""
        stage_result = {
            "success": True,
            "complexity_score": 0.0,
            "maintainability_score": 0.0,
            "quality_warnings": []
        }
        
        await on_event(AgentEvent(
            type="verification_stage",
            content="📊 **STAGE 4: Quality Assessment** - Analyzing code quality metrics...",
            data={"stage": "quality", "total_files": len(modified_abs)}
        ))
        
        # Quality checks: complexity, duplication, code smells
        for abs_path in modified_abs:
            if abs_path.endswith('.py'):
                rel_path = os.path.relpath(abs_path, self.working_directory)
                try:
                    content = self.backend.read_file(rel_path)
                    if not content:
                        continue
                    lines = content.split('\n')
                    line_count = len(lines)
                    if line_count > 500:
                        stage_result["quality_warnings"].append(f"{rel_path}: Large file ({line_count} lines)")

                    # Cyclomatic complexity approximation: count decision points
                    complexity = 0
                    for line in lines:
                        stripped = line.strip()
                        if stripped.startswith(('#', '"', "'")):
                            continue
                        complexity += stripped.count(' and ') + stripped.count(' or ')
                        complexity += sum(1 for k in ('if ', 'elif ', 'for ', 'while ', 'except:', 'except ', 'with ') if k in stripped)
                    if complexity > 50:
                        stage_result["quality_warnings"].append(f"{rel_path}: High complexity (~{complexity} decision points)")

                    # Simple duplicate-line detection (normalize whitespace, ignore empty)
                    seen: Dict[str, int] = {}
                    for ln in lines:
                        n = ln.strip()
                        if len(n) > 15 and not n.startswith('#'):
                            seen[n] = seen.get(n, 0) + 1
                    dupes = [k for k, v in seen.items() if v > 3]
                    if len(dupes) > 5:
                        stage_result["quality_warnings"].append(f"{rel_path}: Many repeated lines (possible duplication)")

                    if content.count('except:') > 0:
                        stage_result["quality_warnings"].append(f"{rel_path}: Bare except clauses detected")
                    if content.count('# TODO') + content.count('# FIXME') > 5:
                        stage_result["quality_warnings"].append(f"{rel_path}: Many TODOs/FIXMEs")
                except Exception as e:
                    logger.debug(f"Quality assessment failed for {rel_path}: {e}")
        return stage_result

    def _calculate_verification_confidence(self, verification_result: Dict[str, Any]) -> Dict[str, Any]:
        """Stage 5: Calculate overall confidence score and recommendations"""
        stages = verification_result["stage_results"]
        
        # Base confidence scoring
        confidence_score = 1.0
        recommendations = []
        
        # Static analysis impact (40% weight)
        static_result = stages.get("static", {})
        if not static_result.get("success", True):
            if static_result.get("critical", False):
                confidence_score *= 0.2  # Critical syntax errors
                recommendations.append("🚨 Critical syntax errors must be fixed before deployment")
            else:
                confidence_score *= 0.7  # Non-critical linting issues
                recommendations.append("⚠️ Consider fixing linting issues for better code quality")
        
        # Testing impact (60% weight for existing testing)
        testing_result = stages.get("testing", {})
        if not testing_result.get("success", True):
            confidence_score *= 0.6
            recommendations.append("🧪 Test failures detected - ensure functionality works correctly")
        elif testing_result.get("tests_run", 0) == 0:
            confidence_score *= 0.8
            recommendations.append("💡 No tests run - consider adding test coverage")
        
        # Overall assessment
        overall_success = all(
            stages.get(stage, {}).get("success", True) 
            for stage in ["static", "testing"]
        )
        
        return {
            "confidence_score": max(0.0, min(1.0, confidence_score)),
            "success": overall_success,
            "recommendations": recommendations
        }

    async def _run_deterministic_verification_gate(
        self,
        on_event: Callable[[AgentEvent], Awaitable[None]],
    ) -> tuple[bool, str]:
        """
        Run intelligent progressive verification with adaptive quality gates.
        
        Multi-stage verification inspired by GitHub Actions, CircleCI, and modern DevOps:
        1. Fast static analysis (syntax, imports, basic linting)
        2. Semantic validation (logic checks, pattern compliance)  
        3. Dynamic testing (unit tests, integration tests)
        4. Quality assessment (coverage, complexity, security)
        5. Confidence scoring and adaptive thresholds
        """
        modified_abs = [f for f in self._file_snapshots.keys() if os.path.isfile(f)]
        if not modified_abs:
            return True, "No modified files (or all deleted)."

        # Try progressive verification first (with fallback to legacy)
        try:
            progressive_result = await self._run_progressive_verification(modified_abs, on_event)
            
            if progressive_result.get("progressive_enabled", False):
                # Use progressive verification results
                success = progressive_result["success"]
                confidence_score = progressive_result.get("confidence_score", 0.0)
                recommendations = progressive_result.get("recommendations", [])
                failures = progressive_result.get("failures", [])
                
                # Attempt error recovery if there are failures
                if not success and failures:
                    recovery_result = await self._handle_verification_failure_with_recovery(
                        failures, modified_abs, on_event
                    )
                    
                    if recovery_result["recovered"]:
                        # Some issues were resolved - re-run verification on affected files
                        success = len(recovery_result["remaining_failures"]) == 0
                        if success:
                            confidence_score = max(confidence_score, 0.8)  # Boost confidence after successful recovery
                            recommendations.extend(recovery_result["recovery_actions"])
                        else:
                            failures = recovery_result["remaining_failures"]
                            recommendations.append(f"🔄 Partial recovery: {len(recovery_result['recovery_actions'])} issues auto-resolved")
                
                # Add contextual guidance based on current situation
                context = self._assess_context_for_guidance(modified_abs)
                guidance = self._generate_contextual_guidance("verify", context)
                
                # Prepare summary message
                if success:
                    summary = f"✅ **Progressive Verification PASSED** (Confidence: {confidence_score:.1%})"
                    if recommendations:
                        summary += f"\n\n**Recommendations**:\n" + "\n".join(f"- {rec}" for rec in recommendations)
                    if guidance:
                        summary += guidance
                else:
                    summary = f"❌ **Progressive Verification FAILED** (Confidence: {confidence_score:.1%})"
                    if failures:
                        summary += f"\n\n**Failures**:\n" + "\n".join(f"- {fail}" for fail in failures[:5])
                    if recommendations:
                        summary += f"\n\n**Recommendations**:\n" + "\n".join(f"- {rec}" for rec in recommendations)
                    if guidance:
                        summary += guidance
                
                await on_event(AgentEvent(
                    type="verification_complete",
                    content=summary,
                    data={
                        "progressive_verification": True,
                        "confidence_score": confidence_score,
                        "stage_results": progressive_result.get("stage_results", {}),
                        "recommendations": recommendations
                    }
                ))
                
                return success, summary
                
        except Exception as e:
            logger.warning(f"Progressive verification system failed: {e}")
            await on_event(AgentEvent(
                type="verification_fallback", 
                content=f"⚠️ Progressive verification failed, using legacy system: {e}",
                data={"error": str(e)}
            ))

        # Fallback to legacy verification

        loop = asyncio.get_event_loop()
        failures: List[str] = []
        checks_run: List[str] = []

        # 1) Per-file lint gate
        for idx, abs_path in enumerate(modified_abs, start=1):
            rel_path = os.path.relpath(abs_path, self.working_directory)
            lint_result = await loop.run_in_executor(
                None,
                lambda rp=rel_path: execute_tool(
                    "lint_file",
                    {"path": rp},
                    self.working_directory,
                    backend=self.backend,
                    extra_context={"todos": self._todos},
                ),
            )
            lint_text = lint_result.output if lint_result.success else (lint_result.error or lint_result.output or "Unknown lint error")
            checks_run.append(f"lint_file {rel_path}")
            await on_event(AgentEvent(
                type="tool_result",
                content=lint_text,
                data={
                    "tool_name": "lint_file",
                    "tool_use_id": f"deterministic-lint-{idx}",
                    "success": lint_result.success,
                    "deterministic_gate": True,
                },
            ))
            if not lint_result.success:
                failures.append(f"lint_file {rel_path}: {lint_text[:1000]}")

        # 2) Impacted tests first, then optional broader suite
        if app_config.deterministic_verification_run_tests:
            impacted_tests = [p for p in self._select_impacted_tests(modified_abs) if p.endswith(".py")]
            if impacted_tests:
                cmd = "pytest -q " + " ".join(shlex.quote(p) for p in impacted_tests[:20])
                test_result = await loop.run_in_executor(
                    None,
                    lambda: execute_tool(
                        "Bash",
                        {"command": cmd, "timeout": 180},
                        self.working_directory,
                        backend=self.backend,
                        extra_context={"todos": self._todos},
                    ),
                )
                test_text = test_result.output if test_result.success else (test_result.error or test_result.output or "Unknown test failure")
                checks_run.append(cmd)
                await on_event(AgentEvent(
                    type="tool_result",
                    content=test_text,
                    data={
                        "tool_name": "Bash",
                        "tool_use_id": "deterministic-tests",
                        "success": test_result.success,
                        "deterministic_gate": True,
                    },
                ))
                if not test_result.success:
                    failures.append(f"{cmd}: {test_text[:1600]}")
            has_py_changes = any(str(p).lower().endswith(".py") for p in modified_abs)
            if app_config.test_run_full_after_impact and has_py_changes and not failures:
                full_cmd = "pytest -q"
                full_result = await loop.run_in_executor(
                    None,
                    lambda: execute_tool(
                        "Bash",
                        {"command": full_cmd, "timeout": 300},
                        self.working_directory,
                        backend=self.backend,
                        extra_context={"todos": self._todos},
                    ),
                )
                full_text = full_result.output if full_result.success else (full_result.error or full_result.output or "Unknown test failure")
                checks_run.append(full_cmd)
                await on_event(AgentEvent(
                    type="tool_result",
                    content=full_text,
                    data={
                        "tool_name": "Bash",
                        "tool_use_id": "deterministic-tests-full",
                        "success": full_result.success,
                        "deterministic_gate": True,
                    },
                ))
                if not full_result.success:
                    failures.append(f"{full_cmd}: {full_text[:1600]}")

        # 3) Verification orchestrator (language/framework aware)
        if app_config.verification_orchestrator_enabled:
            for idx, cmd in enumerate(self._verification_orchestrator_commands(modified_abs), start=1):
                run_result = await loop.run_in_executor(
                    None,
                    lambda c=cmd: execute_tool(
                        "Bash",
                        {"command": c, "timeout": 240},
                        self.working_directory,
                        backend=self.backend,
                        extra_context={"todos": self._todos},
                    ),
                )
                out = run_result.output if run_result.success else (run_result.error or run_result.output or "Verification command failed")
                checks_run.append(cmd)
                await on_event(AgentEvent(
                    type="tool_result",
                    content=out,
                    data={
                        "tool_name": "Bash",
                        "tool_use_id": f"verification-orchestrator-{idx}",
                        "success": run_result.success,
                        "deterministic_gate": True,
                    },
                ))
                if not run_result.success:
                    failures.append(f"{cmd}: {out[:1600]}")

        summary = "Deterministic verification checks:\n- " + "\n- ".join(checks_run[:30])
        if failures:
            summary += "\n\nFailures:\n- " + "\n- ".join(failures[:20])
            self._record_failure_pattern("verification_gate_failure", summary[:2000], {"checks_run": checks_run[:30]})
            return False, summary
        summary += "\n\nAll deterministic verification checks passed."
        return True, summary

    # ------------------------------------------------------------------
    # Core agent loop (used by both run and run_build)
    # ------------------------------------------------------------------

    async def _agent_loop(
        self,
        on_event: Callable[[AgentEvent], Awaitable[None]],
        request_approval: Callable[[str, str, Dict], Awaitable[bool]],
        config: Optional[GenerationConfig] = None,
        request_question_answer: Optional[Callable[..., Awaitable[str]]] = None,
    ):
        """Core streaming agent loop with tool execution."""
        if app_config.codebase_index_enabled and hasattr(self.service, "embed_texts"):
            try:
                from codebase_index import set_embed_fn
                set_embed_fn(self.service.embed_texts)
            except Exception:
                pass
        gen_config = config or self._default_config()
        iteration = 0
        reasoning_trace_repairs = 0

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

            last_stop_reason: Optional[str] = None
            should_auto_continue = False  # max_tokens cut-off: next iteration will send continuation
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

                    build_tools = (TOOL_DEFINITIONS + [ASK_USER_QUESTION_DEFINITION]) if request_question_answer else TOOL_DEFINITIONS

                    def _stream_producer():
                        """Run the sync generator in a background thread, forwarding chunks to the queue."""
                        try:
                            for c in self.service.generate_response_stream(
                                messages=self.history,
                                system_prompt=self._effective_system_prompt(self.system_prompt),
                                model_id=None,
                                config=gen_config,
                                tools=build_tools,
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
                            last_stop_reason = chunk.get("stop_reason") or None

                    producer_thread.join(timeout=5)
                    stream_succeeded = True
                    self._history_len_at_last_call = len(self.history)
                    break  # exit retry loop — stream completed

                except (BedrockError, Exception) as stream_err:
                    producer_thread.join(timeout=2)

                    # Determine if this error is retryable (connection/timeout/throttle/token limit)
                    err_str = str(stream_err).lower()
                    retryable_keywords = [
                        "timeout", "timed out", "connection", "reset by peer",
                        "broken pipe", "eof", "throttl", "serviceunav",
                        "read timeout", "endpoint url", "connect timeout",
                        "network", "socket", "aborted",
                        "max_tokens", "token limit", "ran out of tokens", "output length",
                        "context length", "input length",
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
                        self._record_failure_pattern(
                            "stream_failure",
                            err_msg[:1200],
                            {"attempt": attempt, "max_retries": max_retries},
                        )
                        if any(phrase in err_str for phrase in ("token", "max_tokens", "length limit", "context")):
                            user_msg = (
                                "Response hit a length limit. Conversation was compacted. "
                                "Re-send your message or break the task into smaller steps."
                            )
                            try:
                                self._trim_history()
                            except Exception:
                                pass
                        else:
                            user_msg = f"Streaming error: {err_msg}\n\nYour message was rolled back — you can re-send it."
                        await on_event(AgentEvent(type="stream_failed", content=user_msg))
                        stream_succeeded = False
                        break

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

            # Response was cut off by max_tokens — continue next iteration (user never sees "ran out of tokens")
            if not tool_uses and last_stop_reason in ("max_tokens", "length"):
                self.history.append({
                    "role": "user",
                    "content": (
                        "[SYSTEM] Your previous response was cut off due to length. "
                        "Continue from where you left off. If you were mid tool call, complete it. "
                        "If you were explaining, briefly summarize progress and continue the task."
                    ),
                })
                await on_event(AgentEvent(
                    type="stream_recovering",
                    content="Continuing automatically...",
                ))
                should_auto_continue = True

            if should_auto_continue:
                continue  # next while iteration: stream again with continuation user message

            if not tool_uses:
                assistant_text = self._extract_assistant_text(assistant_content)

                # Hard gate: if we just processed tool results, require structured
                # user-visible reasoning trace before final completion.
                if (
                    app_config.enforce_reasoning_trace
                    and self._last_user_message_has_tool_results()
                    and not self._has_structured_reasoning_trace(assistant_text)
                ):
                    if reasoning_trace_repairs < 2:
                        reasoning_trace_repairs += 1
                        self.history.append({
                            "role": "user",
                            "content": (
                                "[SYSTEM] Before finishing, provide a structured reasoning trace using these exact headings:\n"
                                "- What I learned\n"
                                "- Why it matters\n"
                                "- Decision\n"
                                "- Next actions\n"
                                "- Verification status\n\n"
                                "Then conclude."
                            ),
                        })
                        await on_event(AgentEvent(
                            type="stream_recovering",
                            content="Requesting structured reasoning trace before completion...",
                        ))
                        continue
                else:
                    reasoning_trace_repairs = 0

                # Deterministic verification gate before done (max 2 attempts)
                if not hasattr(self, '_verification_gate_attempts'):
                    self._verification_gate_attempts = 0
                if (
                    app_config.deterministic_verification_gate
                    and self._file_snapshots
                    and not self._deterministic_verification_done
                    and self._verification_gate_attempts < 2
                ):
                    # Only verify files that still exist on disk
                    existing_snapshots = {k: v for k, v in self._file_snapshots.items() if os.path.isfile(k)}
                    if not existing_snapshots:
                        # All tracked files were deleted — nothing to verify
                        self._deterministic_verification_done = True
                    else:
                        gate_ok, gate_summary = await self._run_deterministic_verification_gate(on_event)
                        self._verification_gate_attempts += 1
                        if not gate_ok and self._verification_gate_attempts < 2:
                            self.history.append({
                                "role": "user",
                                "content": (
                                    "[SYSTEM] Verification found issues. Try to fix them, but if the issues "
                                    "are pre-existing or unrelated to your changes, just confirm the task is "
                                    "complete and move on. Do NOT loop — one fix attempt only.\n\n"
                                    + gate_summary
                                ),
                            })
                            await on_event(AgentEvent(
                                type="stream_recovering",
                                content="Verification found issues — one fix attempt...",
                            ))
                            continue
                        # Either passed or exhausted attempts — proceed
                        self._deterministic_verification_done = True
                        self.history.append({
                            "role": "user",
                            "content": (
                                "[SYSTEM] Verification complete:\n\n"
                                + gate_summary
                                + "\n\nProvide final completion update and finish."
                            ),
                        })
                        continue

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
                tool_uses, on_event, request_approval, request_question_answer=request_question_answer
            )
            reasoning_trace_repairs = 0

            # Cap tool results before they enter history (prevention > cure)
            capped_results = self._cap_tool_results(tool_results)

            # Post-edit verification nudge: if any write tools were used,
            # append a system hint reminding the model to verify its changes.
            write_tools_used = {
                tu.get("name") for tu in tool_uses
                if tu.get("name") in ("Edit", "Write", "symbol_edit")
            }
            if write_tools_used:
                modified_files = [
                    tu.get("input", {}).get("path", "?")
                    for tu in tool_uses
                    if tu.get("name") in ("Edit", "Write", "symbol_edit")
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
        request_question_answer: Optional[Callable[..., Awaitable[str]]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Execute a batch of tool calls, running safe (read-only) tools in parallel
        and dangerous (write) tools after collecting approvals.

        Returns a list of tool_result dicts ready for the conversation history.
        """
        loop = asyncio.get_event_loop()
        results_by_id: Dict[str, Dict[str, Any]] = {}

        original_tool_uses = tool_uses
        _special_tools = ("TodoWrite", "TodoRead", "MemoryWrite", "MemoryRead", "AskUserQuestion")
        todo_calls = [tu for tu in tool_uses if tu.get("name") == "TodoWrite"]
        todo_read_calls = [tu for tu in tool_uses if tu.get("name") == "TodoRead"]
        memory_calls = [tu for tu in tool_uses if tu.get("name") in ("MemoryWrite", "MemoryRead")]
        ask_calls = [tu for tu in tool_uses if tu.get("name") == "AskUserQuestion"]
        rest_calls = [tu for tu in tool_uses if tu.get("name") not in _special_tools]

        for tu in todo_calls:
            inp = tu.get("input") or {}
            raw = list(inp.get("todos") or [])
            # Normalize to { id, content, status } for our UI/persistence (SDK schema uses content, status; id optional)
            self._todos = [
                {"id": t.get("id") or str(i), "content": t.get("content", ""), "status": t.get("status", "pending")}
                for i, t in enumerate(raw, 1)
            ]
            lines = [f"Todos updated ({len(self._todos)} items)."]
            for t in self._todos:
                lines.append(f"  [{t.get('status', 'pending')}] {t.get('content', '')}")
            content = "\n".join(lines)
            results_by_id[tu["id"]] = {
                "type": "tool_result",
                "tool_use_id": tu["id"],
                "content": content,
                "is_error": False,
            }
            await on_event(AgentEvent(
                type="tool_result",
                content=content,
                data={"tool_name": "TodoWrite", "tool_use_id": tu["id"], "success": True, "todos": list(self._todos)},
            ))
            await on_event(AgentEvent(type="todos_updated", content="", data={"todos": list(self._todos)}))

        for tu in todo_read_calls:
            # Return current todos as JSON for the model (same shape as TodoWrite: id, content, status)
            todos_list = list(self._todos)
            if not todos_list:
                content = "No todos yet. Use TodoWrite to create a task list."
            else:
                content = json.dumps(todos_list, indent=2)
            results_by_id[tu["id"]] = {
                "type": "tool_result",
                "tool_use_id": tu["id"],
                "content": content,
                "is_error": False,
            }
            await on_event(AgentEvent(
                type="tool_result",
                content=content,
                data={"tool_name": "TodoRead", "tool_use_id": tu["id"], "success": True},
            ))

        _MEMORY_VALUE_CAP = 10_000  # chars per value to avoid abuse

        for tu in memory_calls:
            inp = tu.get("input") or {}
            name = tu.get("name", "")
            if name == "MemoryWrite":
                key = (inp.get("key") or "").strip()
                if not key:
                    content = "Error: key is required and cannot be empty."
                    is_err = True
                else:
                    value = inp.get("value", "")
                    if isinstance(value, str):
                        pass
                    else:
                        value = json.dumps(value) if value is not None else ""
                    value = (value or "")[: _MEMORY_VALUE_CAP]
                    self._memory[key] = value
                    content = f"Stored key '{key}'."
                    is_err = False
            else:
                # MemoryRead
                key = (inp.get("key") or "").strip()
                if key:
                    val = self._memory.get(key)
                    if val is None:
                        content = f"No value stored for key '{key}'."
                    else:
                        content = val
                    is_err = False
                else:
                    if not self._memory:
                        content = "No facts stored yet. Use MemoryWrite to store key-value facts."
                    else:
                        lines = [f"{k}: {v}" for k, v in sorted(self._memory.items())]
                        content = "\n".join(lines)
                    is_err = False
            results_by_id[tu["id"]] = {
                "type": "tool_result",
                "tool_use_id": tu["id"],
                "content": content,
                "is_error": is_err,
            }
            await on_event(AgentEvent(
                type="tool_result",
                content=content,
                data={"tool_name": name, "tool_use_id": tu["id"], "success": not is_err},
            ))

        for tu in ask_calls:
            inp = tu.get("input") or {}
            question = inp.get("question") or ""
            context = inp.get("context") or ""
            options = inp.get("options")
            if isinstance(options, list):
                options = [str(o) for o in options]
            else:
                options = None
            if request_question_answer:
                try:
                    answer = await request_question_answer(question, context, tu["id"], options=options)
                except Exception as e:
                    answer = f"Error asking user: {e}"
            else:
                answer = "No question callback; proceeding with best assumption."
            results_by_id[tu["id"]] = {
                "type": "tool_result",
                "tool_use_id": tu["id"],
                "content": answer,
                "is_error": False,
            }

        tool_uses = rest_calls

        async def _run_command_with_streaming(tool_id: str, tool_input: Dict[str, Any]) -> ToolResult:
            """Run command with live output events when enabled."""
            command = tool_input.get("command", "")
            timeout = int(tool_input.get("timeout", 30) or 30)

            if not app_config.live_command_streaming:
                return await loop.run_in_executor(
                    None, lambda: execute_tool("Bash", tool_input, self.working_directory, backend=self.backend, extra_context={"todos": self._todos})
                )

            partial_sent = {"value": False}

            def _on_output(chunk: str, is_stderr: bool) -> None:
                if not chunk:
                    return
                try:
                    asyncio.run_coroutine_threadsafe(
                        on_event(AgentEvent(
                            type="command_output",
                            content=chunk,
                            data={
                                "tool_use_id": tool_id,
                                "is_stderr": bool(is_stderr),
                            },
                        )),
                        loop,
                    )
                except Exception:
                    pass

                # Partial failure signal for quicker UX feedback
                if not partial_sent["value"] and re.search(r"(error|failed|traceback|exception)", chunk, flags=re.IGNORECASE):
                    partial_sent["value"] = True
                    try:
                        asyncio.run_coroutine_threadsafe(
                            on_event(AgentEvent(
                                type="command_partial_failure",
                                content="Potential failure detected in command output.",
                                data={"tool_use_id": tool_id},
                            )),
                            loop,
                        )
                    except Exception:
                        pass

            def _exec_stream() -> ToolResult:
                stdout, stderr, rc = self.backend.run_command_stream(
                    command,
                    cwd=".",
                    timeout=timeout,
                    on_output=_on_output,
                )
                parts = []
                if stdout:
                    parts.append(stdout)
                if stderr:
                    parts.append(f"[stderr]\n{stderr}")
                output = "\n".join(parts) if parts else "(no output)"
                if rc != 0:
                    output = f"[exit code: {rc}]\n{output}"

                if len(output) > 20000:
                    lines_out = output.split("\n")
                    if len(lines_out) > 200:
                        output = "\n".join(lines_out[:100]) + f"\n\n... [{len(lines_out) - 150} lines truncated] ...\n\n" + "\n".join(lines_out[-50:])
                    else:
                        output = output[:10000] + "\n\n... [truncated] ...\n\n" + output[-5000:]

                return ToolResult(
                    success=rc == 0,
                    output=output,
                    error=None if rc == 0 else f"Command exited with code {rc}",
                )

            return await loop.run_in_executor(None, _exec_stream)

        # Partition into safe and dangerous
        safe_calls = []
        dangerous_calls = []
        for tu in tool_uses:
            name = tu["name"]
            if name in SAFE_TOOLS:
                safe_calls.append(tu)
            else:
                dangerous_calls.append(tu)

        # ---- 1. Run all safe tools concurrently ----
        # NOTE: tool_call events are already emitted by the streaming loop
        # in _agent_loop, so we skip emitting them here to avoid duplicates.
        if safe_calls:
            # Dedup: if multiple reads target the same file (no offset), share the result
            _dedup_reads: Dict[str, asyncio.Future] = {}

            async def _run_safe(tu: Dict[str, Any]) -> tuple:
                name = tu["name"]
                inp = tu["input"]

                # File cache: return cached content for Read if file hasn't been modified
                if name == "Read" and not inp.get("offset") and not inp.get("limit"):
                    path = inp.get("path", "")
                    cache_key = self._file_cache_key(path)

                    # Dedup within the same batch (by resolved path for backend consistency)
                    resolved = self.backend.resolve_path(path)
                    if resolved in _dedup_reads:
                        cached_result = await _dedup_reads[resolved]
                        return tu, cached_result

                    # Check file cache
                    if cache_key in self._file_cache:
                        cached_content, _ = self._file_cache[cache_key]
                        if resolved not in self._file_snapshots:
                            return tu, ToolResult(success=True, output=cached_content)

                result = await loop.run_in_executor(
                    None, lambda _tu=tu: execute_tool(_tu["name"], _tu["input"], self.working_directory, backend=self.backend, extra_context={"todos": self._todos})
                )

                # Cache successful full-file reads
                if name == "Read" and result.success and not inp.get("offset") and not inp.get("limit"):
                    path = inp.get("path", "")
                    cache_key = self._file_cache_key(path)
                    self._file_cache[cache_key] = (result.output, time.time())

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
                if not result.success:
                    self._record_failure_pattern(
                        "safe_tool_failure",
                        result_text[:1000],
                        {"tool_name": tu["name"], "tool_input": tu.get("input", {})},
                    )

        # ---- 2. Handle dangerous tools ----
        #   File writes: generally revertible, but policy engine may force approval.
        #   Commands: require explicit approval unless configured otherwise.
        if dangerous_calls:
            file_write_calls = [
                tu for tu in dangerous_calls
                if tu["name"] in ("Write", "Edit", "symbol_edit")
            ]
            command_calls = [
                tu for tu in dangerous_calls
                if tu["name"] not in ("Write", "Edit", "symbol_edit")
            ]

            # Policy engine + explicit approvals for risky file writes
            filtered_file_writes: List[Dict[str, Any]] = []
            for tu in file_write_calls:
                decision = self._policy_decision(tu["name"], tu["input"])
                if decision.blocked:
                    msg = f"Blocked by policy engine: {decision.reason or 'Operation is not allowed.'}"
                    results_by_id[tu["id"]] = {
                        "type": "tool_result",
                        "tool_use_id": tu["id"],
                        "content": msg,
                        "is_error": True,
                    }
                    await on_event(AgentEvent(
                        type="tool_rejected",
                        content=tu["name"],
                        data={"tool_use_id": tu["id"], "reason": decision.reason, "policy_blocked": True},
                    ))
                    self._record_failure_pattern("policy_block", msg, {"tool_name": tu["name"], "input": tu["input"]})
                    continue

                if decision.require_approval:
                    if self.was_previously_approved(tu["name"], tu["input"]):
                        await on_event(AgentEvent(
                            type="auto_approved",
                            content=tu["name"],
                            data={"tool_input": tu["input"], "policy_reason": decision.reason},
                        ))
                    else:
                        desc = self._format_tool_description(tu["name"], tu["input"])
                        if decision.reason:
                            desc += f"\n\nPolicy note: {decision.reason}"
                        approved = await request_approval(tu["name"], desc, tu["input"])
                        if not approved:
                            results_by_id[tu["id"]] = {
                                "type": "tool_result",
                                "tool_use_id": tu["id"],
                                "content": "User rejected this operation.",
                                "is_error": True,
                            }
                            await on_event(AgentEvent(
                                type="tool_rejected",
                                content=tu["name"],
                                data={"tool_use_id": tu["id"], "reason": decision.reason},
                            ))
                            continue
                        self.remember_approval(tu["name"], tu["input"])
                filtered_file_writes.append(tu)
            file_write_calls = filtered_file_writes

            # --- Phase A: file writes (auto-approved, revertible) ---
            if file_write_calls:
                # Snapshot all files BEFORE any writes
                for tu in file_write_calls:
                    self._snapshot_file(tu["name"], tu["input"])

                # Group by resolved path so same-file edits serialize (backend-agnostic)
                file_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
                for tu in file_write_calls:
                    path = tu["input"].get("path", "")
                    abs_path = self.backend.resolve_path(path)
                    file_groups[abs_path].append(tu)

                # Session checkpoint before risky file batch
                cp_id = self._create_session_checkpoint(
                    label=f"before_file_batch:{len(file_write_calls)}",
                    target_paths=list(file_groups.keys()),
                )
                if cp_id:
                    await on_event(AgentEvent(
                        type="checkpoint_created",
                        content="Checkpoint",
                        data={"checkpoint_id": cp_id, "label": "before_file_batch"},
                    ))

                async def _run_file_group(
                    calls: List[Dict[str, Any]],
                ) -> List[tuple]:
                    """Serial writes to one file; stop on first failure.
                    Includes auto-lint after edits and auto-retry on edit failure."""
                    results = []
                    for tu in calls:
                        result = await loop.run_in_executor(
                            None, lambda _tu=tu: execute_tool(_tu["name"], _tu["input"], self.working_directory, backend=self.backend, extra_context={"todos": self._todos})
                        )

                        # ── Auto-retry on edit failure ──
                        # If Edit failed with "not found" or "multiple occurrences",
                        # re-read the file and include content in the error for immediate retry
                        if not result.success and tu["name"] == "Edit":
                            err = result.error or ""
                            if "not found" in err.lower() or "occurrences" in err.lower():
                                path = tu["input"].get("path", "")
                                try:
                                    fresh = await loop.run_in_executor(
                                        None, lambda: execute_tool(
                                            "Read", {"path": path},
                                            self.working_directory, backend=self.backend,
                                            extra_context={"todos": self._todos},
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
                        if result.success and tu["name"] in ("Edit", "Write", "symbol_edit"):
                            path = tu["input"].get("path", "")
                            try:
                                lint_result = await loop.run_in_executor(
                                    None, lambda: execute_tool(
                                        "lint_file", {"path": path},
                                        self.working_directory, backend=self.backend,
                                        extra_context={"todos": self._todos},
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
                            self._file_cache.pop(self._file_cache_key(path), None)

                            # If this was a created file (snapshot was None), store content
                            # so Revert can bring the file back if the agent later deletes it
                            if tu["name"] == "Write" and path:
                                abs_path = self.backend.resolve_path(path)
                                if self._file_snapshots.get(abs_path) is None:
                                    written = tu["input"].get("content", "")
                                    if isinstance(written, str) and len(written) < 1_000_000:
                                        try:
                                            written.encode("utf-8")
                                            self._file_snapshots[abs_path] = {"created": True, "content": written}
                                        except (UnicodeDecodeError, UnicodeEncodeError):
                                            pass

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
                        if not result.success:
                            self._record_failure_pattern(
                                "file_edit_failure",
                                result_text[:1200],
                                {"tool_name": tu["name"], "tool_input": tu.get("input", {})},
                            )

            # --- Phase B: commands — require approval (irreversible) ---
            # In YOLO mode, auto-approve all commands
            for tu in command_calls:
                tool_name = tu["name"]
                tool_input = tu["input"]
                tool_id = tu["id"]
                decision = self._policy_decision(tool_name, tool_input)

                if decision.blocked:
                    blocked_msg = f"Blocked by policy engine: {decision.reason or 'Operation is not allowed.'}"
                    results_by_id[tool_id] = {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": blocked_msg,
                        "is_error": True,
                    }
                    await on_event(AgentEvent(
                        type="tool_rejected",
                        content=tool_name,
                        data={"tool_use_id": tool_id, "reason": decision.reason, "policy_blocked": True},
                    ))
                    self._record_failure_pattern("policy_block", blocked_msg, {"tool_name": tool_name, "tool_input": tool_input})
                    continue

                # Check YOLO mode, approval memory, or ask for approval
                if decision.require_approval:
                    if self.was_previously_approved(tool_name, tool_input):
                        await on_event(AgentEvent(
                            type="auto_approved",
                            content=tool_name,
                            data={"tool_input": tool_input, "policy_reason": decision.reason},
                        ))
                    else:
                        description = self._format_tool_description(tool_name, tool_input)
                        if decision.reason:
                            description += f"\n\nPolicy note: {decision.reason}"
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
                elif app_config.auto_approve_commands:
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

                # Emit command_start for Bash so the UI shows "running"
                if tool_name == "Bash":
                    await on_event(AgentEvent(
                        type="command_start",
                        content=tool_input.get("command", "?"),
                        data={"tool_use_id": tool_id},
                    ))

                # Session checkpoint before risky command batches
                cp_id = self._create_session_checkpoint(
                    label=f"before_command:{tool_name}",
                    target_paths=list(self._file_snapshots.keys()),
                )
                if cp_id:
                    await on_event(AgentEvent(
                        type="checkpoint_created",
                        content="Checkpoint",
                        data={"checkpoint_id": cp_id, "label": f"before_command:{tool_name}"},
                    ))

                cmd_start = time.time()
                if tool_name == "Bash":
                    result = await _run_command_with_streaming(tool_id, tool_input)
                else:
                    result = await loop.run_in_executor(
                        None, lambda: execute_tool(tool_name, tool_input, self.working_directory, backend=self.backend, extra_context={"todos": self._todos})
                    )
                cmd_duration = round(time.time() - cmd_start, 1)

                result_text = result.output if result.success else (
                    result.error or "Unknown error"
                )
                if not result.success and self._session_checkpoints:
                    last_cp = self._session_checkpoints[-1].get("id", "latest")
                    result_text += f"\n\n[checkpoint] You can rewind with checkpoint id: {last_cp}"

                # Extract exit code from Bash output
                exit_code = None
                if tool_name == "Bash":
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
                if not result.success:
                    self._record_failure_pattern(
                        "command_failure",
                        result_text[:1200],
                        {"tool_name": tool_name, "tool_input": tool_input},
                    )

        return [results_by_id[tu["id"]] for tu in original_tool_uses if tu["id"] in results_by_id]

    def _format_tool_description(self, name: str, inputs: Dict) -> str:
        """Format a human-readable description of a tool call for approval"""
        if name == "Write":
            content = inputs.get("content", "")
            line_count = content.count("\n") + 1
            return f"Write {line_count} lines to {inputs.get('path', '?')}"
        elif name == "Edit":
            return f"Edit {inputs.get('path', '?')}: replace string"
        elif name == "symbol_edit":
            return (
                f"Symbol edit {inputs.get('path', '?')}: "
                f"{inputs.get('symbol', '?')} ({inputs.get('kind', 'all')})"
            )
        elif name == "Bash":
            return f"Run: {inputs.get('command', '?')}"
        elif name == "plan_review":
            step_count = len(inputs.get("plan_steps", []) or [])
            return f"Review and approve plan execution ({step_count} steps)"
        return f"{name}({json.dumps(inputs)[:200]})"
