"""
Progressive multi-stage verification pipeline for the coding agent.
Handles static analysis, semantic validation, testing, quality assessment,
and confidence scoring with adaptive quality gates.
"""

import asyncio
import logging
import os
import re
import shlex
import time
from typing import List, Dict, Any, Callable, Awaitable

from tools import execute_tool
from config import app_config

from .events import AgentEvent

logger = logging.getLogger(__name__)


class ProgressiveVerificationMixin:
    """Mixin providing multi-stage progressive verification and deterministic gates.

    Expects the host class to provide:
    - self.service (BedrockService)
    - self.backend (Backend)
    - self.working_directory (str)
    - self._file_snapshots (dict) via ContextMixin
    - self._todos (list) via ContextMixin
    - self._verification_cache, _last_verification_hashes (dict) via core
    - Methods from RecoveryMixin: _get_incremental_verification_plan,
      _cache_verification_result, _handle_verification_failure_with_recovery,
      _assess_context_for_guidance, _generate_contextual_guidance
    - Methods from VerificationMixin: _record_failure_pattern
    - Methods from core: _select_impacted_tests, _verification_orchestrator_commands
    """

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

        if prof["python"]:
            py_files = [shlex.quote(f) for f in rel_files if f.endswith((".py", ".pyi"))][:40]
            if py_files:
                cmds.append("python -m py_compile " + " ".join(py_files))
            if self.backend.file_exists("pyproject.toml") or self.backend.file_exists("ruff.toml") or self.backend.file_exists(".ruff.toml"):
                cmds.append("ruff check " + " ".join(py_files or ["."]))
            elif self.backend.file_exists(".flake8") or self.backend.file_exists("setup.cfg"):
                cmds.append("flake8 " + " ".join(py_files or ["."]))

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

        if prof["go"]:
            cmds.append("go test ./...")
        if prof["rust"] and self.backend.file_exists("Cargo.toml"):
            cmds.append("cargo test -q")

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
        on_event: Callable[[AgentEvent], Awaitable[None]],
    ) -> Dict[str, Any]:
        """
        Enhanced multi-stage verification pipeline.

        Stages:
        1. Static Analysis — fast syntax, import, style checks
        2. Semantic Validation — logic patterns, security
        3. Dynamic Testing — unit tests with impact analysis
        4. Quality Assessment — complexity, maintainability
        5. Confidence Scoring — risk assessment
        """
        verification_result = {
            "success": True,
            "confidence_score": 0.0,
            "progressive_enabled": True,
            "stage_results": {},
            "recommendations": [],
            "failures": [],
        }

        try:
            verification_plan = self._get_incremental_verification_plan(modified_abs)

            await on_event(AgentEvent(
                type="verification_plan",
                content=(
                    f"📋 **Verification Plan**: {verification_plan['verification_strategy'].title()} strategy - "
                    f"{len(verification_plan['files_to_verify'])} files to verify, "
                    f"{len(verification_plan['cached_results'])} cached"
                ),
                data=verification_plan,
            ))

            for abs_path, cached_result in verification_plan["cached_results"].items():
                verification_result["stage_results"][f"cached_{abs_path}"] = cached_result

            files_to_verify = verification_plan["files_to_verify"]
            if not files_to_verify:
                verification_result["success"] = True
                verification_result["confidence_score"] = 0.95
                verification_result["recommendations"].append("✅ All files passed cached verification")
                return verification_result

            # Stage 1: Static Analysis
            static_result = await self._run_static_analysis_stage(files_to_verify, on_event)
            verification_result["stage_results"]["static"] = static_result

            if not static_result["success"] and static_result.get("critical", False):
                verification_result["success"] = False
                verification_result["failures"].extend(static_result.get("failures", []))
                return verification_result

            # Stage 2: Semantic Validation
            semantic_result = await self._run_semantic_validation_stage(modified_abs, on_event)
            verification_result["stage_results"]["semantic"] = semantic_result

            # Stage 3: Dynamic Testing
            testing_result = await self._run_testing_stage(modified_abs, on_event)
            verification_result["stage_results"]["testing"] = testing_result

            # Stage 4: Quality Assessment
            quality_result = await self._run_quality_assessment_stage(modified_abs, on_event)
            verification_result["stage_results"]["quality"] = quality_result

            # Stage 5: Confidence Scoring
            confidence_result = self._calculate_verification_confidence(verification_result)
            verification_result.update(confidence_result)

            if verification_result["success"]:
                for abs_path in files_to_verify:
                    file_result = {
                        "success": True,
                        "timestamp": time.time(),
                        "confidence_score": verification_result["confidence_score"],
                        "stage": "progressive_verification",
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
        on_event: Callable[[AgentEvent], Awaitable[None]],
    ) -> Dict[str, Any]:
        """Stage 1: Fast static analysis — syntax, imports, basic linting."""
        loop = asyncio.get_event_loop()
        stage_result = {
            "success": True,
            "critical": False,
            "failures": [],
            "warnings": [],
            "files_checked": len(modified_abs),
        }

        await on_event(AgentEvent(
            type="verification_stage",
            content="🔍 **STAGE 1: Static Analysis** - Checking syntax, imports, and code style...",
            data={"stage": "static", "total_files": len(modified_abs)},
        ))

        for abs_path in modified_abs:
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

            if not lint_result.success:
                failure_msg = f"lint_file {rel_path}: {lint_result.output[:800]}"
                stage_result["failures"].append(failure_msg)
                if any(term in lint_result.output.lower() for term in ["syntax error", "invalid syntax", "indentation error"]):
                    stage_result["critical"] = True

            await on_event(AgentEvent(
                type="tool_result",
                content=lint_result.output if lint_result.success else f"❌ {lint_result.output}",
                data={
                    "tool_name": "lint_file",
                    "tool_use_id": f"static-{rel_path}",
                    "success": lint_result.success,
                    "verification_stage": "static",
                },
            ))

        stage_result["success"] = len(stage_result["failures"]) == 0
        return stage_result

    async def _run_semantic_validation_stage(
        self,
        modified_abs: List[str],
        on_event: Callable[[AgentEvent], Awaitable[None]],
    ) -> Dict[str, Any]:
        """Stage 2: Semantic validation — security patterns and code-quality checks."""
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
        on_event: Callable[[AgentEvent], Awaitable[None]],
    ) -> Dict[str, Any]:
        """Stage 3: Dynamic testing with impact analysis."""
        stage_result = {
            "success": True,
            "failures": [],
            "tests_run": 0,
            "coverage_impact": None,
        }

        await on_event(AgentEvent(
            type="verification_stage",
            content="🧪 **STAGE 3: Dynamic Testing** - Running impacted tests...",
            data={"stage": "testing", "total_files": len(modified_abs)},
        ))

        try:
            test_cmds = self._verification_orchestrator_commands(modified_abs)
            test_cmds = [cmd for cmd in test_cmds if "pytest" in cmd or "test" in cmd]

            if test_cmds:
                loop = asyncio.get_event_loop()
                for idx, cmd in enumerate(test_cmds[:3], 1):
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
                            "command": cmd,
                        },
                    ))

                    if not test_result.success:
                        stage_result["failures"].append(f"{cmd}: {test_result.output[:800]}")
                    stage_result["tests_run"] += 1
            else:
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
                            "verification_stage": "testing",
                        },
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
        on_event: Callable[[AgentEvent], Awaitable[None]],
    ) -> Dict[str, Any]:
        """Stage 4: Quality assessment — complexity, maintainability."""
        stage_result = {
            "success": True,
            "complexity_score": 0.0,
            "maintainability_score": 0.0,
            "quality_warnings": [],
        }

        await on_event(AgentEvent(
            type="verification_stage",
            content="📊 **STAGE 4: Quality Assessment** - Analyzing code quality metrics...",
            data={"stage": "quality", "total_files": len(modified_abs)},
        ))

        for abs_path in modified_abs:
            if abs_path.endswith(".py"):
                rel_path = os.path.relpath(abs_path, self.working_directory)
                try:
                    content = self.backend.read_file(rel_path)
                    if not content:
                        continue
                    lines = content.split("\n")
                    line_count = len(lines)
                    if line_count > 500:
                        stage_result["quality_warnings"].append(f"{rel_path}: Large file ({line_count} lines)")

                    complexity = 0
                    for line in lines:
                        stripped = line.strip()
                        if stripped.startswith(("#", '"', "'")):
                            continue
                        complexity += stripped.count(" and ") + stripped.count(" or ")
                        complexity += sum(
                            1
                            for k in ("if ", "elif ", "for ", "while ", "except:", "except ", "with ")
                            if k in stripped
                        )
                    if complexity > 50:
                        stage_result["quality_warnings"].append(f"{rel_path}: High complexity (~{complexity} decision points)")

                    seen: Dict[str, int] = {}
                    for ln in lines:
                        n = ln.strip()
                        if len(n) > 15 and not n.startswith("#"):
                            seen[n] = seen.get(n, 0) + 1
                    dupes = [k for k, v in seen.items() if v > 3]
                    if len(dupes) > 5:
                        stage_result["quality_warnings"].append(f"{rel_path}: Many repeated lines (possible duplication)")

                    if content.count("except:") > 0:
                        stage_result["quality_warnings"].append(f"{rel_path}: Bare except clauses detected")
                    if content.count("# TODO") + content.count("# FIXME") > 5:
                        stage_result["quality_warnings"].append(f"{rel_path}: Many TODOs/FIXMEs")
                except Exception as e:
                    logger.debug(f"Quality assessment failed for {rel_path}: {e}")
        return stage_result

    def _calculate_verification_confidence(self, verification_result: Dict[str, Any]) -> Dict[str, Any]:
        """Stage 5: Calculate overall confidence score and recommendations."""
        stages = verification_result["stage_results"]

        confidence_score = 1.0
        recommendations = []

        static_result = stages.get("static", {})
        if not static_result.get("success", True):
            if static_result.get("critical", False):
                confidence_score *= 0.2
                recommendations.append("🚨 Critical syntax errors must be fixed before deployment")
            else:
                confidence_score *= 0.7
                recommendations.append("⚠️ Consider fixing linting issues for better code quality")

        testing_result = stages.get("testing", {})
        if not testing_result.get("success", True):
            confidence_score *= 0.6
            recommendations.append("🧪 Test failures detected - ensure functionality works correctly")
        elif testing_result.get("tests_run", 0) == 0:
            confidence_score *= 0.8
            recommendations.append("💡 No tests run - consider adding test coverage")

        overall_success = all(
            stages.get(stage, {}).get("success", True)
            for stage in ["static", "testing"]
        )

        return {
            "confidence_score": max(0.0, min(1.0, confidence_score)),
            "success": overall_success,
            "recommendations": recommendations,
        }

    async def _run_deterministic_verification_gate(
        self,
        on_event: Callable[[AgentEvent], Awaitable[None]],
    ) -> tuple:
        """Run lint + test verification on modified files.

        Streamlined gate: runs lint_file per modified file, then targeted tests
        if enabled. Skips the heavier progressive stages (semantic/quality/confidence)
        which added latency without affecting the pass/fail decision.
        """
        modified_abs = [f for f in self._file_snapshots.keys() if os.path.isfile(f)]
        if not modified_abs:
            return True, "No modified files (or all deleted)."

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

        # 2) Targeted tests (if enabled and test files exist)
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

        # 3) Verification orchestrator commands (py_compile, ruff, tsc, etc.)
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
