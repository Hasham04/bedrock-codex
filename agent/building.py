"""
Build and run orchestration for the coding agent.
Handles executing approved plans, direct runs, and post-build verification.
"""

import logging
import os
import re
from typing import List, Dict, Any, Optional, Callable, Awaitable

from bedrock_service import GenerationConfig
from config import app_config

from .events import AgentEvent
from .prompts import _format_build_system_prompt, _FUNCTIONAL_VERIFICATION

logger = logging.getLogger(__name__)


class BuildMixin:
    """Mixin providing build/run orchestration: executing plans and direct task execution.

    Expects the host class to provide:
    - self.service (BedrockService)
    - self.backend (Backend)
    - self.working_directory (str)
    - self.history (list)
    - self.max_iterations (int)
    - self.system_prompt (str)
    - self._cancelled (bool)
    - self._detected_language (str)
    - self._file_snapshots (dict)
    - self._deterministic_verification_done (bool)
    - self._verification_gate_attempts (int)
    - self._scout_context (str)
    - self._current_plan_text (str)
    - self._current_plan_decomposition (list)
    - All mixin methods: _compose_user_content, _decompose_plan_steps,
      _run_parallel_manager_workers, _select_impacted_tests, _get_generation_config_for_phase,
      _agent_loop, _load_project_docs, _run_scout, _detect_context_loss_risk
    """

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
        """Execute a previously approved plan.

        For high-complexity tasks with multiple phases, iterates over phases
        with per-phase context, verification, and history checkpointing.
        For simpler tasks, sends the full plan in one shot (original behavior).
        """
        self._cancelled = False
        if not self._file_snapshots:
            self._file_snapshots = {}
        self._deterministic_verification_done = False
        self._verification_gate_attempts = 0
        self._compact_stale_verification_messages()
        self._phase_summaries: List[str] = []

        saved_prompt = self.system_prompt
        complexity = getattr(self, '_task_complexity', 'low')
        self.system_prompt = _format_build_system_prompt(
            self.working_directory,
            language=self._detected_language,
            complexity=complexity,
        )

        plan_block = self._current_plan_text or "\n".join(plan_steps)
        decomposition = self._decompose_plan_steps(plan_steps)
        self._current_plan_decomposition = decomposition
        worker_insights = await self._run_parallel_manager_workers(task, decomposition)

        decomp_text = self._format_decomposition_summary(decomposition)

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

        use_phased = complexity == "high" and len(decomposition) > 1
        build_config = self._get_generation_config_for_phase("build", config)

        if use_phased:
            await self._run_phased_build(
                task, plan_block, decomposition, decomp_text,
                worker_insights, on_event, request_approval,
                build_config, user_images, request_question_answer,
            )
        else:
            user_content = self._build_single_shot_message(
                task, plan_block, decomposition, decomp_text,
                worker_insights, plan_steps,
            )
            user_content = self._compose_user_content(user_content, user_images)
            self.history.append({"role": "user", "content": user_content})
            await self._agent_loop(on_event, request_approval, build_config, request_question_answer=request_question_answer)

        verify_config = self._get_generation_config_for_phase("verify", config)
        await self._run_post_build_verification(on_event, request_approval, verify_config, request_question_answer=request_question_answer)

        self.system_prompt = saved_prompt
        await on_event(AgentEvent(type="phase_end", content="build"))

    # ------------------------------------------------------------------
    # Build helpers: single-shot vs phased
    # ------------------------------------------------------------------

    @staticmethod
    def _format_decomposition_summary(decomposition: List[Dict[str, Any]]) -> str:
        lines = []
        for batch in decomposition:
            step_ids = [str(s.get("index")) for s in batch.get("steps", [])]
            targets = ", ".join(batch.get("targets", [])[:5]) if batch.get("targets") else "n/a"
            strategy = batch.get("strategy", "direct_edit")
            deps = batch.get("depends_on", [])
            dep_str = f" (depends on phase {', '.join(str(d) for d in deps)})" if deps else ""
            lines.append(
                f"- Phase {batch.get('phase', batch.get('batch'))} [{batch.get('type')}] "
                f"strategy={strategy}: steps {', '.join(step_ids)} | targets: {targets}{dep_str}"
            )
        return "\n".join(lines) if lines else "- Single phase"

    def _build_single_shot_message(
        self, task, plan_block, decomposition, decomp_text,
        worker_insights, plan_steps,
    ) -> str:
        parts = []
        if self._scout_context:
            parts.append(f"<codebase_context>\n{self._scout_context}\n</codebase_context>")
        plan_ctx = getattr(self, '_plan_context_summary', '')
        if plan_ctx:
            parts.append(f"<plan_phase_context>\nKey files read during planning (do NOT re-read these):\n{plan_ctx}\n</plan_phase_context>")
        parts.append(f"<approved_plan>\n{plan_block}\n</approved_plan>")
        parts.append(f"<plan_decomposition>\n{decomp_text}\n</plan_decomposition>")
        if worker_insights:
            parts.append(f"<manager_worker_insights>\n{worker_insights}\n</manager_worker_insights>")
        parts.append(task)
        parts.append(
            "Execute this plan step by step.\n\n"
            "SETUP: Call TodoWrite with all plan steps (status: pending), then set the first to in_progress.\n\n"
            "FOR EACH STEP:\n"
            "1. State which step you are working on (e.g. 'Step 3: ...')\n"
            "2. Check if the target file is already in context — skip Read if so\n"
            "3. If not in context, Read the relevant section with offset/limit\n"
            "4. Make the changes with surgical precision — one logical change per Edit\n"
            "5. Re-read the changed section, run lint_file — fix any errors before proceeding\n"
            "6. Mark step completed in TodoWrite, set next to in_progress\n\n"
            "EFFICIENCY: Batch independent edits (different files) in one response. "
            "Batch lint_file calls after multiple edits.\n\n"
            "DEVIATIONS: If you discover something the plan missed — a dependency, an edge case, "
            "a better approach — adapt and state what you changed and why."
        )
        return "\n\n".join(parts)

    async def _run_phased_build(
        self,
        task: str,
        plan_block: str,
        decomposition: List[Dict[str, Any]],
        decomp_text: str,
        worker_insights: str,
        on_event: Callable[[AgentEvent], Awaitable[None]],
        request_approval: Callable[[str, str, Dict], Awaitable[bool]],
        build_config: Optional[GenerationConfig],
        user_images: Optional[List[Dict[str, Any]]],
        request_question_answer: Optional[Callable[..., Awaitable[str]]],
    ):
        """Execute a high-complexity plan phase by phase with checkpointing."""
        total_phases = len(decomposition)

        for phase_idx, phase in enumerate(decomposition):
            if self._cancelled:
                break

            phase_num = phase.get("phase", phase_idx + 1)
            phase_type = phase.get("type", "file_batch")
            strategy = phase.get("strategy", "direct_edit")
            targets = phase.get("targets", [])
            steps = phase.get("steps", [])

            await on_event(AgentEvent(
                type="phase_start",
                content=f"build_phase_{phase_num}",
                data={"phase": phase_num, "total": total_phases, "type": phase_type, "strategy": strategy},
            ))

            phase_msg = self._build_phase_context(
                task, plan_block, decomp_text, worker_insights,
                phase, phase_num, total_phases,
            )
            phase_msg = self._compose_user_content(phase_msg, user_images if phase_idx == 0 else None)
            self.history.append({"role": "user", "content": phase_msg})

            iters_per_phase = max(8, self.max_iterations // max(total_phases, 1))
            saved_max = self.max_iterations
            self.max_iterations = len(self.history) + iters_per_phase
            await self._agent_loop(on_event, request_approval, build_config, request_question_answer=request_question_answer)
            self.max_iterations = saved_max

            self._checkpoint_phase(phase_num, steps, targets)

            await on_event(AgentEvent(
                type="phase_end",
                content=f"build_phase_{phase_num}",
                data={"phase": phase_num, "total": total_phases},
            ))

    def _build_phase_context(
        self, task, plan_block, decomp_text, worker_insights,
        phase, phase_num, total_phases,
    ) -> str:
        """Compose a focused message for one phase of a phased build."""
        steps = phase.get("steps", [])
        targets = phase.get("targets", [])
        strategy = phase.get("strategy", "direct_edit")
        deps = phase.get("depends_on", [])

        parts = []

        if self._phase_summaries:
            parts.append(
                "<completed_phases>\n"
                + "\n".join(self._phase_summaries)
                + "\n</completed_phases>"
            )

        if phase_num == 1:
            if self._scout_context:
                parts.append(f"<codebase_context>\n{self._scout_context}\n</codebase_context>")
            plan_ctx = getattr(self, '_plan_context_summary', '')
            if plan_ctx:
                parts.append(f"<plan_phase_context>\nKey files read during planning (do NOT re-read these):\n{plan_ctx}\n</plan_phase_context>")
            parts.append(f"<full_plan>\n{plan_block}\n</full_plan>")
            parts.append(f"<plan_decomposition>\n{decomp_text}\n</plan_decomposition>")
            if worker_insights:
                parts.append(f"<manager_worker_insights>\n{worker_insights}\n</manager_worker_insights>")

        step_lines = "\n".join(f"  {s.get('index')}. {s.get('step', '')}" for s in steps)
        target_str = ", ".join(targets[:10]) if targets else "n/a"

        parts.append(
            f"**Phase {phase_num}/{total_phases}** — type: {phase.get('type')}, strategy: {strategy}\n"
            f"Targets: {target_str}\n"
            f"Steps:\n{step_lines}"
        )

        if deps:
            parts.append(f"Dependencies: phases {', '.join(str(d) for d in deps)} must be complete first.")

        if strategy == "scripted_transform":
            parts.append(
                "STRATEGY HINT: This phase involves many similar changes or large file transformations. "
                "Write a Python script and execute it via Bash rather than making individual edits. "
                "Read the source to understand the structure, then generate a transformation script."
            )

        parts.append(task)

        parts.append(
            f"Execute Phase {phase_num} now. "
            "Use TodoWrite to track these steps. "
            "After completing all steps in this phase, run lint_file on changed files and confirm readiness for the next phase."
        )

        return "\n\n".join(parts)

    def _checkpoint_phase(self, phase_num: int, steps: List[Dict], targets: List[str]):
        """Record a summary of a completed phase for inclusion in subsequent phases."""
        step_indices = [str(s.get("index", "?")) for s in steps]
        target_str = ", ".join(targets[:5]) if targets else "n/a"
        summary = (
            f"Phase {phase_num} COMPLETE — steps {', '.join(step_indices)} done. "
            f"Files touched: {target_str}."
        )
        self._phase_summaries.append(summary)
        logger.info(summary)

    async def _run_post_build_verification(
        self,
        on_event: Callable[[AgentEvent], Awaitable[None]],
        request_approval: Callable[[str, str, Dict], Awaitable[bool]],
        config: Optional[GenerationConfig] = None,
        request_question_answer: Optional[Callable[..., Awaitable[str]]] = None,
    ):
        """Run a final verification pass after the build loop completes."""
        if self._cancelled:
            return
        if self._deterministic_verification_done:
            return
        if not self._file_snapshots:
            return

        modified = [f for f in self._file_snapshots.keys() if os.path.isfile(f)]
        if not modified:
            self._deterministic_verification_done = True
            return

        def _snap_len(v):
            if isinstance(v, str): return len(v)
            if isinstance(v, dict) and "content" in v: return len(v["content"])
            return 0
        total_snapshot_size = sum(_snap_len(self._file_snapshots.get(f)) for f in modified)
        is_trivial = len(modified) <= 2 and total_snapshot_size < 500

        files_str = ", ".join(os.path.basename(f) for f in modified[:10])
        if len(modified) > 10:
            files_str += f" (+{len(modified) - 10} more)"

        include_functional_verification = app_config.verification_functional_testing

        if is_trivial:
            if include_functional_verification:
                verify_msg = (
                    "[VERIFICATION FOR CURRENT BUILD ONLY]\n"
                    f"Quick verification — Modified files: {files_str}\n\n"
                    "Run lint_file on changed files. Then write a quick functional verification:\n"
                    "For simple changes, a focused test that proves the change works correctly.\n"
                    "If the change is very minor, at minimum: "
                    'python -c "from <module> import <changed_thing>; print(\'Import + basic usage OK\')"\n'
                    "Confirm the task is complete."
                )
                max_extra_iters = 8
            else:
                verify_msg = (
                    "[VERIFICATION FOR CURRENT BUILD ONLY]\n"
                    f"Quick verification — Modified files: {files_str}\n\n"
                    "Run lint_file on changed files. If clean, confirm the task is complete. "
                    "Do NOT re-implement or re-do anything — the task is done. "
                    "Just verify and report briefly."
                )
                max_extra_iters = 3
        else:
            functional_section = _FUNCTIONAL_VERIFICATION if include_functional_verification else ""

            test_files_found = self._select_impacted_tests(modified)
            if test_files_found:
                test_section = (
                    f"\n\nImpacted tests selected:\n"
                    + "\n".join(f"  - {tf}" for tf in test_files_found[:10])
                    + "\nRun these impacted tests first, then run broader suite if needed."
                )
            else:
                test_section = "\n\nNo existing tests found for the modified code."

            verify_msg = (
                "[VERIFICATION FOR CURRENT BUILD ONLY]\n"
                f"Verification pass — Modified files: {files_str}\n\n"
                "1. Re-read each modified file and check for typos, missing imports, logic errors\n"
                "2. Run lint_file on each changed file and fix any errors\n"
                f"3. Run relevant tests if applicable{test_section}\n"
                "4. Write and run functional verification to prove your changes work correctly\n"
                "5. Briefly confirm the task is complete or flag concerns\n\n"
                f"{functional_section}\n"
                "IMPORTANT: Do NOT re-implement anything. The task is done. "
                "This is a verification pass — lint, test, verify functionality, and confirm."
            )
            max_extra_iters = 20 if include_functional_verification else 8

        self.history.append({"role": "user", "content": verify_msg})
        self._deterministic_verification_done = True

        saved_max = self.max_iterations
        self.max_iterations = saved_max + max_extra_iters
        await self._agent_loop(on_event, request_approval, config, request_question_answer=request_question_answer)
        self.max_iterations = saved_max

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
        """
        self._cancelled = False
        if not preserve_snapshots:
            self._file_snapshots = {}
        self._deterministic_verification_done = False
        self._verification_gate_attempts = 0
        self._compact_stale_verification_messages()

        if len(self.history) > 0 and self._detect_context_loss_risk(task):
            await on_event(AgentEvent(
                type="context_clarification",
                content="I may have lost some conversational context due to memory management. "
                "Could you clarify what you're referring to? For example, if you mentioned 'it' or 'that', "
                "what specific thing are you talking about?"
            ))

        scout_context = None
        _has_sem = "<semantic_context>" in task
        _has_str = "<project_structure>" in task
        # Skip scout when auto-context provides ANY of: semantic results, project structure.
        # The main agent has the same tools and can fill gaps itself.
        if enable_scout and app_config.scout_enabled and len(self.history) == 0 and not _has_sem and not _has_str:
            scout_context = await self._run_scout(task, on_event)
        elif _has_sem or _has_str:
            logger.info("Skipping scout — auto-context already contains semantic/structure context")

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

        self.history.append({"role": "user", "content": self._compose_user_content(user_content, user_images)})

        await self._agent_loop(on_event, request_approval, config, request_question_answer=request_question_answer)

    def _compact_stale_verification_messages(self):
        """Replace verbose verification messages from prior tasks with a compact note.

        Called at the start of run() and run_build() so the model doesn't confuse
        previous-task verification results with the new task.
        """
        markers = (
            "[SYSTEM — VERIFICATION",
            "[SYSTEM] Verification complete",
            "[SYSTEM] Verification found issues",
            "[VERIFICATION FOR CURRENT BUILD ONLY]",
            "Verification pass —",
            "Quick verification —",
        )
        for i, msg in enumerate(self.history):
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if not isinstance(content, str):
                continue
            if any(content.startswith(m) for m in markers):
                self.history[i] = {
                    "role": "user",
                    "content": "[Previous task verification — completed. Ignore for current task.]",
                }

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
