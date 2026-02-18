"""
Main CodingAgent class that orchestrates the tool-use loop with Bedrock.
Inherits capabilities from ContextMixin, VerificationMixin, and ExecutionMixin.
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
from config import (
    app_config,
    model_config,
    supports_thinking,
    supports_adaptive_thinking,
    supports_caching,
    get_context_window,
    get_max_output_tokens,
    get_default_max_tokens,
    get_cache_min_tokens,
)
from backend import Backend, LocalBackend

from .events import AgentEvent, PolicyDecision
from .prompts import (
    _compose_system_prompt, _format_build_system_prompt, _detect_project_language,
    AVAILABLE_TOOL_NAMES, SCOUT_TOOL_NAMES, SCOUT_TOOL_DISPLAY_NAMES,
    PHASE_MODULES, LANG_MODULES,
)
from .intent import classify_intent, needs_planning
from .plan import _strip_plan_preamble, _extract_plan
from .verification import VerificationMixin
from .context import ContextMixin
from .execution import ExecutionMixin

logger = logging.getLogger(__name__)


class CodingAgent(ExecutionMixin, ContextMixin, VerificationMixin):
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
        # Set core attributes before calling mixin __init__s
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

        # Initialize mixins (ContextMixin, VerificationMixin, ExecutionMixin)
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

    def cancel(self):
        """Cancel the current agent run and kill any running command."""
        self._cancelled = True
        # Kill any currently running subprocess / SSH command
        if self.backend:
            try:
                self.backend.cancel_running_command()
            except Exception:
                pass

    def inject_guidance(self, text: str):
        """Thread-safe: queue a user guidance message to be injected at the next iteration."""
        self._pending_guidance.append(text)

    def _consume_guidance(self) -> Optional[str]:
        """Pop all queued guidance messages and combine into one string."""
        if not self._pending_guidance:
            return None
        msgs = list(self._pending_guidance)
        self._pending_guidance.clear()
        return "\n\n".join(msgs)

    def reset(self):
        """Reset conversation history"""
        self.history = []
        self._cancelled = False
        self._pending_guidance = []
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

    def _preserve_conversational_context(self, messages: List[Dict[str, Any]]) -> str:
        """Extract recent conversational context that should be preserved during trimming.
        
        Looks for topics, commands, names, and pronoun antecedents from recent messages
        to help maintain conversational continuity after context compression.
        """
        context_items = []
        pronoun_indicators = ['it', 'that', 'this', 'them', 'those', 'he', 'she', 'they']
        command_indicators = ['run', 'execute', 'try', 'test', 'check', 'start', 'stop']
        
        # Look at last 6 messages for conversational context
        recent_messages = messages[-6:] if len(messages) > 6 else messages
        
        for i, msg in enumerate(recent_messages):
            content_str = self._extract_text_from_message(msg).strip()
            if not content_str:
                continue
                
            # Look for pronoun references that might become orphaned
            content_lower = content_str.lower()
            has_pronouns = any(word in content_lower for word in pronoun_indicators)
            has_commands = any(word in content_lower for word in command_indicators)
            
            if has_pronouns or has_commands:
                role = msg.get('role', 'unknown')
                snippet = content_str[:150]
                context_items.append(f"Recent {role}: {snippet}")
        
        if context_items:
            return "CONVERSATIONAL CONTEXT:\n" + "\n".join(context_items[-3:])  # Keep last 3 most relevant
        return ""

    def _detect_context_loss_risk(self, user_msg: str) -> bool:
        """Detect when a user message might reference lost conversational context.
        
        Returns True if the message contains pronouns without clear antecedents,
        suggesting the user is referring to something that may have been trimmed.
        """
        pronouns = ['it', 'that', 'this', 'them', 'those', 'he', 'she', 'they']
        command_refs = ['run it', 'try it', 'execute it', 'test it', 'check it', 'start it']
        
        msg_lower = user_msg.lower().strip()
        words = msg_lower.split()
        
        # Short messages with pronouns are high risk
        if len(words) <= 10:
            if any(pronoun in msg_lower for pronoun in pronouns):
                return True
            if any(cmd_ref in msg_lower for cmd_ref in command_refs):
                return True
        
        # Look for isolated pronouns at the start of sentences
        sentences = [s.strip() for s in user_msg.split('.') if s.strip()]
        for sentence in sentences:
            sentence_lower = sentence.lower()
            # Check if sentence starts with a pronoun
            first_words = sentence_lower.split()[:3]
            if first_words and any(pronoun in first_words for pronoun in pronouns):
                return True
        
        return False

    def _extract_text_from_message(self, msg: Dict[str, Any]) -> str:
        """Extract text content from a message structure."""
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        elif isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            return " ".join(text_parts)
        return ""

    def _assistant_signals_completion(self, assistant_text: str) -> bool:
        """Detect if the assistant is explicitly signaling task completion.
        
        Returns True if the text contains clear completion indicators,
        False if it's just a conversational response that should allow user followup.
        """
        if not assistant_text or len(assistant_text.strip()) < 10:
            return False
            
        text_lower = assistant_text.lower().strip()
        
        # Strong completion signals
        completion_phrases = [
            "task is complete", "task complete", "completed successfully",
            "all done", "finished", "implementation is complete", 
            "ready to go", "should be working now", "fixed the issue",
            "problem is resolved", "issue is resolved", "resolved the problem",
            "changes have been applied", "successfully implemented",
            "task has been completed", "work is done"
        ]
        
        # Look for completion signals
        if any(phrase in text_lower for phrase in completion_phrases):
            return True
            
        # Look for explicit "let me know if..." endings that suggest completion
        followup_phrases = [
            "let me know if you need", "let me know if there's",
            "feel free to", "if you need any", "anything else",
            "further assistance", "additional help"
        ]
        
        if any(phrase in text_lower for phrase in followup_phrases):
            return True
        
        # Check if the response is addressing a completely different topic
        # than what was in the last user message (suggests topic change)
        return False

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
            
            # Add recent conversational context for better continuity
            context_preservation = self._preserve_conversational_context(messages)
            if context_preservation:
                conversation_text = context_preservation + "\n\n" + conversation_text
            
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
                    "1. **Recent discussion**: What was being discussed in the last few messages — topics, commands, names, "
                    "and specific things the user or assistant referred to. Preserve enough context that pronouns like "
                    "'it', 'that', 'this', 'them' can be resolved by reading this summary.\n"
                    "2. **Task**: What the user asked for (exact goal).\n"
                    "3. **Files touched**: Paths read, edited, or created (with one-line reason each).\n"
                    "4. **Decisions**: Key design/implementation choices and why.\n"
                    "5. **Current state**: What is done, what remains, any errors or blockers.\n"
                    "6. **Next steps**: What the agent should do next (concrete).\n"
                    "CRITICAL: Section 1 is the most important — without it the agent cannot understand follow-up messages. "
                    "Be concise but reconstruction-grade. Use bullet points. Max 600 words."
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

        With server-side context editing enabled (Anthropic beta), the API
        automatically clears old tool results and thinking blocks.  Our
        client-side trimming is therefore a safety net — not the primary
        compaction strategy.  We use conservative thresholds that maximise
        the usable 200K window instead of the old 41%-of-window approach.

        Output headroom: most agent turns produce 2-10K tokens of text +
        tool calls.  We reserve 20K per turn (generous) instead of the old
        64K, which was based on the model's *max* capacity — not typical use.

        Tier 1 (>75%): Gentle — compress bulky tool results/text.
        Tier 2 (>88%): Aggressive — summarize old messages, drop old thinking.
        Tier 3 (>95%): Emergency — drop to summary + recent messages.
        """
        context_window = get_context_window(self.service.model_id)
        # Reserve enough for a single turn's output (thinking + text + tool calls).
        # 20K is generous — most turns use 2-10K.  The model's 128K *max* output
        # capacity is irrelevant here; that's a per-turn ceiling, not what it
        # actually produces.
        reserved_output = min(20_000, get_max_output_tokens(self.service.model_id))
        usable = max(1, context_window - reserved_output)  # ~180K for Opus 4.6
        tier1_limit = int(usable * 0.75)  # ~135K — gentle compression
        tier2_limit = int(usable * 0.88)  # ~158K — aggressive summarization
        tier3_limit = int(usable * 0.95)  # ~171K — emergency drop

        current = self._current_token_estimate()
        if current <= tier1_limit:
            return  # plenty of room

        hot_files = self._extract_file_paths_from_history()
        safe_tail = min(8, len(self.history))

        # ── Tier 1: Gentle compression ────────────────────────────────
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

                if btype == "tool_result":
                    text = block.get("content", "")
                    if isinstance(text, str) and len(text) > 400:
                        tool_name = self._find_tool_name_for_result(
                            block.get("tool_use_id", ""), i
                        )
                        is_hot = any(hp in text[:500] for hp in hot_files) if hot_files else False
                        compressed = self._compress_tool_result(text, tool_name, is_hot)
                        content[j] = {**block, "content": compressed}

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

        # ── Tier 2: Aggressive — summarize old messages ───────────────
        logger.info(f"Context tier 2: ~{current:,} tokens > {tier2_limit:,}. Summarizing.")

        # Drop old thinking blocks entirely (not just the text — removing the
        # whole block avoids sending corrupted signature/thinking mismatches
        # to the API which can cause rejections).  The last assistant turn's
        # thinking is always preserved (inside safe_tail).
        for i in range(max(0, len(self.history) - safe_tail)):
            msg = self.history[i]
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            msg["content"] = [
                b for b in content
                if not (isinstance(b, dict) and b.get("type") == "thinking")
            ]

        ratio = current / tier2_limit
        if ratio > 3:
            keep_last = min(10, len(self.history))
        elif ratio > 1.5:
            keep_last = min(14, len(self.history))
        else:
            keep_last = min(18, len(self.history))

        keep_first = 1
        if len(self.history) > keep_first + keep_last:
            old_messages = self.history[keep_first:-keep_last]
            summary = self._summarize_old_messages(old_messages)

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

        # ── Tier 3: Emergency — drop everything non-essential ─────────
        logger.info(f"Context tier 3 emergency: ~{current:,} tokens > {tier3_limit:,}")

        # Drop all thinking blocks from all messages except the very last
        for msg in self.history[:-1]:
            content = msg.get("content")
            if isinstance(content, list):
                msg["content"] = [
                    b for b in content
                    if not (isinstance(b, dict) and b.get("type") == "thinking")
                ]
        current = self._total_history_tokens()

        if current > tier3_limit:
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
            first = self.history[0]
            last_two = self.history[-2:]
            summary_msg = {"role": "user", "content": self._running_summary or "(earlier work trimmed)"}
            self.history = [first, summary_msg] + last_two
            current = self._total_history_tokens()

        if current > tier3_limit:
            for msg in self.history:
                content = msg.get("content")
                if isinstance(content, list):
                    # Drop thinking from everything including last message as last resort
                    msg["content"] = [
                        b for b in content
                        if not (isinstance(b, dict) and b.get("type") == "thinking")
                    ]
                    for j, block in enumerate(msg["content"]):
                        if isinstance(block, dict):
                            for key in ("content", "text"):
                                val = block.get(key, "")
                                if isinstance(val, str) and len(val) > 100:
                                    msg["content"][j] = {**block, key: val[:80] + " (trimmed)"}
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
        _consecutive_light_iters = 0  # track iterations with 0-1 tool calls (winding down)

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

                # Emit tool_call events so the frontend shows the tool carousel
                for tu in result.tool_uses:
                    await on_event(AgentEvent(
                        type="tool_call",
                        content=tu.name,
                        data={"name": tu.name, "input": tu.input, "id": tu.id},
                    ))

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

                    # Emit tool_result event so the frontend updates the tool block status
                    await on_event(AgentEvent(
                        type="tool_result",
                        content=tu.name,
                        data={
                            "tool_use_id": tu.id,
                            "success": tr.success,
                            "output": text[:500] if text else "",
                        },
                    ))

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

                # Early exit: if LLM used only 1 tool for 2 consecutive turns,
                # it's winding down — break to save time
                if len(result.tool_uses) <= 1:
                    _consecutive_light_iters += 1
                else:
                    _consecutive_light_iters = 0
                if _consecutive_light_iters >= 2 and result.content and len(result.content) > 200:
                    await on_event(AgentEvent(
                        type="scout_end",
                        content=f"Scout finished ({scout_iteration} iterations, early exit)",
                    ))
                    return result.content.strip()

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
        self._current_plan_text = None

        # Run scout for first message — skip if auto-context already has rich context
        scout_context = None
        has_semantic = "<semantic_context>" in task
        has_structure = "<project_structure>" in task
        if app_config.scout_enabled and len(self.history) == 0 and not (has_semantic and has_structure):
            scout_context = await self._run_scout(task, on_event)
            self._scout_context = scout_context  # cache for build phase
        elif has_semantic or has_structure:
            logger.info("Skipping scout — auto-context already contains semantic/structure context")

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
            nudge_sent = False  # only nudge once
            accumulated_texts: List[str] = []

            for plan_iter in range(max_plan_iters):
                if self._cancelled:
                    return None

                await on_event(AgentEvent(
                    type="scout_progress",
                    content=f"Planning — {'reading codebase' if plan_iter < 3 else 'analyzing & planning'}...",
                ))

                # After many iterations without concluding, nudge the model.
                # Use a higher threshold for audit/analysis tasks that need more exploration.
                nudge_threshold = 30 if any(kw in task.lower() for kw in ("audit", "review", "analyze", "analyse", "find all", "rip apart", "end to end", "security")) else 15
                if plan_iter >= nudge_threshold and not nudge_sent:
                    nudge_sent = True
                    plan_messages.append({
                        "role": "user",
                        "content": (
                            "You have read many files and should have a strong understanding by now. "
                            "When you are ready, write the complete plan document. You may read a few "
                            "more files if truly needed, but prioritize producing the plan. "
                            "Make sure all your findings are included — don't leave anything out."
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

                if text.strip():
                    accumulated_texts.append(text.strip())

                plan_messages.append({"role": "assistant", "content": assistant_content})

                if not tool_uses:
                    # Use the longest accumulated text block as the plan.
                    # The model often outputs the full plan/audit alongside a
                    # tool call (e.g. TodoWrite), then follows up with just a
                    # short summary. The longest block is the actual plan.
                    plan_text = max(accumulated_texts, key=len) if accumulated_texts else ""
                    logger.info(
                        f"Plan loop ended at iter {plan_iter}: "
                        f"{len(accumulated_texts)} text blocks, "
                        f"sizes={[len(t) for t in accumulated_texts]}, "
                        f"selected={len(plan_text)} chars"
                    )
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

            # ── Force a conclusion if the loop ended without a plan ──
            if not plan_text:
                await on_event(AgentEvent(
                    type="scout_progress",
                    content="Planning: finalizing plan document...",
                ))
                is_audit = any(kw in task.lower() for kw in ("audit", "review", "analyze", "analyse", "find all", "rip apart", "end to end", "security"))
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
                    plan_messages, None,  # no tools
                )
                if final_text:
                    accumulated_texts.append(final_text.strip())
                    plan_messages.append({"role": "assistant", "content": final_content})
                # Pick the longest text block — the actual plan, not a brief summary
                if accumulated_texts:
                    plan_text = max(accumulated_texts, key=len)

            # Fallback: if plan_text is too short, the actual plan may be in
            # thinking blocks (extended thinking puts substantive content there,
            # with only brief commentary in text blocks). Scan all assistant
            # messages for the longest thinking block that looks like a plan.
            if len(plan_text) < 500:
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
                        "Your plan needs more detail to be executable. You are still in PLANNING mode - do not start implementing yet.\n\n"
                        "IMPROVE THE PLAN with these requirements:\n"
                        "1) Include all required sections: Why, Approach, Affected Files, Checklist, Implementation Steps, Verification.\n"
                        f"2) Provide at least {min_steps} detailed, numbered Implementation Steps.\n"
                        "3) Each step must specify: exact file path + target function/class/method + precise change description.\n"
                        "4) Remove vague language like 'let me check', 'I will look at' - be concrete and actionable.\n"
                        "5) If the request has multiple parts, address each part explicitly in both Checklist and Steps.\n"
                        "6) Remember: another engineer must be able to follow your plan without asking questions.\n\n"
                        "Output the COMPLETE improved plan now:"
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
        """Check if plan has minimum structure and actionable content."""
        if not plan_text or not steps:
            return False
        
        # Accept plans with any reasonable structure
        low = (plan_text or "").lower()
        has_structure = any(marker in low for marker in [
            "## steps", "## implementation", "## plan", "## approach", "1.", "- "
        ])
        if not has_structure:
            return False

        # Require at least one actionable step
        actionable_count = sum(1 for s in steps if self._is_actionable_plan_step(s))
        return actionable_count >= 1

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
            cleaned = _strip_plan_preamble(plan_text)
            self.backend.write_file(rel_path, cleaned)
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

        # Switch to the build-specific system prompt for plan execution
        saved_prompt = self.system_prompt
        self.system_prompt = _format_build_system_prompt(self.working_directory, language=self._detected_language)

        # Build the user message with the approved plan and scout context
        # Use full plan text instead of just steps for better context
        plan_block = self._current_plan_text or "\n".join(plan_steps)
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

        # Check for potential context loss (pronouns without antecedents)
        if len(self.history) > 0 and self._detect_context_loss_risk(task):
            await on_event(AgentEvent(
                type="context_clarification", 
                content="I may have lost some conversational context due to memory management. "
                "Could you clarify what you're referring to? For example, if you mentioned 'it' or 'that', "
                "what specific thing are you talking about?"
            ))

        # Run scout for first message — skip if auto-context already has rich context
        scout_context = None
        _has_sem = "<semantic_context>" in task
        _has_str = "<project_structure>" in task
        if enable_scout and app_config.scout_enabled and len(self.history) == 0 and not (_has_sem and _has_str):
            scout_context = await self._run_scout(task, on_event)
        elif _has_sem or _has_str:
            logger.info("Skipping scout — auto-context already contains semantic/structure context")

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

