"""
Plan generation and management for the coding agent.
Handles producing implementation plans via an agentic loop with read-only tools.
"""

import asyncio
import json
import logging
import os
import queue
import re
import threading
from typing import List, Dict, Any, Optional, Callable, Awaitable

from bedrock_service import GenerationConfig
from tools import SCOUT_TOOL_DEFINITIONS, execute_tool, ASK_USER_QUESTION_DEFINITION
from config import app_config, get_context_window, get_max_output_tokens

from .events import AgentEvent
from .prompts import _compose_system_prompt, SCOUT_TOOL_NAMES
from .plan import _strip_plan_preamble, _extract_plan

logger = logging.getLogger(__name__)


def _extract_plan_title(plan_text: str) -> str:
    """Extract a human-readable title from the plan's first markdown heading."""
    for line in plan_text.split("\n")[:10]:
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            # Strip common boilerplate prefixes the LLM tends to add
            for prefix in [
                "Implementation Plan:", "Implementation Plan —",
                "Implementation Plan for", "Implementation Plan",
                "Plan:", "Plan —", "Plan for",
                "Audit Findings:", "Audit:",
                "Phase 1:", "Step 1:",
                "Summary:", "Overview:",
            ]:
                if title.lower().startswith(prefix.lower()):
                    title = title[len(prefix):].strip().lstrip("—-:").strip()
                    break
            # Strip wrapping quotes or backticks
            title = title.strip('"\'`')
            if title:
                return title[:80]
    # Fallback: use first non-empty, non-tag line
    for line in plan_text.split("\n")[:5]:
        stripped = line.strip()
        if stripped and not stripped.startswith("<") and not stripped.startswith("```"):
            cleaned = stripped.lstrip("#").strip().strip('"\'`')
            if cleaned:
                return cleaned[:60]
    return "Plan"


class PlanningMixin:
    """Mixin providing plan generation and quality assessment.

    Expects the host class to provide:
    - self.service (BedrockService)
    - self.backend (Backend)
    - self.working_directory (str)
    - self.history (list)
    - self._cancelled (bool)
    - self._detected_language (str)
    - self._scout_context (str)
    - self._current_plan, _current_plan_text, _current_plan_decomposition (via core)
    - self._plan_file_path, _plan_text (via core)
    - self._todos (list) via ContextMixin
    - self._total_input_tokens, _total_output_tokens, _cache_read_tokens (int)
    - Methods: _run_scout, _refine_task, _load_project_docs, _generate_context_metadata,
      _effective_system_prompt, _get_generation_config_for_phase, _compose_user_content,
      _compress_tool_result, _decompose_plan_steps
    """

    async def run_plan(
        self,
        task: str,
        on_event: Callable[[AgentEvent], Awaitable[None]],
        request_question_answer: Optional[Callable[[str, Optional[str], str], Awaitable[str]]] = None,
        user_images: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[List[str]]:
        """
        Generate a plan for the task using an agentic loop with read-only tools.
        Returns a list of plan step strings, or None if planning fails/is cancelled.
        """
        self._cancelled = False
        self._current_plan = None
        self._current_plan_text = None
        self._plan_title = None

        scout_context = None
        has_semantic = "<semantic_context>" in task
        has_structure = "<project_structure>" in task
        # Skip scout when auto-context provides sufficient context — planning has
        # the same tools and will fill gaps itself.  Only run scout when we have
        # NO auto-context at all and it's the first message.
        if app_config.scout_enabled and len(self.history) == 0 and not has_semantic and not has_structure:
            scout_context = await self._run_scout(task, on_event)
            # Cap scout context to prevent oversized build-phase injection
            max_scout = self._ctx_scale(5000)
            if scout_context and len(scout_context) > max_scout:
                scout_context = scout_context[:max_scout] + "\n... (scout context truncated)"
            self._scout_context = scout_context
        elif has_semantic or has_structure:
            logger.info("Skipping scout — auto-context already contains semantic/structure context")

        await on_event(AgentEvent(type="phase_start", content="plan"))

        task_for_plan = task
        if app_config.task_refinement_enabled:
            refined = await self._refine_task(task, on_event)
            if refined:
                task_for_plan = refined

        self._task_complexity = self._estimate_task_complexity(task_for_plan)
        logger.info(f"Task complexity estimated: {self._task_complexity}")

        plan_system = _compose_system_prompt(
            "plan", self.working_directory, SCOUT_TOOL_NAMES,
            language=self._detected_language,
            complexity=self._task_complexity,
        )
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

        loop = asyncio.get_event_loop()
        plan_config = self._get_generation_config_for_phase("plan")

        plan_messages: List[Dict[str, Any]] = [
            {"role": "user", "content": self._compose_user_content(plan_user, user_images)}
        ]

        # Complexity-aware iteration limits — simple tasks don't need 50 rounds of file reading
        is_audit = any(kw in task.lower() for kw in ("audit", "review", "analyze", "analyse", "find all", "rip apart", "end to end", "security"))
        if is_audit:
            max_plan_iters = 40
        elif self._task_complexity in ("high",):
            max_plan_iters = 25
        else:
            max_plan_iters = 12
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
            all_text_blocks: List[str] = []
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
                        all_text_blocks.append(c_text)
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
            full_text = "\n\n".join(all_text_blocks).strip() if all_text_blocks else c_text
            return full_text, t_uses, a_content

        try:
            nudge_sent = False
            accumulated_texts: List[str] = []

            for plan_iter in range(max_plan_iters):
                if self._cancelled:
                    return None

                await on_event(AgentEvent(
                    type="scout_progress",
                    content=f"Planning — {'reading codebase' if plan_iter < 3 else 'analyzing & planning'}...",
                ))

                # Early nudge for non-audit tasks, later for audits
                if is_audit:
                    nudge_threshold = 20
                elif self._task_complexity in ("high",):
                    nudge_threshold = 8
                else:
                    nudge_threshold = 5

                if plan_iter >= nudge_threshold and not nudge_sent:
                    nudge_sent = True
                    plan_messages.append({
                        "role": "user",
                        "content": (
                            "You have gathered sufficient context. Write the complete plan document NOW. "
                            "You may read 1-2 more files if absolutely critical, but prioritize producing the plan. "
                            "Include all findings and steps — don't leave anything out."
                        ),
                    })

                plan_tools = (SCOUT_TOOL_DEFINITIONS + [ASK_USER_QUESTION_DEFINITION]) if request_question_answer else SCOUT_TOOL_DEFINITIONS
                iter_tools = plan_tools if plan_iter < max_plan_iters - 1 else None

                # Trim plan_messages before each API call to stay within context window
                self._trim_plan_messages(plan_messages)

                text, tool_uses, assistant_content = await _stream_plan_call(
                    plan_messages, iter_tools,
                )
                if self._cancelled:
                    return None

                if text.strip():
                    accumulated_texts.append(text.strip())

                plan_messages.append({"role": "assistant", "content": assistant_content})

                if not tool_uses:
                    plan_text = max(accumulated_texts, key=len) if accumulated_texts else ""
                    logger.info(
                        f"Plan loop ended at iter {plan_iter}: "
                        f"{len(accumulated_texts)} text blocks, "
                        f"sizes={[len(t) for t in accumulated_texts]}, "
                        f"selected={len(plan_text)} chars"
                    )
                    break

                # Split into clarifying questions vs read-only tools
                question_calls = [tu for tu in tool_uses if tu.get("name") == "AskUserQuestion"]
                other_calls = [tu for tu in tool_uses if tu.get("name") != "AskUserQuestion"]

                tool_results = []

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
                            await on_event(AgentEvent(type="tool_result", content=text_r[:200], data={"tool_use_id": tu["id"], "name": "AskUserQuestion", "success": True}))
                        except Exception as e:
                            text_r = f"Clarification failed or skipped: {e}"
                            tool_results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": text_r, "is_error": True})
                            await on_event(AgentEvent(type="tool_result", content=text_r, data={"tool_use_id": tu["id"], "name": "AskUserQuestion", "success": False}))
                    else:
                        text_r = "Clarification not available; proceed with your best assumption."
                        tool_results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": text_r, "is_error": False})
                        await on_event(AgentEvent(type="tool_result", content=text_r, data={"tool_use_id": tu["id"], "name": "AskUserQuestion", "success": True}))

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
                            data={"tool_use_id": tu["id"], "name": tu["name"], "success": tr.success},
                        ))

                plan_messages.append({"role": "user", "content": tool_results})

            # Force a conclusion if the loop ended without a plan
            if not plan_text:
                await on_event(AgentEvent(
                    type="scout_progress",
                    content="Planning: finalizing plan document...",
                ))
                if is_audit:
                    conclusion_prompt = (
                        "STOP reading files. Output the COMPLETE audit findings NOW.\n\n"
                        "Start directly with '# Audit Findings' — no preamble, no 'let me verify', "
                        "no commentary before the findings. Just the document.\n\n"
                        "Organize by severity (Critical > High > Medium > Low). For each finding include: "
                        "exact file path, line number(s), what the bug is, why it matters, and how to fix it.\n\n"
                        "End with a prioritized fix plan. Include ALL findings — do not omit anything you discovered."
                    )
                else:
                    conclusion_prompt = (
                        "STOP reading files. Output the COMPLETE implementation plan NOW.\n\n"
                        "Start directly with '# Implementation Plan' — no preamble, no 'let me verify', "
                        "no commentary before the plan. Just the plan document.\n\n"
                        "Include: Why, Approach, Affected Files table, numbered Steps with exact "
                        "file paths and function names, Edge Cases & Risks, and Verification commands. "
                        "Be thorough and specific."
                    )
                plan_messages.append({
                    "role": "user",
                    "content": conclusion_prompt,
                })
                final_text, _, final_content = await _stream_plan_call(
                    plan_messages, None,
                )
                if final_text:
                    accumulated_texts.append(final_text.strip())
                    plan_messages.append({"role": "assistant", "content": final_content})
                if accumulated_texts:
                    plan_text = max(accumulated_texts, key=len)

            # Fallback: check thinking blocks for plan content
            if len(plan_text) < 2000:
                best_thinking = ""
                for msg in plan_messages:
                    if msg.get("role") != "assistant":
                        continue
                    content = msg.get("content", [])
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "thinking":
                            thinking_text = block.get("thinking", "")
                            if len(thinking_text) > len(best_thinking):
                                best_thinking = thinking_text
                        elif block.get("type") == "text":
                            txt = block.get("text", "")
                            if len(txt) > len(plan_text):
                                plan_text = txt
                if best_thinking and len(best_thinking) > len(plan_text) * 2:
                    logger.info(
                        f"Plan text was {len(plan_text)} chars, found {len(best_thinking)} chars "
                        f"in thinking block — using thinking content as plan"
                    )
                    plan_text = best_thinking

            if not plan_text:
                await on_event(AgentEvent(type="error", content="Planning produced no output."))
                return None

            extracted = _extract_plan(plan_text)
            if extracted:
                plan_text = extracted

            steps = self._parse_plan_steps(plan_text)

            # Quality gate
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
                        "The plan is close but needs these specific additions:\n"
                        f"1) Add at least {min_steps} numbered implementation steps with exact file paths\n"
                        "2) Add a verification section (test/lint commands to run after changes)\n\n"
                        "Keep everything you already have — just add the missing parts. Output the complete plan:"
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
            self._current_plan_text = plan_text or ""
            self._current_plan_decomposition = self._decompose_plan_steps(steps)

            # Capture file contents read during planning so build phase can reuse them
            context_parts = []
            for msg in plan_messages:
                if msg.get("role") != "user":
                    continue
                content = msg.get("content", [])
                if not isinstance(content, list):
                    continue
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result" and not block.get("is_error"):
                        text = block.get("content", "")
                        if isinstance(text, str) and len(text) > 100:
                            context_parts.append(text[:3000])
            self._plan_context_summary = "\n---\n".join(context_parts)[:30000]

            # Only write a plan file if the plan has actionable steps —
            # avoids creating garbage files from conversational LLM responses.
            plan_file_path = None
            if steps:
                plan_file_path = self._write_plan_file(task, plan_text)
            self._plan_file_path = plan_file_path
            self._plan_text = plan_text or ""
            plan_title = getattr(self, "_plan_title", None) or _extract_plan_title(plan_text or "")
            self._plan_title = plan_title

            await on_event(AgentEvent(
                type="phase_plan",
                content="\n".join(steps),
                data={
                    "steps": steps,
                    "plan_text": plan_text,
                    "plan_file": plan_file_path,
                    "plan_title": plan_title,
                    "decomposition": self._current_plan_decomposition,
                },
            ))

            return steps

        except Exception as e:
            logger.error(f"Plan phase failed: {e}")
            await on_event(AgentEvent(type="error", content=f"Planning failed: {e}"))
            return None

    # ------------------------------------------------------------------
    # Plan-phase context trimming
    # ------------------------------------------------------------------

    def _trim_plan_messages(self, plan_messages: List[Dict[str, Any]]) -> None:
        """Trim the planning-phase message list to stay within the model's context window.

        Unlike the main agent loop (which trims self.history via HistoryMixin),
        the planning loop uses a local plan_messages list that can grow unboundedly
        across many tool-use iterations.  This method applies multi-tier trimming
        directly on that list, mutating it in place.
        """
        context_window = get_context_window(self.service.model_id)
        reserved_output = min(64_000, get_max_output_tokens(self.service.model_id) // 2)
        usable = max(1, context_window - reserved_output)

        def _est(msgs: List[Dict[str, Any]]) -> int:
            return sum(self._message_tokens(m) for m in msgs)

        current = _est(plan_messages)
        if current <= int(usable * 0.60):
            return

        # ── Tier 1: Drop thinking blocks from all but last 2 messages ──
        for msg in plan_messages[:-2]:
            content = msg.get("content")
            if isinstance(content, list):
                new_content = [
                    b for b in content
                    if not (isinstance(b, dict) and b.get("type") == "thinking")
                ]
                if len(new_content) < len(content):
                    msg["content"] = new_content

        current = _est(plan_messages)
        if current <= int(usable * 0.70):
            return

        # ── Tier 2: Truncate large tool results in older messages ──────
        for msg in plan_messages[:-2]:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for j, block in enumerate(content):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    text = block.get("content", "")
                    if isinstance(text, str) and len(text) > 2000:
                        content[j] = {**block, "content": text[:1000] + "\n... (trimmed) ..."}

        current = _est(plan_messages)
        if current <= int(usable * 0.80):
            return

        # ── Tier 3: Drop oldest middle messages, keep first + last N ───
        keep_tail = min(6, len(plan_messages))
        if len(plan_messages) > 1 + keep_tail:
            first = plan_messages[0]
            tail = plan_messages[-keep_tail:]
            plan_messages.clear()
            plan_messages.append(first)
            plan_messages.extend(tail)

        current = _est(plan_messages)
        if current <= int(usable * 0.90):
            return

        # ── Tier 4: Emergency — hard-truncate every remaining block ────
        for msg in plan_messages[:-1]:
            content = msg.get("content")
            if isinstance(content, list):
                for j, block in enumerate(content):
                    if isinstance(block, dict):
                        for key in ("content", "text", "thinking"):
                            val = block.get(key, "")
                            if isinstance(val, str) and len(val) > 500:
                                content[j] = {**block, key: val[:400] + " (trimmed)"}
            elif isinstance(content, str) and len(content) > 2000:
                msg["content"] = content[:1000] + " (trimmed)"

        logger.info(
            f"Plan context trimmed: ~{_est(plan_messages):,} tokens, "
            f"{len(plan_messages)} messages"
        )

    # ------------------------------------------------------------------
    # Plan parsing and quality assessment
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_plan_steps(plan_text: str) -> List[str]:
        """Extract actionable numbered steps from the plan document."""
        steps_section = None
        sec_match = re.search(
            r"(?ims)^##\s*(?:implementation\s+steps|steps)\s*$\n(.*?)(?=^##\s+|\Z)",
            plan_text,
        )
        if sec_match:
            steps_section = sec_match.group(1).strip()

        target = steps_section if steps_section else plan_text

        steps: List[str] = []
        for raw_line in target.split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            if re.match(r"^\d+[\.\)]\s+", line):
                steps.append(line)
            elif steps and not line.startswith("#") and not re.match(r"^\|.*\|$", line):
                if raw_line.startswith(" ") or raw_line.startswith("\t") or line.startswith(("-", "*")):
                    steps[-1] += " " + line

        if not steps:
            for raw_line in target.split("\n"):
                line = raw_line.strip()
                if re.match(r"^[-*]\s+\*\*\[(EDIT|CREATE|RUN|VERIFY|DELETE)\]\*\*", line, flags=re.IGNORECASE):
                    steps.append(line)

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
            " also ", " then ", " next ", " in addition ",
            " as well ", " after that ", " plus ",
        ]
        if any(m in t for m in markers):
            return True
        return t.count(" and ") >= 2

    @staticmethod
    def _is_actionable_plan_step(step: str) -> bool:
        """Filter out weak/meta steps like 'let me check X'."""
        s = (step or "").strip()
        if len(s) < 10:
            return False
        low = s.lower()
        weak_prefixes = (
            "ok", "okay", "let me", "now let me",
            "i will check", "check line", "todo",
        )
        if any(low.startswith(p) for p in weak_prefixes):
            return False
        verbs = (
            "edit", "update", "change", "replace", "add", "remove", "create",
            "run", "test", "lint", "verify", "refactor", "fix", "inject",
        )
        return any(v in low for v in verbs)

    def _estimate_task_complexity(self, task: str, plan_text: str = "", steps: Optional[List[str]] = None) -> str:
        """Estimate task complexity as 'low', 'medium', or 'high'.

        Signals that push towards 'high':
        - Many files mentioned (>5)
        - Many plan steps (>8)
        - Keywords: split, refactor, migrate, decompose, restructure, rename across, convert all
        - Large scope markers: "all files", "every", "across the codebase"
        - Multi-phase indicators: numbered sub-tasks with distinct targets
        """
        combined = f"{task}\n{plan_text}".lower()
        score = 0

        high_keywords = [
            "split", "decompose", "restructure", "migrate", "convert all",
            "rename across", "refactor into", "extract into", "move all",
            "rewrite", "overhaul", "reorganize",
        ]
        for kw in high_keywords:
            if kw in combined:
                score += 3

        scope_markers = ["all files", "every file", "across the codebase", "entire project", "each module"]
        for sm in scope_markers:
            if sm in combined:
                score += 2

        file_refs = re.findall(r"[A-Za-z0-9_\-./]+\.[A-Za-z]{1,5}", combined)
        unique_files = set(f for f in file_refs if "/" in f or f.count(".") == 1)
        if len(unique_files) > 10:
            score += 4
        elif len(unique_files) > 5:
            score += 2

        step_count = len(steps) if steps else 0
        if step_count > 12:
            score += 4
        elif step_count > 8:
            score += 2
        elif step_count > 5:
            score += 1

        if self._task_looks_multi_item(task):
            score += 1

        if score >= 6:
            return "high"
        if score >= 3:
            return "medium"
        return "low"

    def _plan_quality_sufficient(self, task: str, plan_text: str, steps: List[str]) -> bool:
        """Check if plan has minimum structure and actionable content.

        For high-complexity tasks, demands more specificity: multiple actionable
        steps with file paths and verification steps between phases.
        """
        if not plan_text or not steps:
            return False

        low = (plan_text or "").lower()
        has_structure = any(marker in low for marker in [
            "## steps", "## implementation", "## plan", "## approach",
            "### steps", "### implementation", "### plan",
            "**steps", "**implementation", "**approach",
            "1.", "1)", "- ",
        ])
        if not has_structure:
            return False

        actionable_count = sum(1 for s in steps if self._is_actionable_plan_step(s))

        complexity = getattr(self, "_task_complexity", "low")

        if complexity == "high":
            steps_with_paths = sum(
                1 for s in steps
                if self._is_actionable_plan_step(s)
                and re.search(r"[A-Za-z0-9_\-./]+\.[A-Za-z]{1,5}", s)
            )
            if steps_with_paths < 3:
                return False
            has_verify = any(
                re.search(r"\b(verify|test|lint|check|validate)\b", s, re.IGNORECASE)
                for s in steps
            )
            if not has_verify:
                return False
            return True

        if complexity == "medium":
            return actionable_count >= 2

        return actionable_count >= 1

    def _write_plan_file(self, task: str, plan_text: str) -> Optional[str]:
        """Write the plan as a markdown file under .bedrock-codex/plans/."""
        try:
            from datetime import datetime
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            cleaned = _strip_plan_preamble(plan_text)
            title = _extract_plan_title(cleaned)
            self._plan_title = title
            safe_title = re.sub(r'[<>:"/\\|?*]', '', title)[:60].strip().rstrip('.')
            if not safe_title:
                safe_title = "Plan"
            filename = f"{safe_title}.md"
            rel_path = f".bedrock-codex/plans/{filename}"
            if self.backend.file_exists(rel_path):
                filename = f"{safe_title} ({timestamp}).md"
                rel_path = f".bedrock-codex/plans/{filename}"
            self.backend.write_file(rel_path, cleaned)
            logger.info(f"Plan written to {rel_path}")
            return rel_path
        except Exception as e:
            logger.warning(f"Failed to write plan file: {e}")
            return None
