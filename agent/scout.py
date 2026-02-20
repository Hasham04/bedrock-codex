"""
Scouting and task refinement for the coding agent.
Handles running a fast model to gather codebase context before the main agent starts.
"""

import asyncio
import json
import logging
import os
import re
import queue
import threading
from typing import List, Dict, Any, Optional, Callable, Awaitable

from bedrock_service import GenerationConfig, BedrockError
from tools import SCOUT_TOOL_DEFINITIONS, execute_tool, ToolResult
from config import app_config

from .events import AgentEvent
from .prompts import (
    _compose_system_prompt, _detect_project_language,
    SCOUT_TOOL_NAMES, SCOUT_TOOL_DISPLAY_NAMES,
)

logger = logging.getLogger(__name__)


class ScoutMixin:
    """Mixin providing scouting (fast context gathering) and task refinement.

    Expects the host class to provide:
    - self.service (BedrockService)
    - self.backend (Backend)
    - self.working_directory (str)
    - self._detected_language (str)
    - self._total_input_tokens, _total_output_tokens (int)
    - self._cache_read_tokens (int)
    """

    _TASK_REFINE_SYSTEM = """You are a coding task refiner. Given a user's raw task description, produce a compact spec with:

## Output specification
- <what the user wants>

## Constraints
- <constraint 1>
- <constraint 2>

## Impact scope
- <what else might be affected>

## Verification criteria
- <how to verify correctness>

## Task
<original or lightly clarified task>

Keep the whole response under 400 words. If the request is already very clear and minimal, you may return it with only brief output spec and "None" for constraints."""

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
            throughput_mode="cross-region",
        )
        scout_model = app_config.scout_model
        scout_messages: List[Dict[str, Any]] = []

        # Check if auto-context already injected structure/semantic info
        has_structure = "<project_structure>" in task
        has_semantic = "<semantic_context>" in task

        if has_structure and has_semantic:
            # Auto-context already provides both — do a minimal 2-iteration scout
            scout_user_content = (
                "You are a fast scout agent. Auto-context already includes the project structure and "
                "semantic search results. Your job: scan the auto-context below, then do ONE targeted "
                "read of the most critical file(s) for this task. Return a concise summary.\n\n"
                "IMPORTANT: Batch all tool calls in a single response. Finish in 1-2 turns MAX.\n\n"
                f"Task: {task[:3000]}"
            )
            max_scout_iters = 2
        elif has_structure or has_semantic:
            scout_user_content = (
                "You are a fast scout agent. Some context is already injected (check the task below). "
                "Fill in what's missing — if you have structure but not semantic results, run semantic_retrieve. "
                "If you have semantic but not structure, run project_tree. Then read 1-3 key files.\n\n"
                "IMPORTANT: Batch ALL tool calls together in each response. Finish in 2-3 turns MAX.\n\n"
                f"Task: {task[:3000]}"
            )
            max_scout_iters = 3
        else:
            scout_user_content = (
                "You are a fast scout agent. Quickly gather context about the codebase.\n\n"
                f"Task: {task[:3000]}\n\n"
                "IMPORTANT: Call project_tree AND semantic_retrieve TOGETHER in your first response "
                "(they are independent — batch them). Then read 1-3 key files if needed. "
                "Return a concise context summary. Finish in 2-4 turns MAX — speed matters."
            )
            max_scout_iters = min(app_config.scout_max_iterations, 6)

        scout_messages.append({"role": "user", "content": scout_user_content})

        all_text: List[str] = []
        scout_tools = SCOUT_TOOL_DEFINITIONS

        for scout_iter in range(max_scout_iters):
            # Stream scout response
            q: queue.Queue = queue.Queue()

            def _producer():
                try:
                    for chunk in self.service.generate_response_stream(
                        messages=scout_messages,
                        system_prompt=scout_system,
                        tools=scout_tools,
                        model_id=scout_model,
                        config=scout_config,
                    ):
                        q.put(chunk)
                except Exception as e:
                    q.put({"type": "error", "error": str(e)})
                finally:
                    q.put(None)

            t = threading.Thread(target=_producer, daemon=True)
            t.start()

            c_text = ""
            t_uses: List[Dict[str, Any]] = []
            c_tool: Optional[Dict[str, Any]] = None
            a_content: List[Dict[str, Any]] = []

            while True:
                chunk = q.get()
                if chunk is None:
                    break
                ct = chunk.get("type", "")

                if ct == "text":
                    c_text += chunk.get("text", "")
                elif ct == "tool_use_start":
                    c_tool = {"type": "tool_use", "id": chunk.get("id", ""),
                              "name": chunk.get("name", ""), "input_json": ""}
                    display_name = SCOUT_TOOL_DISPLAY_NAMES.get(chunk.get("name", ""), chunk.get("name", ""))
                    await on_event(AgentEvent(type="scout_progress", content=f"Scouting: {display_name}..."))
                elif ct == "tool_input_delta" and c_tool:
                    c_tool["input_json"] += chunk.get("json_delta", "")
                elif ct == "tool_use_end" and c_tool:
                    try:
                        inp = json.loads(c_tool["input_json"]) if c_tool["input_json"] else {}
                    except json.JSONDecodeError:
                        inp = {}
                    tu = {"type": "tool_use", "id": c_tool["id"], "name": c_tool["name"], "input": inp}
                    t_uses.append(tu)
                    a_content.append(tu)
                    c_tool = None
                elif ct == "usage_start":
                    usage = chunk.get("usage", {})
                    self._total_input_tokens += usage.get("input_tokens", 0)
                    self._cache_read_tokens += usage.get("cache_read_input_tokens", 0)
                elif ct == "message_end":
                    usage = chunk.get("usage", {})
                    self._total_output_tokens += usage.get("output_tokens", 0)

            t.join(timeout=5)

            if c_text:
                a_content.insert(0, {"type": "text", "text": c_text})
                all_text.append(c_text)

            if not t_uses:
                break

            scout_messages.append({"role": "assistant", "content": a_content})

            # Execute scout tools
            async def _exec_scout_tool(tu) -> tuple:
                try:
                    result = execute_tool(tu["name"], tu["input"], self.backend, self.working_directory)
                    out = result.content if isinstance(result, ToolResult) else str(result)
                    if len(out) > 4000:
                        out = out[:2000] + f"\n... ({len(out) - 2000} chars truncated) ..."
                    return tu["id"], out, False
                except Exception as e:
                    return tu["id"], str(e), True

            tasks = [_exec_scout_tool(tu) for tu in t_uses]
            results = await asyncio.gather(*tasks)

            tool_results: List[Dict[str, Any]] = []
            for tid, content, is_err in results:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tid,
                    "content": content,
                    **({"is_error": True} if is_err else {}),
                })

            scout_messages.append({"role": "user", "content": tool_results})

        context = "\n\n".join(all_text).strip()
        if context:
            await on_event(AgentEvent(type="scout_end", content="Scout complete"))
            return context

        await on_event(AgentEvent(type="scout_end", content="Scout complete (no context)"))
        return None

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
