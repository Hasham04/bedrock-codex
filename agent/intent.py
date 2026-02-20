"""
Intent classification for coding tasks.
Determines whether tasks need scouting, planning, and classifies complexity.
"""

import json
import logging
from typing import Dict, Any
from config import app_config

logger = logging.getLogger(__name__)


CLASSIFY_SYSTEM = """You are a task classifier for a coding agent. Analyze the user's message and return ONLY valid JSON:
{"scout": true/false, "plan": true/false, "question": true/false, "complexity": "trivial"|"simple"|"complex"}

**Complexity levels**:
- **Trivial**: Greetings, yes/no, single commands, short confirmations, "do it"
- **Simple**: Single-file edits, questions about specific code, explanations, rename a variable, fix a typo
- **Complex**: Multi-file changes, architecture work, new features, refactors, audits, reviews, analysis tasks, anything spanning multiple subsystems, migrations, deprecations, backward-compatible changes, cross-service modifications, anything touching shared libraries or interfaces

Audit, review, analysis, and investigation tasks are ALWAYS complex.

**Field rules**:
- **question** = true ONLY when the user is asking a question or requesting an explanation (NOT requesting code changes). Questions are answered directly without planning.
- **scout** = true when the agent needs to explore the codebase to understand structure, find files, or locate relevant code. Scout is cheap — when in doubt, set true.
- **plan** = true when multi-step coordination across multiple files is required. NEVER true for questions. NEVER true for execution commands.
- **plan** = false when: the user says "implement", "execute", "go ahead", "do it", "build it", "apply the plan", or similar — they want EXECUTION, not a new plan. Also false for single-file edits that don't need coordination.

**Examples**:
- "Fix the bug in auth.py" → {"scout": true, "plan": false, "question": false, "complexity": "simple"}
- "Add user authentication system" → {"scout": true, "plan": true, "question": false, "complexity": "complex"}
- "What does this function do?" → {"scout": true, "plan": false, "question": true, "complexity": "simple"}
- "Run the tests" → {"scout": false, "plan": false, "question": false, "complexity": "trivial"}
- "Explain how recursion works" → {"scout": false, "plan": false, "question": true, "complexity": "simple"}
- "Hi, how are you?" → {"scout": false, "plan": false, "question": true, "complexity": "trivial"}
- "Can you explain what this error means?" → {"scout": true, "plan": false, "question": true, "complexity": "simple"}
- "Refactor the database layer to use connection pooling" → {"scout": true, "plan": true, "question": false, "complexity": "complex"}
- "Do an end to end audit of this codebase" → {"scout": true, "plan": true, "question": false, "complexity": "complex"}
- "Review this code for security issues" → {"scout": true, "plan": true, "question": false, "complexity": "complex"}
- "Implement the plan" → {"scout": false, "plan": false, "question": false, "complexity": "complex"}
- "Go ahead and implement it" → {"scout": false, "plan": false, "question": false, "complexity": "complex"}
- "Do it" → {"scout": false, "plan": false, "question": false, "complexity": "trivial"}
- "Implement the plan also change the UI to use a single pill" → {"scout": true, "plan": false, "question": false, "complexity": "complex"}
- "Add a button to the header and update the tests" → {"scout": true, "plan": true, "question": false, "complexity": "complex"}
- "Migrate the auth system from sessions to JWT" → {"scout": true, "plan": true, "question": false, "complexity": "complex"}
- "Deprecate the old API endpoint" → {"scout": true, "plan": true, "question": false, "complexity": "complex"}
- "Make this change backward compatible" → {"scout": true, "plan": true, "question": false, "complexity": "complex"}
- "Update the shared utils to support the new format" → {"scout": true, "plan": true, "question": false, "complexity": "complex"}
- "Debug why the login is failing" → {"scout": true, "plan": false, "question": false, "complexity": "complex"}

When uncertain: scout=true (cheap), plan=false, question=false, complexity="complex".
Return ONLY the JSON object, no explanation."""

# Cache for the classifier — avoids re-calling for the same message
_classify_cache: Dict[str, Dict[str, Any]] = {}


def classify_intent(task: str, service=None) -> Dict[str, Any]:
    """Use a fast LLM call to classify whether a task needs scouting and/or planning,
    and determine task complexity for smart model routing.

    Returns {"scout": bool, "plan": bool, "complexity": "trivial"|"simple"|"complex"}.
    Falls back to conservative defaults if the LLM call fails.
    """
    stripped = task.strip()
    if not stripped:
        return {"scout": False, "plan": False, "question": False, "complexity": "trivial"}

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
        result = json.loads(text)
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
    if len(words) <= 2:
        return {"scout": False, "plan": False, "question": False, "complexity": "trivial"}
    question_starters = ("what", "why", "how", "explain", "can you explain",
                         "tell me", "describe", "is it", "are there", "do you",
                         "could you", "would you", "hi", "hello", "hey")
    is_question = (stripped.endswith("?") or
                   any(stripped.startswith(q) for q in question_starters))
    if is_question:
        return {"scout": True, "plan": False, "question": True, "complexity": "simple"}
    execute_indicators = (
        "implement the plan", "implement it", "execute the plan", "execute it",
        "go ahead", "do it", "build it", "apply the plan", "apply the changes",
        "just implement", "just execute", "just do it",
    )
    if any(kw in stripped for kw in execute_indicators):
        return {"scout": False, "plan": False, "question": False, "complexity": "complex"}
    complex_indicators = (
        "audit", "refactor", "review", "analyze", "analyse", "overhaul",
        "redesign", "end to end", "end-to-end", "codebase", "rip apart",
        "find all bugs", "security review", "architecture",
        "migrate", "migration", "deprecate", "backward compatible",
        "backward-compatible", "breaking change", "cross-service",
        "shared library", "api contract", "interface change",
        "upgrade", "downgrade", "rollback strategy",
    )
    if any(kw in stripped for kw in complex_indicators):
        return {"scout": True, "plan": True, "question": False, "complexity": "complex"}
    # Default to complex — safer to over-allocate (main model for a simple task)
    # than under-allocate (fast model for an audit)
    return {"scout": True, "plan": False, "question": False, "complexity": "complex"}


def needs_planning(task: str, service=None) -> bool:
    """Use LLM-based intent classification to decide if planning is needed."""
    return classify_intent(task, service).get("plan", False)