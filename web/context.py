"""
Auto-context assembly and @mention resolution.

Provides Cursor-style automatic context injection into agent conversations.
"""

import logging
import os
import re
import time
from typing import Optional, Dict, Any, List, Tuple

from backend import Backend
from bedrock_service import BedrockService
from web.state import (
    _project_tree_cache,
    _PROJECT_TREE_TTL,
    _AUTO_CONTEXT_CHAR_BUDGET,
    _bg_codebase_index,
    _bg_index_ready,
)
import web.state as _state

logger = logging.getLogger(__name__)


async def _build_index_background(backend: Backend, working_directory: str):
    """Build codebase index in background thread on first WS connect."""
    try:
        from codebase_index import get_index, set_embed_fn
        from config import app_config

        if not getattr(app_config, "codebase_index_enabled", True):
            _bg_index_ready.set()
            return

        svc = BedrockService()
        if hasattr(svc, "embed_texts"):
            set_embed_fn(svc.embed_texts)

        import asyncio
        idx = await asyncio.to_thread(
            lambda: get_index(working_directory, embed_fn=svc.embed_texts if hasattr(svc, "embed_texts") else None, backend=backend)
        )

        if not idx.chunks:
            await asyncio.to_thread(lambda: idx.build(backend))

        _state._bg_codebase_index = idx
        logger.info("Background index ready: %d chunks", len(idx.chunks))
    except Exception as e:
        logger.warning("Background index build failed: %s", e)
    finally:
        _bg_index_ready.set()


def _assemble_auto_context(
    working_directory: str,
    editor_context: Optional[Dict[str, Any]] = None,
    modified_files: Optional[set] = None,
    backend: Optional[Backend] = None,
    user_query: Optional[str] = None,
) -> str:
    """Assemble auto-context that gets injected into the agent conversation.

    Works with both local and SSH backends. Uses the Backend abstraction for
    all file reads and command execution.

    Priority order (each section is added only if budget remains):
    1. Active file (window around cursor, or first 200 lines)
    2. Selected text (if any)
    3. Recently modified files by agent (last 3, first 50 lines each)
    3.5. Dependency-aware context (1-hop imports)
    4. Git diff summary
    5. Project tree (cached)
    6. Open files list

    Returns empty string if no useful context available.
    """
    from backend import LocalBackend as _LB
    b = backend or _LB(os.path.abspath(working_directory))
    sections: List[str] = []
    budget = _AUTO_CONTEXT_CHAR_BUDGET
    editor_context = editor_context or {}

    def add_section(label: str, content: str) -> bool:
        nonlocal budget
        section = f"<{label}>\n{content.rstrip()}\n</{label}>"
        if len(section) <= budget:
            sections.append(section)
            budget -= len(section)
            return True
        elif budget > 200:
            truncated = content[:budget - 100].rstrip()
            section = f"<{label}>\n{truncated}\n… (truncated)\n</{label}>"
            sections.append(section)
            budget -= len(section)
            return True
        return False

    abs_wd = b.working_directory if hasattr(b, 'working_directory') else os.path.abspath(working_directory)

    def _read_file_safe(path: str) -> Optional[str]:
        """Read a file via Backend (works for both local and SSH)."""
        try:
            return b.read_file(path)
        except Exception:
            return None

    # 1. Active file
    active = editor_context.get("activeFile", {})
    active_path = active.get("path", "")
    if active_path:
        cursor_line = active.get("cursorLine")
        try:
            content = _read_file_safe(active_path)
            if content is not None:
                all_lines = content.splitlines(keepends=True)
                total = len(all_lines)
                if cursor_line and total > 200:
                    start = max(0, cursor_line - 100)
                    end = min(total, cursor_line + 100)
                    window = all_lines[start:end]
                    header = f"# {active_path} (lines {start+1}-{end} of {total}, cursor at line {cursor_line})\n"
                else:
                    window = all_lines[:200]
                    header = f"# {active_path} ({total} lines total" + (f", cursor at line {cursor_line}" if cursor_line else "") + ")\n"
                    if total > 200:
                        header = f"# {active_path} (showing first 200 of {total} lines)\n"
                numbered = "".join(f"{start + i + 1 if cursor_line and total > 200 else i + 1:6}|{l}" for i, l in enumerate(window))
                add_section("active_file", header + numbered)
        except Exception:
            pass

    # 2. Selected text
    selected = editor_context.get("selectedText", "")
    if selected:
        add_section("selected_text", f"# Selected in {active_path}\n{selected}")

    # 3. Recently modified files (by agent)
    if modified_files:
        recent_mods = list(modified_files)[-3:]
        for mp in recent_mods:
            if mp == active_path:
                continue
            try:
                content = _read_file_safe(mp)
                if content is not None:
                    lines = content.splitlines(keepends=True)[:50]
                    text = f"# {mp} (recently modified, first {len(lines)} lines)\n" + "".join(lines)
                    add_section("modified_file", text)
            except Exception:
                pass

    # 3.5. Dependency-aware context (1-hop imports of active file)
    if active_path and budget > 500:
        try:
            from codebase_index import get_index, get_dependency_neighborhood
            idx = get_index(abs_wd)
            if idx.file_imports:
                neighbors = get_dependency_neighborhood(
                    active_path, idx.file_imports, idx.reverse_imports, max_neighbors=5
                )
                if neighbors:
                    dep_lines = [f"# Related files (1-hop imports of {active_path})"]
                    for np_ in neighbors:
                        try:
                            dep_content = _read_file_safe(np_)
                            if dep_content is not None:
                                first_lines = dep_content.splitlines(keepends=True)[:20]
                                dep_lines.append(f"\n## {np_} (first 20 lines)")
                                dep_lines.append("".join(first_lines))
                        except Exception:
                            pass
                    if len(dep_lines) > 1:
                        add_section("dependency_context", "\n".join(dep_lines))
        except Exception:
            pass

    # 3.7. Semantic search — use background index to find relevant chunks for the query
    if user_query and budget > 1000:
        try:
            idx = _state._bg_codebase_index
            if idx and idx.chunks:
                results = idx.retrieve(user_query, top_k=5)
                if results:
                    sem_lines = ["# Relevant code (semantic search)"]
                    for chunk in results:
                        snippet = chunk.to_search_snippet(max_lines=20)
                        sem_lines.append(snippet)
                    add_section("semantic_context", "\n\n".join(sem_lines))
        except Exception:
            pass

    # 4. Git diff summary (via Backend.run_command for SSH support)
    try:
        stdout, _, rc = b.run_command("git diff --stat", ".", timeout=5)
        if rc == 0 and stdout and stdout.strip():
            diff_stat = stdout.strip()
            diff_content = f"# git diff --stat\n{diff_stat}"
            if len(diff_stat) < 2000:
                stdout2, _, rc2 = b.run_command("git diff --no-color", ".", timeout=5)
                if rc2 == 0 and stdout2:
                    diff_lines = stdout2.split("\n")[:50]
                    diff_content += "\n\n# git diff (first 50 lines)\n" + "\n".join(diff_lines)
            add_section("git_diff", diff_content)
    except Exception:
        pass

    # 5. Project tree (cached)
    cache_key = abs_wd
    now = time.time()
    cached = _project_tree_cache.get(cache_key)
    if cached and (now - cached[0]) < _PROJECT_TREE_TTL:
        tree_text = cached[1]
    else:
        try:
            from tools import project_tree
            result = project_tree(backend=b, working_directory=abs_wd)
            tree_text = result.output if result.success else ""
            _project_tree_cache[cache_key] = (now, tree_text)
        except Exception:
            tree_text = ""
    if tree_text:
        add_section("project_structure", tree_text)

    # 6. Linter errors on recently modified and active files
    if budget > 300:
        lint_targets = set()
        if active_path:
            lint_targets.add(active_path)
        if modified_files:
            lint_targets.update(list(modified_files)[-3:])
        if lint_targets:
            try:
                from tools import lint_file as _lint_file
                lint_errors = []
                for lp in lint_targets:
                    try:
                        lr = _lint_file(path=lp, backend=b, working_directory=abs_wd)
                        if lr.success and lr.output and "no issues" not in lr.output.lower():
                            lint_errors.append(f"## {lp}\n{lr.output[:500]}")
                    except Exception:
                        pass
                if lint_errors:
                    add_section("linter_errors", "# Linter errors on active/modified files\n" + "\n\n".join(lint_errors))
            except Exception:
                pass

    # 7. Open files list
    open_files = editor_context.get("openFiles", [])
    if open_files and len(open_files) > 1:
        file_list = "\n".join(f"  {f}" for f in open_files if f != active_path)
        if file_list:
            add_section("open_files", f"# Other open files in editor\n{file_list}")

    if not sections:
        return ""

    return "<auto_context>\n" + "\n\n".join(sections) + "\n</auto_context>"


def active_file_in_context(auto_ctx: str) -> bool:
    """Check if auto-context contains an active file section."""
    return "<active_file>" in auto_ctx


# ============================================================
# @ Mention resolution
# ============================================================

_MENTION_RE = re.compile(r"@([\w./_-]+)")
_MENTION_TOKEN_CAP = 6000  # ~1500 tokens per mention, cap total


def _resolve_mentions(task_text: str, working_directory: str, backend: Optional[Backend] = None) -> str:
    """Resolve @file and @special mentions in the task text.

    Works with both local and SSH backends.
    Returns modified task text with mentions replaced by inline references.
    """
    from backend import LocalBackend as _LB
    b = backend or _LB(os.path.abspath(working_directory))
    abs_wd = b.working_directory if hasattr(b, 'working_directory') else os.path.abspath(working_directory)
    mentions = list(_MENTION_RE.finditer(task_text))
    if not mentions:
        return task_text

    resolved_parts: List[str] = []
    budget = _MENTION_TOKEN_CAP

    for m in mentions:
        ref = m.group(1)

        if ref == "codebase":
            try:
                from tools import project_tree
                result = project_tree(backend=b, working_directory=abs_wd)
                if result.success and budget > len(result.output):
                    tag = f"\n<mentioned_codebase>\n{result.output}\n</mentioned_codebase>\n"
                    resolved_parts.append(tag)
                    budget -= len(tag)
            except Exception:
                pass
            continue

        if ref == "git":
            try:
                stdout, _, rc = b.run_command("git diff --no-color", ".", timeout=10)
                if rc == 0 and stdout and stdout.strip():
                    content = stdout[:3000]
                    if budget > len(content):
                        tag = f"\n<mentioned_git_diff>\n{content}\n</mentioned_git_diff>\n"
                        resolved_parts.append(tag)
                        budget -= len(tag)
            except Exception:
                pass
            continue

        if ref == "terminal":
            resolved_parts.append("\n<mentioned_terminal>(terminal context not available in this environment)</mentioned_terminal>\n")
            continue

        try:
            if b.is_file(ref):
                content = b.read_file(ref)
                if len(content) > 4000:
                    content = content[:4000] + "\n… (truncated)"
                if budget > len(content):
                    tag = f"\n<mentioned_file path=\"{ref}\">\n{content}\n</mentioned_file>\n"
                    resolved_parts.append(tag)
                    budget -= len(tag)
        except Exception:
            pass

    if resolved_parts:
        return task_text + "\n" + "".join(resolved_parts)
    return task_text
