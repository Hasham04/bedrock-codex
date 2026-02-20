"""
Main CodingAgent class that orchestrates the tool-use loop with Bedrock.
Inherits capabilities from multiple mixins for a modular architecture:
  BuildMixin, PlanningMixin, ScoutMixin, ProgressiveVerificationMixin,
  RecoveryMixin, HistoryMixin, ExecutionMixin, ContextMixin, VerificationMixin.
"""

import asyncio
import logging
import os
import re
from typing import List, Dict, Any, Optional

from bedrock_service import BedrockService, GenerationConfig
from config import (
    app_config,
    model_config,
    supports_thinking,
    supports_adaptive_thinking,
    get_default_max_tokens,
    get_thinking_max_budget,
)
from backend import Backend, LocalBackend

from .events import AgentEvent
from .prompts import (
    _compose_system_prompt,
    _detect_project_language,
    AVAILABLE_TOOL_NAMES,
)

# Mixins — order determines MRO (left-to-right, depth-first)
from .building import BuildMixin
from .planning import PlanningMixin
from .scout import ScoutMixin
from .progressive_verification import ProgressiveVerificationMixin
from .recovery import RecoveryMixin
from .history import HistoryMixin
from .execution import ExecutionMixin
from .context import ContextMixin
from .verification import VerificationMixin

logger = logging.getLogger(__name__)


class CodingAgent(
    BuildMixin,
    PlanningMixin,
    ScoutMixin,
    ProgressiveVerificationMixin,
    RecoveryMixin,
    HistoryMixin,
    ExecutionMixin,
    ContextMixin,
    VerificationMixin,
):
    """
    Core coding agent that orchestrates the tool-use loop with Bedrock.

    Flow:
    1. User sends a task
    2. Agent calls Bedrock with messages + tool definitions
    3. Bedrock responds with text and/or tool_use blocks
    4. If tool_use: execute tools (with approval for writes), send results back, loop
    5. If end_turn: return the final text response
    """

    _PROJECT_DOCS_MAX_CHARS = 50_000

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
        self.working_directory = (working_directory if is_ssh else os.path.abspath(working_directory))
        self._backend_id = (f"ssh:{getattr(self.backend, '_host', '')}:{self.working_directory}" if is_ssh else "local")
        self.max_iterations = max_iterations
        self.history: List[Dict[str, Any]] = []
        self._detected_language = _detect_project_language(self.working_directory)
        self.system_prompt = _compose_system_prompt("direct", self.working_directory, AVAILABLE_TOOL_NAMES, language=self._detected_language)
        self._cancelled = False
        self._pending_guidance: List[str] = []
        self._guidance_interrupt = False

        # Initialize mixins (ContextMixin, VerificationMixin, ExecutionMixin, etc.)
        super().__init__()

        # Core-only state not managed by mixins
        self._file_cache: Dict[str, tuple] = {}
        self._history_len_at_last_call = 0
        self._consecutive_stream_errors: int = 0
        self._last_stream_error_sig: str = ""
        self._verification_cache: Dict[str, Dict[str, Any]] = {}
        self._dependency_graph: Dict[str, List[str]] = {}
        self._last_verification_hashes: Dict[str, str] = {}
        self._incremental_state: Dict[str, Any] = {}
        self._task_complexity: str = "low"
        self._phase_summaries: List[str] = []
        self._step_failure_counts: Dict[str, int] = {}

    # ------------------------------------------------------------------
    # Cancellation and guidance injection
    # ------------------------------------------------------------------

    def cancel(self):
        """Cancel the current agent run and kill any running command."""
        self._cancelled = True
        if self.backend:
            try:
                self.backend.cancel_running_command()
            except Exception:
                pass

    def inject_guidance(self, text: str):
        """Thread-safe: queue a user guidance message to be injected at the next iteration.
        Also sets _guidance_interrupt to abort any in-progress Bedrock stream."""
        self._pending_guidance.append(text)
        self._guidance_interrupt = True

    def _consume_guidance(self) -> Optional[str]:
        """Pop all queued guidance messages and combine into one string.
        Also clears the interrupt flag so the next stream proceeds normally."""
        if not self._pending_guidance:
            return None
        msgs = list(self._pending_guidance)
        self._pending_guidance.clear()
        self._guidance_interrupt = False
        return "\n\n".join(msgs)

    def reset(self):
        """Reset conversation history and all agent state."""
        self.history = []
        self._cancelled = False
        self._pending_guidance = []
        self._guidance_interrupt = False
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._cache_read_tokens = 0
        self._cache_write_tokens = 0
        self._approved_commands = set()
        self._history_len_at_last_call = 0
        self._running_summary = ""
        self._current_plan = None
        self._current_plan_text = None
        self._scout_context = None
        self._plan_context_summary = ""
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
        self._consecutive_stream_errors = 0
        self._last_stream_error_sig = ""
        self._pending_guidance = []

    # ------------------------------------------------------------------
    # File cache helpers
    # ------------------------------------------------------------------

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
        matches = self._STEP_RE.findall(text[:500])
        if matches:
            try:
                step_num = int(matches[-1])
                if 1 <= step_num <= len(self._current_plan):
                    old = self._plan_step_index
                    self._plan_step_index = step_num
                    if step_num != old:
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

    # ------------------------------------------------------------------
    # Project documentation loading
    # ------------------------------------------------------------------

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
                        continue
                    _add(f"project-docs/{name}", f"project-docs/{name}")
        except Exception as e:
            logger.debug(f"Could not list project-docs: {e}")

        if not parts:
            return ""
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Context metadata
    # ------------------------------------------------------------------

    def _generate_context_metadata(self, project_docs: str) -> str:
        """Generate strategic metadata about the provided project context."""
        lines = project_docs.split('\n')
        total_lines = len(lines)

        readme_count = len(re.findall(r'--- README.*---', project_docs, re.IGNORECASE))
        contributing_count = len(re.findall(r'--- CONTRIBUTING.*---', project_docs, re.IGNORECASE))
        doc_sections = len(re.findall(r'--- .*\.md.*---', project_docs))
        code_snippets = len(re.findall(r'```', project_docs))

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

        if is_truncated or max_chars_reached:
            metadata_parts.append("⚠️  **Completeness**: PARTIAL - Some docs may be truncated due to size limits")
            metadata_parts.append("💡 **Strategy**: Use tools to read specific files for complete details")
        else:
            metadata_parts.append("✅ **Completeness**: FULL - Complete project documentation loaded")

        metadata_parts.extend([
            "",
            "🎯 **How to Use This Context**:",
            "- This context provides project overview and patterns",
            "- For implementation details, read specific source files using tools",
            "- Look for existing patterns and utilities to reuse",
            "- Pay attention to architectural decisions and constraints",
        ])

        return "\n".join(metadata_parts)

    # ------------------------------------------------------------------
    # Structured response parsing
    # ------------------------------------------------------------------

    def _parse_structured_response(self, response_text: str) -> Dict[str, Any]:
        """Parse structured responses that follow the expected output format."""
        structured = {
            "overview": None,
            "analysis": None,
            "implementation": None,
            "verification": None,
            "confidence": None,
            "has_structure": False,
        }

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

    # ------------------------------------------------------------------
    # Dynamic system prompt composition
    # ------------------------------------------------------------------

    def _effective_system_prompt(self, base: str) -> str:
        """Return system prompt with dynamic context sections appended."""
        prompt = base

        rules = self._load_project_rules()
        if rules:
            prompt += "\n\n<project_rules>\nThese project-specific rules MUST be followed:\n\n" + rules + "\n</project_rules>"

        learned = self._failure_patterns_prompt()
        if learned:
            prompt += "\n\n<known_failure_patterns>\n" + learned + "\n</known_failure_patterns>"

        if self._todos:
            lines = ["<current_todos>", "Your task checklist (update with TodoWrite as you progress):"]
            for t in self._todos:
                s = t.get("status", "pending")
                c = (t.get("content") or "").strip()
                lines.append(f"  [{s}] {c}")
            lines.append("</current_todos>")
            prompt += "\n\n" + "\n".join(lines)

        reminders = self._gather_system_reminders()
        if reminders:
            prompt += "\n\n<system_reminders>\n" + "\n".join(f"- {r}" for r in reminders) + "\n</system_reminders>"

        return prompt

    # ------------------------------------------------------------------
    # Plan decomposition helpers
    # ------------------------------------------------------------------

    def _extract_step_targets(self, step: str) -> List[str]:
        """Extract likely file paths from a plan step line."""
        if not step:
            return []
        quoted = re.findall(r"`([^`]+)`", step)
        path_like = [q for q in quoted if "/" in q or "." in os.path.basename(q)]
        if path_like:
            return path_like[:3]
        toks = re.findall(r"[A-Za-z0-9_\-./]+\.[A-Za-z0-9]+", step)
        return toks[:3]

    def _decompose_plan_steps(self, steps: List[str]) -> List[Dict[str, Any]]:
        """Decompose plan steps into hierarchical phases with dependency tracking
        and strategy hints.

        Returns a list of phase dicts, each containing:
          - phase: int (1-based)
          - type: "file_batch" | "command_batch" | "scripted_transform"
          - strategy: "direct_edit" | "scripted_transform" | "generate_new"
          - steps: list of step items
          - targets: deduplicated file paths
          - depends_on: list of phase numbers this phase depends on
        """
        if not steps:
            return []

        # Build items with metadata
        items: List[Dict[str, Any]] = []
        for idx, step in enumerate(steps, start=1):
            s = step.strip()
            targets = self._extract_step_targets(s)
            is_run = bool(re.search(
                r"\b\[run\]\b|\brun\b|\bverify\b|\btest\b|\blint\b",
                s, flags=re.IGNORECASE,
            ))
            is_scripted = bool(re.search(
                r"\bscript\b|\bgenerate\b|\bbulk\b|\bbatch\b|\bextract.*into\b|\bsplit.*into\b",
                s, flags=re.IGNORECASE,
            ))
            items.append({
                "index": idx,
                "step": s,
                "targets": targets,
                "is_run": is_run,
                "is_scripted": is_scripted,
                "creates": self._step_creates_files(s),
            })

        # Build a map: file -> first phase that creates/writes it
        creates_map: Dict[str, int] = {}

        # Group items into phases: split at command steps and at target-set boundaries
        phases: List[Dict[str, Any]] = []
        current: Dict[str, Any] = {"type": "file_batch", "steps": [], "targets": []}

        for item in items:
            if item["is_run"]:
                if current["steps"]:
                    phases.append(current)
                    current = {"type": "file_batch", "steps": [], "targets": []}
                phases.append({
                    "type": "command_batch",
                    "steps": [item],
                    "targets": item["targets"],
                })
                continue

            # Start a new phase if targets are disjoint from current batch
            current_target_set = set(current["targets"])
            item_target_set = set(item["targets"])
            if (current["steps"]
                    and item_target_set
                    and current_target_set
                    and not item_target_set.intersection(current_target_set)):
                phases.append(current)
                current = {"type": "file_batch", "steps": [], "targets": []}

            current["steps"].append(item)
            current["targets"].extend(item["targets"])

        if current["steps"]:
            phases.append(current)

        # Assign phase numbers, deduplicate targets, detect strategy, track dependencies
        for p_idx, phase in enumerate(phases, start=1):
            phase["phase"] = p_idx
            # Legacy compat: also set "batch" key
            phase["batch"] = p_idx
            phase["targets"] = sorted(list(dict.fromkeys(phase.get("targets", []))))[:20]

            # Track which phases create files
            for item in phase["steps"]:
                for f in item.get("creates", []):
                    if f not in creates_map:
                        creates_map[f] = p_idx

            # Determine strategy hint
            has_scripted = any(it.get("is_scripted") for it in phase["steps"])
            many_targets = len(phase["targets"]) > 4
            if phase["type"] == "command_batch":
                phase["strategy"] = "direct_edit"
            elif has_scripted or (many_targets and getattr(self, "_task_complexity", "low") == "high"):
                phase["strategy"] = "scripted_transform"
                phase["type"] = "scripted_transform"
            else:
                phase["strategy"] = "direct_edit"

            phase["depends_on"] = []

        # Compute inter-phase dependencies: if phase P reads a file that phase Q creates, P depends on Q
        for phase in phases:
            for target in phase["targets"]:
                creator = creates_map.get(target)
                if creator and creator < phase["phase"] and creator not in phase["depends_on"]:
                    phase["depends_on"].append(creator)

        return phases

    @staticmethod
    def _step_creates_files(step: str) -> List[str]:
        """Detect if a step creates new files (via 'create', 'write', 'generate')."""
        low = step.lower()
        if not re.search(r"\b(create|write|generate|new file|add file)\b", low):
            return []
        quoted = re.findall(r"`([^`]+)`", step)
        return [q for q in quoted if "/" in q or "." in os.path.basename(q)][:3]

    # ------------------------------------------------------------------
    # Parallel manager-workers
    # ------------------------------------------------------------------

    async def _run_parallel_manager_workers(self, task: str, decomposition: List[Dict[str, Any]]) -> str:
        """Run parallel worker analyses and merge into manager insights.

        For 'scripted_transform' phases, workers produce actual transformation
        script outlines (higher token budget). For regular phases, workers
        produce concise execution guidance.
        """
        if not app_config.parallel_subagents_enabled:
            return ""
        eligible = [
            b for b in decomposition
            if b.get("type") in ("file_batch", "scripted_transform") and b.get("steps")
        ]
        if len(eligible) < 2:
            return ""

        max_workers = max(1, min(app_config.parallel_subagents_max_workers, len(eligible), 4))
        selected = eligible[:max_workers]
        loop = asyncio.get_event_loop()

        def _worker_prompt(batch: Dict[str, Any]) -> str:
            steps = "\n".join(f"- {s.get('step','')}" for s in batch.get("steps", [])[:8])
            targets = ", ".join(batch.get("targets", [])[:12]) or "n/a"
            strategy = batch.get("strategy", "direct_edit")

            if strategy == "scripted_transform":
                return (
                    "You are a worker agent for a scripted transformation lane.\n"
                    "This lane involves mechanical/repetitive changes across multiple files.\n"
                    "Produce a concrete Python script outline that performs these transformations.\n\n"
                    "Return with this exact format:\n"
                    "Script purpose:\n- ...\n"
                    "Script outline:\n```python\n# transformation script\n...\n```\n"
                    "Risks:\n- ...\nVerification:\n- ...\n\n"
                    f"Task:\n{task[:2000]}\n\n"
                    f"Lane phase #{batch.get('phase', batch.get('batch'))} "
                    f"[{strategy}] targets: {targets}\n"
                    f"Lane steps:\n{steps}\n"
                )
            return (
                "You are a worker agent for one execution lane.\n"
                "Return concise actionable guidance with this exact format:\n"
                "Edits:\n- ...\nRisks:\n- ...\nVerification:\n- ...\n\n"
                f"Task:\n{task[:2000]}\n\n"
                f"Lane phase #{batch.get('phase', batch.get('batch'))} "
                f"[{strategy}] targets: {targets}\n"
                f"Lane steps:\n{steps}\n"
            )

        async def _run_worker(batch: Dict[str, Any]) -> str:
            strategy = batch.get("strategy", "direct_edit")
            budget = 4000 if strategy == "scripted_transform" else 1800
            cfg = GenerationConfig(
                max_tokens=budget,
                enable_thinking=False,
                throughput_mode=model_config.throughput_mode,
            )
            sys_prompt = (
                "You produce transformation script outlines for a coding manager."
                if strategy == "scripted_transform"
                else "You produce terse worker execution guidance for a coding manager."
            )
            prompt = _worker_prompt(batch)
            def _call():
                res = self.service.generate_response(
                    messages=[{"role": "user", "content": prompt}],
                    system_prompt=sys_prompt,
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
        for idx, (batch, txt) in enumerate(zip(selected, worker_outputs), start=1):
            if not txt:
                continue
            strategy = batch.get("strategy", "direct_edit")
            cap = 3000 if strategy == "scripted_transform" else 2000
            merged_lines.append(f"### Worker lane {idx} [{strategy}]\n{txt[:cap]}")
        if not merged_lines:
            return ""
        return "\n\n".join(merged_lines)

    # ------------------------------------------------------------------
    # Test discovery and impact selection
    # ------------------------------------------------------------------

    def _discover_test_files(self, modified_paths: List[str]) -> List[str]:
        """Find test files that likely correspond to modified source files."""
        test_files = []
        seen = set()
        for abs_path in modified_paths:
            base = os.path.basename(abs_path)
            name, ext = os.path.splitext(base)
            dir_path = os.path.dirname(abs_path)

            candidates = [
                os.path.join(dir_path, f"test_{name}{ext}"),
                os.path.join(dir_path, f"{name}_test{ext}"),
                os.path.join(dir_path, f"{name}.test{ext}"),
                os.path.join(dir_path, f"{name}.spec{ext}"),
                os.path.join(dir_path, "tests", f"test_{name}{ext}"),
                os.path.join(dir_path, "test", f"test_{name}{ext}"),
                os.path.join(dir_path, "__tests__", f"{name}.test{ext}"),
                os.path.join(dir_path, "__tests__", f"{name}.spec{ext}"),
                os.path.join(os.path.dirname(dir_path), "tests", f"test_{name}{ext}"),
                os.path.join(os.path.dirname(dir_path), "test", f"test_{name}{ext}"),
            ]
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

        for tf in self._discover_test_files(modified_paths):
            if tf not in seen:
                impacted.append(tf)
                seen.add(tf)

        if not app_config.test_impact_selection_enabled:
            return impacted

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
                    for line in raw.split("\n"):
                        m = re.match(r"^([^:]+):", line)
                        if m:
                            p = m.group(1).strip()
                            rel = os.path.relpath(p, self.working_directory) if os.path.isabs(p) else p
                            if rel not in seen:
                                impacted.append(rel)
                                seen.add(rel)
                except Exception:
                    continue

        return impacted[:40]

    # ------------------------------------------------------------------
    # Generation config
    # ------------------------------------------------------------------

    def _default_config(self) -> GenerationConfig:
        """Create default generation config."""
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

    def _get_generation_config_for_phase(
        self, phase: str, base_config: Optional[GenerationConfig] = None,
    ) -> GenerationConfig:
        """Create phase-specific generation config with optimized sampling parameters."""
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
            config.temperature = 0.8
            config.top_p = 0.9
            config.enable_thinking = False
            config.thinking_budget = 0
        elif phase == "plan":
            config.temperature = 0.3
            config.top_p = 0.9
            config.enable_thinking = model_config.enable_thinking and supports_thinking_model
            config.thinking_budget = model_config.thinking_budget if supports_thinking_model else 0
            if supports_adaptive_thinking(model_id):
                config.adaptive_thinking_effort = "high"
        elif phase == "build":
            config.temperature = 0.1
            config.top_p = 0.95
            config.enable_thinking = model_config.enable_thinking and supports_thinking_model
            config.thinking_budget = model_config.thinking_budget if supports_thinking_model else 0
            if supports_adaptive_thinking(model_id):
                config.adaptive_thinking_effort = "high"
        elif phase == "verify":
            config.temperature = 0.1
            config.top_p = 0.95
            config.enable_thinking = model_config.enable_thinking and supports_thinking_model
            config.thinking_budget = (
                min(model_config.thinking_budget * 1.2, get_thinking_max_budget(model_id))
                if supports_thinking_model
                else 0
            )
            if supports_adaptive_thinking(model_id):
                config.adaptive_thinking_effort = "max"

        return config
