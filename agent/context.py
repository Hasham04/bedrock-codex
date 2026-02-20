"""
Context management and session state for the coding agent.
Handles memory, todos, file snapshots, approval tracking, checkpoints, and state persistence.
"""

import json
import logging
import os
import queue as _queue
import time
from typing import Dict, List, Any, Optional
from pathlib import Path

from config import app_config
from tools.schemas import NATIVE_EDITOR_NAME, NATIVE_BASH_NAME, EDITOR_WRITE_COMMANDS

logger = logging.getLogger(__name__)


class ContextMixin:
    """Mixin providing context management, state persistence, and session capabilities."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Context and memory management
        self._memory: Dict[str, str] = {}  # key -> value for MemoryWrite/MemoryRead
        self._todos: List[Dict[str, Any]] = []  # TodoWrite/TodoRead state

        # File snapshots for revert functionality
        self._file_snapshots: Dict[str, Any] = {}

        # Planning state
        self._current_plan: str = ""
        self._current_plan_decomposition: List[Dict[str, Any]] = []
        self._plan_file_path: str = ""
        self._plan_text: str = ""
        self._plan_title: str = ""
        self._plan_step_index: int = 0

        # Context for sharing between phases
        self._scout_context: str = ""
        self._plan_context_summary: str = ""

        # Session management
        self._session_checkpoints: List[Dict[str, Any]] = []
        self._checkpoint_counter: int = 0
        self._step_checkpoints: Dict[int, Dict[str, Optional[str]]] = {}

        # Running summary for context preservation
        self._running_summary: str = ""

        # Command approval tracking
        self._approved_commands: set = set()

        # Verification state
        self._deterministic_verification_done: bool = False

        # Token tracking
        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0
        self._cache_read_tokens: int = 0
        self._cache_write_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """Total token usage across all API calls."""
        return self._total_input_tokens + self._total_output_tokens

    @property
    def modified_files(self) -> Dict[str, Any]:
        """Return files modified in this session (path -> original_content or None if new file)."""
        return dict(self._file_snapshots)

    def reset_session_state(self):
        """Reset all session state to start fresh."""
        self._memory = {}
        self._todos = []
        self._file_snapshots = {}
        self._current_plan = ""
        self._current_plan_decomposition = []
        self._plan_file_path = ""
        self._plan_text = ""
        self._plan_title = ""
        self._plan_step_index = 0
        self._scout_context = ""
        self._plan_context_summary = ""
        self._session_checkpoints = []
        self._checkpoint_counter = 0
        self._step_checkpoints = {}
        self._running_summary = ""
        self._approved_commands = set()
        self._deterministic_verification_done = False
        # Keep token tracking across resets for session lifetime totals

    # ------------------------------------------------------------------
    # File Snapshots
    # ------------------------------------------------------------------

    def _snapshot_file(self, tool_name: str, tool_input: Dict[str, Any]) -> None:
        """Capture the original content of a file before it's modified.
        Only snapshots once per file per build run — first write wins."""
        is_native_write = (
            tool_name == NATIVE_EDITOR_NAME
            and tool_input.get("command") in EDITOR_WRITE_COMMANDS
        )
        if not is_native_write and tool_name != "symbol_edit":
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
    # Step Checkpoints (plan step revert)
    # ------------------------------------------------------------------

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
    # Approval memory – skip re-prompting for previously-approved ops
    # ------------------------------------------------------------------

    def _approval_key(self, tool_name: str, tool_input: Dict[str, Any]) -> str:
        """Return a hashable key that uniquely identifies an operation for approval purposes."""
        if tool_name == NATIVE_BASH_NAME:
            return f"cmd:{tool_input.get('command', '')}"
        is_file_tool = (
            (tool_name == NATIVE_EDITOR_NAME and tool_input.get("command") in EDITOR_WRITE_COMMANDS)
            or tool_name == "symbol_edit"
        )
        if is_file_tool:
            path = tool_input.get("path", "")
            resolved = self.backend.resolve_path(path)
            cmd = tool_input.get("command", tool_name)
            return f"{cmd}:{self._backend_id}:{resolved}"
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
    # System Reminders
    # ------------------------------------------------------------------

    def _gather_system_reminders(self) -> List[str]:
        """Collect contextual reminders based on current agent state.

        These are injected into the system prompt to nudge the model
        toward better behavior based on the current situation.
        """
        reminders: List[str] = []

        # Remind about pending plan
        if self._current_plan and self._current_plan_decomposition:
            total_steps = len(self._current_plan_decomposition)
            if self._plan_step_index < total_steps:
                remaining = total_steps - self._plan_step_index
                reminders.append(
                    f"Active plan: step {self._plan_step_index + 1}/{total_steps} "
                    f"({remaining} remaining). Follow plan steps in order. Note any deviations."
                )
            else:
                reminders.append("Implementation plan is complete. Verify all changes work correctly before finishing.")

        # File modification tracking
        if self._file_snapshots:
            count = len([p for p in self._file_snapshots if self._file_snapshots[p] is not None])
            new_count = len([p for p in self._file_snapshots if self._file_snapshots[p] is None])
            if count > 0 or new_count > 0:
                msg = f"You have {count} file(s) with pending modifications"
                if new_count > 0:
                    msg += f" and {new_count} new file(s)"
                msg += ". The user can keep or revert these changes."
                reminders.append(msg)

        # Remind about todos that may need attention
        if self._todos:
            in_progress = [t for t in self._todos if t.get("status") == "in_progress"]
            pending = [t for t in self._todos if t.get("status") == "pending"]
            if in_progress:
                reminders.append(f"You have {len(in_progress)} task(s) in progress. Complete them before starting new work.")
            elif pending and not in_progress:
                reminders.append(f"You have {len(pending)} pending task(s). Set one to in_progress and begin.")

        # Token budget awareness — nudge conciseness when context is getting large
        if self._total_input_tokens > 150_000:
            reminders.append(
                "Context window is getting large. Be concise in tool calls — "
                "use offset/limit for reads, avoid re-reading files already in context."
            )

        # Remind to run tests when files have been modified and a test command is known
        if self._file_snapshots and self._memory.get("test_cmd"):
            reminders.append(
                f"You have modified {len(self._file_snapshots)} file(s) and know the test command. "
                "Run tests to verify your changes before finishing."
            )

        return reminders

    # ------------------------------------------------------------------
    # State Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Serialize agent state for session persistence."""
        # Serialize file snapshots — skip binary files (> 1MB or decode failure)
        snapshots = {}
        for path, content in self._file_snapshots.items():
            if content is None:
                snapshots[path] = None  # new file marker
            elif isinstance(content, dict) and content.get("created") and "content" in content:
                raw = content["content"]
                if isinstance(raw, str) and len(raw) < 1_000_000:
                    try:
                        raw.encode("utf-8")
                        snapshots[path] = content
                    except (UnicodeDecodeError, UnicodeEncodeError):
                        pass
            elif isinstance(content, str) and len(content) < 1_000_000:
                try:
                    content.encode("utf-8")
                    snapshots[path] = content
                except (UnicodeDecodeError, UnicodeEncodeError):
                    pass

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

        # Step checkpoints — same size rules, cap to last 15 steps
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
            "history": getattr(self, 'history', []),
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
            "plan_title": self._plan_title,
            "scout_context": self._scout_context,
            "file_snapshots": snapshots,
            "session_checkpoints": checkpoints,
            "checkpoint_counter": self._checkpoint_counter,
            "step_checkpoints": step_checkpoints_ser,
            "plan_step_index": self._plan_step_index,
            "deterministic_verification_done": self._deterministic_verification_done,
            "todos": list(self._todos),
            "memory": dict(self._memory),
            "pending_guidance": list(self._drain_guidance_snapshot()),
        }

    def _drain_guidance_snapshot(self) -> List[str]:
        """Non-destructively snapshot the pending guidance queue for serialization."""
        items: List[str] = []
        while True:
            try:
                items.append(self._pending_guidance.get_nowait())
            except _queue.Empty:
                break
        for item in items:
            self._pending_guidance.put(item)
        return items

    def from_dict(self, data: Dict[str, Any]) -> None:
        """Restore agent state from a persisted session. Unknown keys are ignored."""
        if not isinstance(data, dict):
            return

        # Restore basic session data
        history = data.get("history")
        if isinstance(history, list):
            if hasattr(self, 'history'):
                self.history = history

        # Token usage
        token_usage = data.get("token_usage", {})
        if isinstance(token_usage, dict):
            self._total_input_tokens = int(token_usage.get("input_tokens", 0))
            self._total_output_tokens = int(token_usage.get("output_tokens", 0))
            self._cache_read_tokens = int(token_usage.get("cache_read_tokens", 0))
            self._cache_write_tokens = int(token_usage.get("cache_write_tokens", 0))

        # Approved commands
        approved = data.get("approved_commands")
        if isinstance(approved, list):
            self._approved_commands = set(approved)

        # Plans and state
        self._running_summary = data.get("running_summary", "")
        self._current_plan = data.get("current_plan", "")
        self._plan_file_path = data.get("plan_file_path", "")
        self._plan_text = data.get("plan_text", "")
        self._plan_title = data.get("plan_title", "")
        self._scout_context = data.get("scout_context", "")
        self._plan_step_index = int(data.get("plan_step_index", 0))
        self._deterministic_verification_done = bool(data.get("deterministic_verification_done", False))

        # Plan decomposition
        decomp = data.get("current_plan_decomposition")
        if isinstance(decomp, list):
            self._current_plan_decomposition = decomp

        # Session checkpoints
        checkpoints = data.get("session_checkpoints")
        if isinstance(checkpoints, list):
            self._session_checkpoints = checkpoints
        self._checkpoint_counter = int(data.get("checkpoint_counter", 0))

        # Step checkpoints
        step_cp_data = data.get("step_checkpoints", {})
        if isinstance(step_cp_data, dict):
            self._step_checkpoints = {}
            for k, v in step_cp_data.items():
                try:
                    step_num = int(k)
                    if isinstance(v, dict):
                        self._step_checkpoints[step_num] = v
                except ValueError:
                    pass

        # Todos
        todos = data.get("todos")
        if isinstance(todos, list):
            self._todos = todos

        # Memory
        raw_memory = data.get("memory")
        self._memory = dict(raw_memory) if isinstance(raw_memory, dict) else {}
        self._cancelled = False

        # Restore pending guidance
        pending_guidance = data.get("pending_guidance")
        if isinstance(pending_guidance, list):
            for g in pending_guidance:
                if isinstance(g, str) and g.strip():
                    self._pending_guidance.put(g)

        # Restore file snapshots
        raw_snapshots = data.get("file_snapshots", {})
        if isinstance(raw_snapshots, dict):
            self._file_snapshots = dict(raw_snapshots)

        # Validate history structure after restore — fix orphaned tool_use blocks
        # that may have been persisted from a mid-stream-failure save.
        if hasattr(self, '_repair_history'):
            self._repair_history()