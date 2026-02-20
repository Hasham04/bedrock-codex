"""
Error recovery, confidence assessment, and contextual guidance for the coding agent.
Handles verification caching, uncertainty detection, and automatic error recovery.
"""

import ast
import hashlib
import logging
import os
import re
import time
from typing import List, Dict, Any, Optional, Callable, Awaitable

from .events import AgentEvent

logger = logging.getLogger(__name__)


class RecoveryMixin:
    """Mixin providing error recovery, confidence assessment, verification caching,
    and contextual guidance.

    Expects the host class to provide:
    - self.backend (Backend)
    - self.working_directory (str)
    - self._file_snapshots (dict) via ContextMixin
    - self._failure_pattern_cache (list) via VerificationMixin
    - self._verification_cache (dict) via core
    - self._last_verification_hashes (dict) via core
    - self._dependency_graph (dict) via core
    """

    # ------------------------------------------------------------------
    # Confidence parsing
    # ------------------------------------------------------------------

    def _parse_confidence_indicators(self, text: str) -> Dict[str, Any]:
        """Parse confidence indicators and uncertainty markers from model response."""
        confidence_info = {
            "confidence_level": None,
            "uncertainty_flags": [],
            "risk_indicators": [],
            "needs_review": False
        }

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

        risk_phrases = [
            r"could break", r"might fail", r"potential.*issue",
            r"breaking change", r"backward compatibility",
            r"security.*concern", r"edge case",
            r"needs.*testing", r"haven't.*tested"
        ]

        for phrase in risk_phrases:
            matches = re.findall(phrase, text, re.IGNORECASE)
            confidence_info["risk_indicators"].extend(matches)

        if (len(confidence_info["uncertainty_flags"]) > 2 or
            len(confidence_info["risk_indicators"]) > 1 or
            confidence_info["confidence_level"] == "low"):
            confidence_info["needs_review"] = True

        return confidence_info

    # ------------------------------------------------------------------
    # Verification caching
    # ------------------------------------------------------------------

    def _compute_file_hash(self, abs_path: str) -> Optional[str]:
        """Compute hash of file for caching purposes"""
        try:
            with open(abs_path, 'rb') as f:
                return hashlib.sha256(f.read()).hexdigest()[:16]
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

            if len(self._verification_cache) > 1000:
                oldest_keys = list(self._verification_cache.keys())[:100]
                for key in oldest_keys:
                    del self._verification_cache[key]

    def _get_incremental_verification_plan(self, modified_abs: List[str]) -> Dict[str, Any]:
        """Create smart verification plan based on caches and dependencies."""
        plan = {
            "files_to_verify": [],
            "cached_results": {},
            "verification_strategy": "full"
        }

        files_needing_verification = []
        cached_count = 0

        for abs_path in modified_abs:
            cached_result = self._get_cached_verification_result(abs_path)
            if cached_result and cached_result.get("success", False):
                plan["cached_results"][abs_path] = cached_result
                cached_count += 1
            else:
                files_needing_verification.append(abs_path)

        if cached_count == len(modified_abs):
            plan["verification_strategy"] = "minimal"
        elif cached_count > len(modified_abs) * 0.5:
            plan["verification_strategy"] = "incremental"
        else:
            plan["verification_strategy"] = "full"

        plan["files_to_verify"] = files_needing_verification

        return plan

    # ------------------------------------------------------------------
    # Uncertainty handling
    # ------------------------------------------------------------------

    def _handle_uncertain_response(self, response_text: str, confidence_info: Dict[str, Any]) -> str:
        """Generate follow-up guidance when the model expresses uncertainty."""
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

    # ------------------------------------------------------------------
    # Contextual guidance
    # ------------------------------------------------------------------

    def _generate_contextual_guidance(self, phase: str, context: Dict[str, Any]) -> str:
        """Generate adaptive, contextual guidance based on current phase and context."""
        guidance_parts = []

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

        current_hour = time.localtime().tm_hour
        context["working_late"] = current_hour < 6 or current_hour > 22

        context["verification_failures"] = len([
            p for p in (self._failure_pattern_cache or [])
            if p.get("timestamp", 0) > time.time() - 1800
        ])

        return context

    # ------------------------------------------------------------------
    # Automatic error recovery
    # ------------------------------------------------------------------

    async def _handle_verification_failure_with_recovery(
        self,
        failures: List[str],
        modified_abs: List[str],
        on_event: Callable[[AgentEvent], Awaitable[None]]
    ) -> Dict[str, Any]:
        """Intelligent error recovery: attempts multiple strategies based on failure patterns."""
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

                try:
                    compile(fixed_content, abs_path, 'exec')
                    await on_event(AgentEvent(
                        type="auto_fix_success",
                        content=f"🔧 **Auto-Fixed Syntax**: {os.path.relpath(abs_path, self.working_directory)} - {', '.join(fixes_applied)}",
                        data={"fixes": fixes_applied, "file": abs_path}
                    ))
                    return True
                except SyntaxError:
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

    # ------------------------------------------------------------------
    # Adaptive strategy escalation
    # ------------------------------------------------------------------

    def _record_step_failure(self, target: str):
        """Track how many times a step/file has failed during execution."""
        counts = getattr(self, "_step_failure_counts", {})
        counts[target] = counts.get(target, 0) + 1
        self._step_failure_counts = counts

    def _suggest_strategy_escalation(self, tool_results: list) -> Optional[str]:
        """Analyze recent tool failures and suggest a strategy escalation if warranted.

        Returns a guidance message string to inject into history, or None.
        Checks:
        - Repeated edit failures on the same file -> suggest scripted approach
        - Repeated failures of any kind on the same target -> suggest smaller steps
        - High context usage during a complex task -> suggest checkpointing
        """
        if not tool_results:
            return None

        counts = getattr(self, "_step_failure_counts", {})
        complexity = getattr(self, "_task_complexity", "low")
        suggestions: List[str] = []

        failed_files: Dict[str, int] = {}
        for tr in tool_results:
            if not isinstance(tr, dict):
                continue
            if tr.get("is_error") or not tr.get("content", ""):
                continue
            content = str(tr.get("content", ""))
            is_fail = (
                tr.get("is_error")
                or "not found" in content.lower()
                or "multiple occurrences" in content.lower()
                or "error" in content.lower()[:100]
            )
            if not is_fail:
                continue
            tool_id = tr.get("tool_use_id", "")
            target = self._extract_failure_target(content, tool_id)
            if target:
                failed_files[target] = failed_files.get(target, 0) + 1
                self._record_step_failure(target)

        for target, recent_fails in failed_files.items():
            total = counts.get(target, recent_fails)
            if total >= 2:
                suggestions.append(
                    f"Direct editing has failed {total} times for `{target}`. "
                    "Consider writing a Python script via Bash to perform this transformation "
                    "programmatically instead of individual edits."
                )
            if total >= 3:
                suggestions.append(
                    f"Repeated failures on `{target}` ({total} attempts). "
                    "Break this step into smaller sub-steps, or try a completely different approach. "
                    "Do NOT retry the same failing operation."
                )

        if complexity == "high":
            capacity = getattr(self, "_context_capacity_ratio", 0.0)
            if capacity > 0.6:
                suggestions.append(
                    "Context is over 60% full during a complex task. "
                    "Summarize completed work and indicate which files you're done with "
                    "so the context trimmer can reclaim space."
                )

        if not suggestions:
            return None

        return (
            "**Strategy Escalation** — The system detected repeated failures:\n"
            + "\n".join(f"- {s}" for s in suggestions)
        )

    @staticmethod
    def _extract_failure_target(content: str, tool_id: str) -> Optional[str]:
        """Extract the file path or target from a failed tool result."""
        m = re.search(r"(?:File|path)[:\s]+[`'\"]?([A-Za-z0-9_\-./]+\.[A-Za-z]{1,5})", content)
        if m:
            return m.group(1)
        m = re.search(r"([A-Za-z0-9_\-./]+\.[A-Za-z]{1,5})", content[:300])
        if m:
            return m.group(1)
        return None
