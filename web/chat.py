"""
Main WebSocket endpoint — one agent per connection.

Handles session restore, replay, the message loop, event handling,
auto-context injection, task/build runners, and file watching.
"""

import asyncio
import difflib
import json
import logging
import os
import re
import time
from typing import Optional, Dict, Any, List

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from bedrock_service import BedrockService, BedrockError
from agent import CodingAgent, AgentEvent, classify_intent
from backend import Backend, LocalBackend, SSHBackend
from sessions import SessionStore, Session
from config import (
    get_model_name,
    get_model_config,
    get_context_window,
    model_config,
    app_config,
    supports_thinking,
    supports_caching,
)

from web.state import (
    _reconnect_sessions, _active_save_fns,
    _WSRef,
)
import web.state as _state
from web.context import (
    _assemble_auto_context,
    _resolve_mentions,
    active_file_in_context,
    _build_index_background,
)
from web.api_files import _normalize_user_images

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, session_id: Optional[str] = None):
    await ws.accept()

    # ── Reconnect hand-off ──────────────────────────────────────
    # If a previous handler is waiting for a reconnect on this session,
    # hand the new WS over and keep *this* handler alive (so the WS
    # stays open) until the old handler finishes using it.
    _req_sid = (ws.query_params.get("session_id") or "").strip()
    if not _req_sid and session_id:
        _req_sid = str(session_id).strip()

    if _req_sid and _req_sid in _reconnect_sessions:
        entry = _reconnect_sessions[_req_sid]
        done_event = asyncio.Event()
        try:
            entry["future"].set_result((ws, done_event))
        except asyncio.InvalidStateError:
            pass  # future already resolved (race)
        else:
            # Park this handler — the OLD handler now owns the WS.
            try:
                await done_event.wait()
            except (asyncio.CancelledError, Exception):
                pass
            return

    # ── Normal / fresh connection ───────────────────────────────
    wsr = _WSRef(ws)

    is_ssh = _state._ssh_info is not None
    # For SSH projects, _working_directory is composite (user@host:port:dir)
    # For local, normalize to absolute path
    if is_ssh:
        wd = _state._working_directory
        # The actual filesystem directory for the agent is the remote dir
        agent_wd = _state._ssh_info["directory"]
    else:
        wd = os.path.abspath(_state._working_directory)
        agent_wd = wd

    # Initialise services
    try:
        bedrock_service = BedrockService()
        backend = _state._backend or LocalBackend(agent_wd)
        agent = CodingAgent(
            bedrock_service,
            working_directory=agent_wd,
            max_iterations=int(os.getenv("MAX_TOOL_ITERATIONS", "50")),
            backend=backend,
        )
        _state._active_agent = agent
    except Exception as e:
        await wsr.send_json({"type": "error", "content": f"Init failed: {e}"})
        await ws.close()
        return

    # Session management — wd is the composite key (unique per project + SSH target)
    requested_session_id = (ws.query_params.get("session_id") or "").strip()
    if not requested_session_id and session_id:
        requested_session_id = str(session_id).strip()

    store = SessionStore()
    session: Optional[Session] = None

    # Flush the previous session to disk before loading
    # (e.g. when switching sessions via the dropdown)
    if requested_session_id and requested_session_id in _active_save_fns:
        try:
            _active_save_fns[requested_session_id]()
        except Exception:
            pass

    if requested_session_id:
        loaded = store.load(requested_session_id)
        if loaded and loaded.working_directory == wd:
            session = loaded
        elif loaded:
            # Session exists but is for a different workspace (e.g. different path or SSH resolved path changed)
            logger.debug("Session %s is for another workspace (wd mismatch), using latest for this workspace", requested_session_id)
        else:
            logger.debug("Session %s not found, using latest for this workspace", requested_session_id)
    if session is None:
        session = store.get_latest(wd)
    # Keys that are web.py UI state, not agent state
    _ui_state_keys = {
        "ssh_info",
        "awaiting_build",
        "awaiting_keep_revert",
        "kept_file_contents",
        "pending_task",
        "pending_plan",
        "pending_images",
        "current_thinking_text",
        "current_text_buffer",
    }
    _restored_ui_state: Dict[str, Any] = {}
    if session is None:
        session = store.create_session(wd, model_config.model_id)
        store.save(session)
    else:
        # Restore agent/session state even for empty-history sessions.
        # Otherwise, switching to a newly created agent session would
        # incorrectly fall back to a fresh default session.
        restore_data = {
            "history": session.history or [],
            "token_usage": session.token_usage or {},
        }
        # Restore extra state (running_summary, current_plan, approved_commands, etc.)
        if session.extra_state:
            # Separate UI state from agent state
            for k, v in session.extra_state.items():
                if k in _ui_state_keys:
                    _restored_ui_state[k] = v
                else:
                    restore_data[k] = v
        agent.from_dict(restore_data)

    def save_session():
        if session and agent:
            state = agent.to_dict()
            session.history = state["history"]
            session.token_usage = state["token_usage"]
            # Persist all agent state keys from to_dict() plus UI state.
            session.extra_state = {k: v for k, v in state.items()
                                   if k not in ("history", "token_usage")}
            # UI state for reconnection
            session.extra_state.update({
                "awaiting_build": awaiting_build,
                "awaiting_keep_revert": awaiting_keep_revert,
                "kept_file_contents": _kept_file_contents,
                "pending_task": pending_task,
                "pending_plan": pending_plan,
                "pending_images": [{k: v for k, v in img.items() if k != "data"} for img in (pending_images or [])],
                # In-progress stream buffers (survive server restart)
                "current_thinking_text": _current_thinking_text,
                "current_text_buffer": _current_text_buffer,
            })
            # Persist SSH connection info so it can be reused on reopen
            if _state._ssh_info:
                session.extra_state["ssh_info"] = _state._ssh_info
            session.working_directory = wd
            try:
                store.save(session)
            except Exception as exc:
                logger.error(f"Session save failed: {exc}")

    # Register this save function globally so shutdown handler can call it
    if session and session.session_id:
        _active_save_fns[session.session_id] = save_session

    # State machine — restore from session if reconnecting
    pending_task: Optional[str] = _restored_ui_state.get("pending_task")
    pending_plan: Optional[List[str]] = _restored_ui_state.get("pending_plan")
    pending_images: List[Dict[str, Any]] = list(_restored_ui_state.get("pending_images") or [])
    awaiting_build: bool = bool(_restored_ui_state.get("awaiting_build"))
    # Only show Keep/Revert if we have pending snapshots and user hasn't already resolved (Keep/Revert).
    # If we saved awaiting_keep_revert=False (user clicked Keep/Revert), never show the bar again.
    awaiting_keep_revert: bool = _restored_ui_state.get("awaiting_keep_revert", False)
    # Track file contents at the time of Keep so we can exclude unchanged files from the next diff.
    # Maps abs_path -> content_at_keep_time.  Files re-modified after Keep will have different
    # current content and will reappear in the diff.
    _kept_file_contents: Dict[str, str] = dict(_restored_ui_state.get("kept_file_contents") or {})
    task_start: Optional[float] = None
    _last_save_time: float = time.time()
    _agent_task: Optional[asyncio.Task] = None
    _cancel_ack_sent: bool = False

    # ------------------------------------------------------------------
    # History replay — rebuild chat from persisted history
    # ------------------------------------------------------------------

    # Known internal XML tags injected by the agent or auto-context into user messages.
    _INTERNAL_XML_TAGS = [
        "codebase_context", "approved_plan", "plan_decomposition",
        "manager_worker_insights", "project_context", "current_plan",
        "updated_plan", "scout_context", "verification_context",
        # Auto-context tags from build_auto_context()
        "auto_context", "active_file", "selected_text", "modified_file",
        "dependency_context", "semantic_context", "git_diff", "project_structure",
        "linter_errors", "open_files", "recent_files",
    ]
    _STRIP_XML_RE = re.compile(
        r"<(" + "|".join(_INTERNAL_XML_TAGS) + r")(?:\s[^>]*)?>[\s\S]*?</\1>",
        re.IGNORECASE,
    )
    _INSTRUCTION_SUFFIXES = [
        "Execute this plan step by step.",
        "State which step you are working on.",
        "Work through them in order; set each to completed and the next to in_progress as you go.",
    ]

    def _strip_internal_replay_content(text: str) -> Optional[str]:
        """Strip internal agent context from a user message for replay.
        Returns the cleaned user-facing text, or None if the message
        should be completely hidden."""
        if not text or not text.strip():
            return None
        # Skip system-injected messages (any case)
        if text.upper().startswith("[SYSTEM]") or text.startswith("[SYSTEM —"):
            return None
        # Skip internal phase tracking and editor context
        if text.startswith("<completed_phases>") or "<completed_phases>" in text:
            return None
        if text.startswith("**Phase ") and "— type:" in text:
            return None
        if text.startswith("[Editor context:") or "[Editor context:" in text:
            return None
        # Skip compressed / trimmed markers
        if "(earlier context compressed)" in text or "(earlier work trimmed)" in text:
            return None
        # Skip verification nudges
        if text.startswith("Verification pass") or text.startswith("Quick check"):
            return None
        if text.startswith("Quick verification") or text.startswith("[VERIFICATION FOR CURRENT"):
            return None
        if text.startswith("[Previous task verification"):
            return None
        if text.startswith("You have completed all plan steps"):
            return None
        # If the user said "User's message: " (follow-up with plan context), extract it
        if "<current_plan>" in text and "User's message: " in text:
            return text.split("User's message: ", 1)[-1].strip() or None
        # Strip all known internal XML blocks
        cleaned = _STRIP_XML_RE.sub("", text).strip()
        # Strip known instruction suffixes
        for suffix in _INSTRUCTION_SUFFIXES:
            cleaned = cleaned.replace(suffix, "").strip()
        # Remove any remaining instructional block that starts with "Before touching files"
        # or "For each step:" — these are agent instructions, not user text
        for marker in [
            "Before touching files, call TodoWrite",
            "For each step:\n",
            "If you discover something the plan missed",
        ]:
            idx = cleaned.find(marker)
            if idx >= 0:
                cleaned = cleaned[:idx].strip()
        return cleaned if cleaned else None

    async def replay_history():
        """Walk through saved history and emit replay events so the
        frontend can rebuild the full conversation on reconnect.

        Improvements over basic replay:
        - Pairs tool_call with tool_result by tracking tool_use IDs
        - Skips system-injected messages (verification nudges, summaries)
        - Filters out compressed/trimmed content markers
        """
        if not agent.history:
            # Even with empty history, we must send replay_done so frontend knows restoration is complete
            await wsr.send_json({"type": "replay_done"})
            return

        # Build a map of tool_use_id -> tool_result for pairing
        tool_results_map: Dict[str, Dict[str, Any]] = {}
        for msg in agent.history:
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") == "tool_result":
                    tid = block.get("tool_use_id", "")
                    if tid:
                        result_content = block.get("content", "")
                        text = ""
                        if isinstance(result_content, str):
                            text = result_content
                        elif isinstance(result_content, list):
                            text = " ".join(
                                b.get("text", "") for b in result_content
                                if b.get("type") == "text"
                            )
                        tool_results_map[tid] = {
                            "content": text[:1000],
                            "success": not block.get("is_error", False),
                        }

        emitted_tool_results: set = set()

        for msg in agent.history:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "user":
                if isinstance(content, str):
                    # Detect guidance messages and replay with correct styling
                    if "[USER GUIDANCE" in content and "]\n\n" in content:
                        guidance_text = content.split("]\n\n", 1)[-1].strip()
                        if guidance_text:
                            await wsr.send_json({"type": "replay_guidance", "content": guidance_text})
                            continue
                    cleaned = _strip_internal_replay_content(content)
                    if cleaned:
                        await wsr.send_json({"type": "replay_user", "content": cleaned})
                elif isinstance(content, list):
                    image_count = 0
                    for block in content:
                        if block.get("type") == "text":
                            block_text = block.get("text", "")
                            # Detect guidance messages inlined in tool results
                            if "[USER GUIDANCE" in block_text and "]\n\n" in block_text:
                                guidance_text = block_text.split("]\n\n", 1)[-1].strip()
                                if guidance_text:
                                    await wsr.send_json({"type": "replay_guidance", "content": guidance_text})
                                continue
                            cleaned = _strip_internal_replay_content(block_text)
                            if cleaned:
                                await wsr.send_json({"type": "replay_user", "content": cleaned})
                        elif block.get("type") == "image":
                            image_count += 1
                    if image_count > 0:
                        await wsr.send_json({
                            "type": "replay_user",
                            "content": f"📷 {image_count} image attachment{'s' if image_count != 1 else ''}",
                        })

            elif role == "assistant":
                if isinstance(content, str):
                    _display = _STRIP_XML_RE.sub("", content).strip()
                    if _display:
                        await wsr.send_json({"type": "replay_text", "content": _display})
                elif isinstance(content, list):
                    for block in content:
                        btype = block.get("type", "")
                        if btype == "thinking":
                            thinking_text = block.get("thinking", "")
                            if thinking_text and thinking_text != "...":
                                await wsr.send_json({"type": "replay_thinking", "content": thinking_text})
                        elif btype == "text":
                            text = _STRIP_XML_RE.sub("", block.get("text", "")).strip()
                            if text:
                                await wsr.send_json({"type": "replay_text", "content": text})
                        elif btype == "tool_use":
                            tool_id = block.get("id", "")
                            await wsr.send_json({
                                "type": "replay_tool_call",
                                "data": {
                                    "name": block.get("name", ""),
                                    "input": block.get("input", {}),
                                    "id": tool_id,
                                },
                            })
                            # Immediately pair with its result if available
                            if tool_id in tool_results_map:
                                tr = tool_results_map[tool_id]
                                await wsr.send_json({
                                    "type": "replay_tool_result",
                                    "content": tr["content"],
                                    "data": {"tool_use_id": tool_id, "success": tr["success"]},
                                })
                                emitted_tool_results.add(tool_id)
                        elif btype == "server_tool_use":
                            await wsr.send_json({
                                "type": "server_tool_use",
                                "data": {
                                    "id": block.get("id", ""),
                                    "name": block.get("name", ""),
                                    "input": block.get("input", {}),
                                },
                            })
                        elif btype == "web_search_tool_result":
                            await wsr.send_json({
                                "type": "web_search_result",
                                "data": {
                                    "tool_use_id": block.get("tool_use_id", ""),
                                    "content": block.get("content", []),
                                },
                            })

        await wsr.send_json({"type": "replay_done"})

        # Send UI state so frontend can restore interactive elements
        # (plan buttons, keep/revert bar, etc.)
        state_msg: Dict[str, Any] = {
            "type": "replay_state",
            "awaiting_build": awaiting_build,
            "awaiting_keep_revert": awaiting_keep_revert,
        }
        todos = getattr(agent, "_todos", None) or []
        # Always send todos (even if empty) to properly restore checklist state
        state_msg["todos"] = todos
        if awaiting_build and pending_plan:
            state_msg["pending_plan"] = pending_plan
            state_msg["plan_step_index"] = agent._plan_step_index
            # Restore plan file / text so "Open in Editor" and full plan doc survive reload
            plan_file = getattr(agent, "_plan_file_path", None) or None
            plan_text = getattr(agent, "_plan_text", "") or ""
            # Fallback: if session has no plan_file (e.g. old session or two plans on disk),
            # use the latest plan file from .bedrock-codex/plans/ so the button always shows
            if not plan_file and agent.backend:
                try:
                    entries = agent.backend.list_dir(".bedrock-codex/plans")
                    md_files = [e["name"] for e in (entries or []) if e.get("type") == "file" and (e.get("name") or "").endswith(".md")]
                    if md_files:
                        # Newest by name (plan-YYYYMMDD-HHMMSS-slug.md sorts chronologically)
                        latest_name = max(md_files)
                        plan_file = ".bedrock-codex/plans/" + latest_name
                        if not plan_text:
                            try:
                                plan_text = agent.backend.read_file(plan_file)
                            except Exception:
                                pass
                except Exception:
                    pass
            state_msg["plan_file"] = plan_file
            state_msg["plan_text"] = plan_text or ""
            state_msg["plan_title"] = getattr(agent, "_plan_title", "") or ""
        if awaiting_keep_revert and agent._file_snapshots:
            # Generate and send actual diffs for the keep/revert bar
            diffs = generate_diffs()
            if diffs:
                state_msg["has_diffs"] = True
                state_msg["diffs"] = diffs
        await wsr.send_json(state_msg)

    # ------------------------------------------------------------------
    # Event bridge: AgentEvent → WebSocket JSON
    # ------------------------------------------------------------------

    async def on_event(event: AgentEvent):
        nonlocal awaiting_keep_revert, _last_save_time, _cancel_ack_sent
        nonlocal _current_thinking_text, _current_text_buffer
        # Skip agent's own cancelled event if we already sent one from the cancel handler
        if event.type == "cancelled" and _cancel_ack_sent:
            return

        # Track in-progress thinking/text so reconnects can resume blocks
        if event.type == "thinking_start":
            _current_thinking_text = ""
        elif event.type == "thinking":
            _current_thinking_text += (event.content or "")
        elif event.type == "thinking_end":
            _current_thinking_text = ""
        elif event.type == "text_start":
            _current_text_buffer = ""
        elif event.type == "text":
            _current_text_buffer += (event.content or "")
        elif event.type in ("text_end", "done", "error", "cancelled"):
            _current_text_buffer = ""
        elif event.type == "guidance_interrupt":
            _current_thinking_text = ""
            _current_text_buffer = ""

        msg: Dict[str, Any] = {"type": event.type}

        if event.content:
            msg["content"] = event.content
        if event.data:
            msg["data"] = event.data

        # So frontend can use evt.todos without digging into evt.data
        if event.type == "todos_updated" and event.data and "todos" in event.data:
            msg["todos"] = event.data["todos"]

        # Special handling for plan phase
        if event.type == "phase_plan":
            steps = event.data.get("steps", []) if event.data else []
            if not steps and event.content:
                steps = [l.strip() for l in event.content.strip().split("\n") if l.strip()]
            plan_file = event.data.get("plan_file") if event.data else None
            plan_text = event.data.get("plan_text", "") if event.data else ""
            plan_title = event.data.get("plan_title", "") if event.data else ""
            msg = {
                "type": "plan",
                "steps": steps,
                "plan_file": plan_file,
                "plan_text": plan_text,
                "plan_title": plan_title,
            }

        await wsr.send_json(msg)

        # ── Periodic auto-save: save after key events or every 5s during streaming ──
        now = time.time()
        should_save = False
        if event.type in ("tool_result", "thinking_end", "text_end", "done"):
            should_save = True  # natural save points
        elif event.type in ("thinking", "text") and now - _last_save_time >= 5:
            should_save = True  # save mid-stream every 5s so kills lose minimal content
        elif now - _last_save_time >= 10:
            should_save = True  # general time-based fallback
        if should_save:
            _last_save_time = now
            try:
                await asyncio.to_thread(save_session)
            except Exception:
                pass  # don't break streaming on save failure

    async def dummy_approval(tool_name: str, desc: str, data: Dict) -> bool:
        """Auto-approve everything — user reviews via keep/revert."""
        return True

    # ------------------------------------------------------------------
    # Diff generation (works for both local and SSH: SSH uses backend.read_file)
    # ------------------------------------------------------------------

    def _rel_from_working(abs_path: str, working_directory: str) -> str:
        """Return path relative to working_directory (POSIX-style for SSH)."""
        norm_wd = (working_directory or "").rstrip("/")
        norm_abs = (abs_path or "").replace("\\", "/")
        if norm_abs == norm_wd:
            return "."
        if norm_wd and norm_abs.startswith(norm_wd + "/"):
            return norm_abs[len(norm_wd) + 1:]
        return norm_abs.split("/")[-1] if "/" in norm_abs else norm_abs

    def generate_diffs() -> List[Dict[str, Any]]:
        modified = agent.modified_files
        if not modified:
            return []
        is_ssh = getattr(agent.backend, "_host", None) is not None
        diffs = []
        for abs_path, original in modified.items():
            if is_ssh:
                rel = _rel_from_working(abs_path, agent.working_directory)
                try:
                    new_content = agent.backend.read_file(abs_path)
                except Exception:
                    new_content = ""
            else:
                rel = os.path.relpath(abs_path, wd)
                try:
                    with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                        new_content = f.read()
                except FileNotFoundError:
                    new_content = ""

            # Skip files that haven't changed since the user last clicked Keep.
            if abs_path in _kept_file_contents and new_content == _kept_file_contents[abs_path]:
                continue

            new_lines = new_content.splitlines(keepends=True)
            # Resolve original content: dict means "created file", None means "new file"
            is_created = original is None or (isinstance(original, dict) and original.get("created"))
            old_text = "" if is_created else (original if isinstance(original, str) else "")
            old_lines = old_text.splitlines(keepends=True)

            diff_lines = list(difflib.unified_diff(
                old_lines, new_lines,
                fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm=""
            ))

            additions = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
            deletions = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))

            label = "new file" if is_created else "modified"

            diffs.append({
                "path": rel,
                "label": label,
                "additions": additions,
                "deletions": deletions,
                "diff": "\n".join(diff_lines),
            })

        return diffs

    # ------------------------------------------------------------------
    # Message loop
    # ------------------------------------------------------------------

    _watcher_task = None
    _save_interval_task: Optional[asyncio.Task] = None
    _pending_question: Dict[str, Any] = {"future": None, "tool_use_id": None}
    # Buffer in-progress thinking/text so they survive reconnects AND server restarts.
    # Populated by streaming events; cleared when the block ends.
    # Restored from session extra_state if the server was killed mid-stream.
    _current_thinking_text: str = _restored_ui_state.get("current_thinking_text", "") or ""
    _current_text_buffer: str = _restored_ui_state.get("current_text_buffer", "") or ""

    async def _periodic_save_loop():
        """Save session every 10s so kills/crashes lose minimal state."""
        while True:
            await asyncio.sleep(10)
            try:
                await asyncio.to_thread(save_session)
            except Exception:
                pass

    async def _message_loop():
        """Read and dispatch incoming WS messages. Extracted as a reusable
        closure so it can be re-entered after a reconnect."""
        nonlocal _cancel_ack_sent, _agent_task, awaiting_keep_revert, _kept_file_contents
        nonlocal awaiting_build, pending_task, pending_plan, pending_images
        nonlocal session, _current_thinking_text, _current_text_buffer
        nonlocal _save_interval_task
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await wsr.send_json({"type": "error", "content": "Invalid JSON"})
                continue

            msg_type = data.get("type", "")

            # ── User answer to clarifying question (plan phase) ───
            if msg_type == "user_answer":
                if _pending_question["future"] and data.get("tool_use_id") == _pending_question["tool_use_id"]:
                    try:
                        _pending_question["future"].set_result(data.get("answer", ""))
                    except asyncio.InvalidStateError:
                        pass
                continue

            # ── Cancel ─────────────────────────────────────────────
            if msg_type == "cancel":
                _cancel_ack_sent = True
                agent.cancel()  # sets flag + kills running subprocess
                if _agent_task and not _agent_task.done():
                    # Give the agent task a moment to wind down gracefully
                    try:
                        await asyncio.wait_for(asyncio.shield(_agent_task), timeout=3.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                        _agent_task.cancel()
                _agent_task = None
                await wsr.send_json({"type": "cancelled"})
                # Show keep/revert bar if the agent modified files before being stopped
                if agent._file_snapshots:
                    awaiting_keep_revert = True
                    diffs = generate_diffs()
                    if diffs:
                        await wsr.send_json({"type": "diff", "files": diffs, "cumulative": True})
                continue

            # ── Guidance (mid-task user correction) ─────────────────
            if msg_type == "guidance":
                guidance_text = data.get("content", "").strip()
                if not guidance_text:
                    continue
                # Length limit
                _MAX_GUIDANCE_LEN = 10_000
                if len(guidance_text) > _MAX_GUIDANCE_LEN:
                    await wsr.send_json({
                        "type": "error",
                        "content": f"Guidance too long ({len(guidance_text)} chars, max {_MAX_GUIDANCE_LEN}).",
                    })
                    continue
                # Rate limit — at most one guidance message every 2 seconds
                _GUIDANCE_COOLDOWN = 2.0
                now = time.time()
                if now - _state._last_guidance_time < _GUIDANCE_COOLDOWN:
                    await wsr.send_json({"type": "info", "content": "Please wait before sending more guidance."})
                    continue
                _state._last_guidance_time = now

                if _agent_task and not _agent_task.done():
                    agent.inject_guidance(guidance_text)
                    await wsr.send_json({"type": "guidance_queued", "content": guidance_text})
                else:
                    await wsr.send_json({"type": "info", "content": "No task is running. Send as a normal message instead."})
                continue

            # ── Keep / Revert ──────────────────────────────────────
            if msg_type == "keep" and awaiting_keep_revert:
                kept_paths = [os.path.relpath(p, wd) for p in agent.modified_files]
                # Preserve snapshots so diffs stay cumulative across multiple
                # plan/build cycles.  Only dismiss the UI — a later Revert can
                # still undo everything back to the original baseline.
                # Record current content of each file so the next diff only shows
                # files that were modified after this Keep.
                is_ssh = getattr(agent.backend, "_host", None) is not None
                for abs_path in agent.modified_files:
                    try:
                        if is_ssh:
                            _kept_file_contents[abs_path] = agent.backend.read_file(abs_path)
                        else:
                            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                                _kept_file_contents[abs_path] = f.read()
                    except Exception:
                        _kept_file_contents[abs_path] = ""
                awaiting_keep_revert = False
                feedback = "[System] The user accepted your changes."
                if kept_paths:
                    feedback += f" The following files were kept: {', '.join(kept_paths[:20])}"
                    if len(kept_paths) > 20:
                        feedback += f" (and {len(kept_paths) - 20} more)"
                agent.history.append({"role": "user", "content": feedback})
                save_session()
                await wsr.send_json({"type": "kept"})
                continue

            if msg_type == "revert" and awaiting_keep_revert:
                reverted = agent.revert_all()
                reverted_rel = [os.path.relpath(p, wd) for p in reverted]
                awaiting_keep_revert = False
                _kept_file_contents.clear()
                feedback = "[System] The user reverted your changes. The following files were reverted to their previous state: "
                feedback += ", ".join(reverted_rel[:20]) if reverted_rel else "(none)"
                if len(reverted_rel) > 20:
                    feedback += f" (and {len(reverted_rel) - 20} more)"
                agent.history.append({"role": "user", "content": feedback})
                save_session()
                await wsr.send_json({
                    "type": "reverted",
                    "files": reverted_rel,
                })
                continue

            if msg_type == "keep" and not awaiting_keep_revert:
                await wsr.send_json({"type": "info", "content": "No pending changes to accept."})
                continue

            if msg_type == "revert" and not awaiting_keep_revert:
                await wsr.send_json({"type": "info", "content": "No pending changes to revert."})
                continue

            # ── Revert to specific plan step ──────────────────────
            if msg_type == "revert_to_step":
                try:
                    step = int(data.get("step", 0) or 0)
                except (TypeError, ValueError):
                    step = 0
                if step < 1:
                    await wsr.send_json({
                        "type": "error",
                        "content": "Invalid step for revert (must be at least 1).",
                    })
                    continue
                had_checkpoint = step in getattr(agent, "_step_checkpoints", {})
                reverted = agent.revert_to_step(step)
                reverted_rel = [os.path.relpath(p, wd) for p in reverted]
                feedback = f"[System] The user reverted to step {step}. The following files were reverted: "
                feedback += ", ".join(reverted_rel[:20]) if reverted_rel else "(none)"
                if len(reverted_rel) > 20:
                    feedback += f" (and {len(reverted_rel) - 20} more)"
                agent.history.append({"role": "user", "content": feedback})
                save_session()
                await wsr.send_json({
                    "type": "reverted_to_step",
                    "step": step,
                    "files": reverted_rel,
                    "no_checkpoint": not had_checkpoint and len(reverted_rel) == 0,
                })
                continue

            # ── Add todo (user adds a task in real time; agent sees it on next TodoRead) ───
            if msg_type == "add_todo":
                content = (data.get("content") or "").strip()
                if content:
                    existing_ids = []
                    for t in agent._todos:
                        tid = t.get("id")
                        if isinstance(tid, int):
                            existing_ids.append(tid)
                        elif isinstance(tid, str) and tid.isdigit():
                            existing_ids.append(int(tid))
                    next_id = str(max(existing_ids, default=0) + 1)
                    agent._todos.append({"id": next_id, "content": content, "status": "pending"})
                    save_session()
                    await wsr.send_json({"type": "todos_updated", "todos": list(agent._todos)})
                continue

            # ── Remove todo (user removes a task; agent sees updated list on next TodoRead) ───
            if msg_type == "remove_todo":
                todo_id = data.get("id")
                if todo_id is not None:
                    before = len(agent._todos)
                    agent._todos = [t for t in agent._todos if str(t.get("id")) != str(todo_id)]
                    if len(agent._todos) != before:
                        save_session()
                    await wsr.send_json({"type": "todos_updated", "todos": list(agent._todos)})
                continue

            # ── Build (approve plan) ───────────────────────────────
            if msg_type == "build" and awaiting_build:
                awaiting_build = False
                edited_steps = data.get("steps") or pending_plan or []
                build_editor_ctx = data.get("context")
                _cancel_ack_sent = False
                _agent_task = asyncio.create_task(
                    _run_build_bg(pending_task or "", edited_steps, pending_images or [], editor_context=build_editor_ctx)
                )
                continue

            # ── Plan feedback (re-plan with user corrections) ──────
            if msg_type == "plan_feedback" and awaiting_build:
                feedback_text = (data.get("feedback") or "").strip()
                if not feedback_text:
                    await wsr.send_json({"type": "info", "content": "Empty feedback — please provide details."})
                    continue
                awaiting_build = False
                # Use the original task, not accumulated feedback chains
                original_task = pending_task or ""
                # Strip any previous feedback annotations to avoid growing chains
                if "\n\n[User feedback on plan]:" in original_task:
                    original_task = original_task.split("\n\n[User feedback on plan]:")[0]
                combined = f"{original_task}\n\n[User feedback on plan]: {feedback_text}"
                pending_task = None  # Clear to prevent further accumulation
                pending_plan = None
                _cancel_ack_sent = False
                _agent_task = asyncio.create_task(
                    _run_task_bg(combined, pending_images, editor_context=data.get("context"))
                )
                continue

            # ── Reject plan ────────────────────────────────────────
            if msg_type == "reject_plan":
                awaiting_build = False
                pending_plan = None
                pending_task = None
                pending_images = None
                save_session()
                await wsr.send_json({"type": "plan_rejected"})
                continue

            # ── Reset ──────────────────────────────────────────────
            if msg_type == "reset":
                if _agent_task and not _agent_task.done():
                    agent.cancel()
                    _agent_task.cancel()
                    _agent_task = None
                # Stop periodic save to prevent an in-flight background
                # save from overwriting the new (empty) session file after
                # we reset.  Wait for any running to_thread(save_session)
                # to finish so the file is stable before we write fresh state.
                if _save_interval_task and not _save_interval_task.done():
                    _save_interval_task.cancel()
                    try:
                        await _save_interval_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    _save_interval_task = None
                agent.reset()
                pending_plan = None
                pending_task = None
                pending_images = None
                awaiting_build = False
                awaiting_keep_revert = False
                _current_thinking_text = ""
                _current_text_buffer = ""
                old_session_id = session.session_id if session else None
                session = store.create_session(wd, model_config.model_id)
                save_session()
                # Update the shutdown save registry so the new session_id
                # is persisted if the server is killed before the next save.
                if old_session_id and old_session_id != session.session_id:
                    _active_save_fns.pop(old_session_id, None)
                _active_save_fns[session.session_id] = save_session
                # Restart periodic save with the clean state.
                _save_interval_task = asyncio.create_task(_periodic_save_loop())
                await wsr.send_json({
                    "type": "reset_done",
                    "session_id": session.session_id,
                    "session_name": session.name,
                })
                continue

            # ── New task ───────────────────────────────────────────
            if msg_type == "task":
                task_text = data.get("content", "").strip()
                try:
                    task_images = _normalize_user_images(data.get("images") or [])
                except ValueError as ve:
                    await wsr.send_json({"type": "error", "content": f"Image upload error: {ve}"})
                    continue

                if not task_text and not task_images:
                    continue
                if not task_text and task_images:
                    task_text = "Analyze the attached image(s) and help me with the request."

                # Don't start a new task while one is running
                if _agent_task and not _agent_task.done():
                    await wsr.send_json({"type": "error", "content": "Agent is already running. Cancel first."})
                    continue

                # If user sends a new task instead of clicking Keep/Revert,
                # auto-keep the changes and dismiss the keep/revert state.
                if awaiting_keep_revert:
                    awaiting_keep_revert = False
                    await wsr.send_json({"type": "clear_keep_revert"})

                # If user sends a new task while plan Build/Feedback/Reject is pending,
                # dismiss the plan gate (the new task supersedes it).
                if awaiting_build:
                    awaiting_build = False
                    pending_task = None
                    pending_plan = None
                    pending_images = None

                # Preserve snapshots whenever any exist so diffs stay cumulative
                # across multiple plan/build/Keep cycles in the same session.
                preserve_snapshots = bool(agent._file_snapshots)

                # Name session from first task — use LLM for smart titles
                if session and session.name == "default" and not agent.history:
                    if task_text:
                        # Set a quick placeholder immediately so UI isn't blank
                        words = task_text.split()[:6]
                        session.name = " ".join(words) + ("..." if len(task_text.split()) > 6 else "")
                        # Fire-and-forget: generate a proper title in background
                        _title_source = task_text

                        _title_session_id = session.session_id

                        async def _generate_session_title(source_text: str):
                            try:
                                loop = asyncio.get_event_loop()
                                title = await loop.run_in_executor(
                                    None, bedrock_service.generate_title, source_text
                                )
                                if title and session and session.session_id == _title_session_id:
                                    session.name = title
                                    save_session()
                                    try:
                                        await wsr.send_json({
                                            "type": "session_name_update",
                                            "session_id": session.session_id,
                                            "session_name": title,
                                        })
                                    except Exception:
                                        pass  # WS may have closed
                            except Exception as e:
                                logger.debug(f"Background title generation failed: {e}")

                        asyncio.create_task(_generate_session_title(_title_source))
                    else:
                        session.name = "Image prompt"

                editor_context = data.get("context")
                _cancel_ack_sent = False
                _agent_task = asyncio.create_task(_run_task_bg(task_text, task_images, preserve_snapshots, editor_context=editor_context))
                continue

    try:
        # Send initial info
        mcfg = get_model_config(model_config.model_id)
        # Display-friendly working directory
        if is_ssh and _state._ssh_info:
            display_wd = f"{_state._ssh_info['user']}@{_state._ssh_info['host']}:{_state._ssh_info['directory']}"
        else:
            display_wd = wd
        await wsr.send_json({
            "type": "init",
            "model_name": get_model_name(model_config.model_id),
            "working_directory": display_wd,
            "context_window": get_context_window(model_config.model_id),
            "thinking": supports_thinking(model_config.model_id),
            "caching": supports_caching(model_config.model_id),
            "session_id": session.session_id if session else "",
            "session_name": session.name if session else "default",
            "message_count": session.message_count if session else 0,
            "total_tokens": agent.total_tokens,
            "input_tokens": getattr(agent, "_total_input_tokens", 0),
            "output_tokens": getattr(agent, "_total_output_tokens", 0),
            "cache_read": getattr(agent, "_cache_read_tokens", 0),
            "is_ssh": is_ssh,
        })

        # Replay conversation history so frontend rebuilds the chat
        await replay_history()

        # If the server was killed mid-stream, restore the in-progress
        # thinking/text block so the user sees what was accumulated.
        if _current_thinking_text:
            await wsr.send_json({"type": "replay_thinking", "content": _current_thinking_text})
            _current_thinking_text = ""  # consumed — don't re-send
        if _current_text_buffer:
            await wsr.send_json({"type": "replay_text", "content": _current_text_buffer})
            _current_text_buffer = ""  # consumed

        # Start periodic save so SSH disconnect/kill doesn't lose conversation
        _save_interval_task = asyncio.create_task(_periodic_save_loop())

        # Kick off background codebase index build (first connect only)
        if _state._bg_index_task is None or _state._bg_index_task.done():
            _state._bg_index_ready.clear()
            _state._bg_index_task = asyncio.create_task(
                _build_index_background(_state._backend or backend, agent_wd)
            )

        # ── Background task helpers ─────────────────────────────
        async def _send_status():
            """Send token count status to frontend."""
            try:
                ctx_window = get_context_window(model_config.model_id)
                ctx_est = agent._current_token_estimate() if hasattr(agent, '_current_token_estimate') else 0
                await wsr.send_json({
                    "type": "status",
                    "tokens": agent.total_tokens,
                    "input_tokens": agent._total_input_tokens,
                    "output_tokens": agent._total_output_tokens,
                    "cache_read": agent._cache_read_tokens,
                    "cache_write": agent._cache_write_tokens,
                    "context_usage_pct": round(ctx_est / ctx_window * 100) if ctx_window else 0,
                })
            except Exception:
                pass

        # Send status so token badge and context gauge are correct after connect (not stale 0 / yellow)
        await _send_status()

        async def _request_question_answer(
            question: str,
            context: Optional[str],
            tool_use_id: str,
            *,
            options: Optional[List[str]] = None,
        ) -> str:
            _pending_question["future"] = asyncio.get_event_loop().create_future()
            _pending_question["tool_use_id"] = tool_use_id
            try:
                payload = {
                    "type": "user_question",
                    "question": question,
                    "context": context or "",
                    "tool_use_id": tool_use_id,
                }
                if options:
                    payload["options"] = options
                await wsr.send_json(payload)
                return await asyncio.wait_for(_pending_question["future"], timeout=300.0)  # 5 min max
            finally:
                _pending_question["future"] = None
                _pending_question["tool_use_id"] = None

        async def _detect_and_apply_plan_update(ag, current_plan):
            """Check agent's last response for <updated_plan> tags and update plan/checklist."""
            nonlocal pending_plan
            if not ag.history:
                return
            last_msg = ag.history[-1]
            text = ""
            if isinstance(last_msg.get("content"), str):
                text = last_msg["content"]
            elif isinstance(last_msg.get("content"), list):
                for block in last_msg["content"]:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text += block.get("text", "")
            if not text:
                return

            # Look for <updated_plan>...</updated_plan> tags
            match = re.search(r"<updated_plan>\s*(.*?)\s*</updated_plan>", text, re.DOTALL)
            if not match:
                return

            plan_body = match.group(1).strip()
            if not plan_body:
                return

            # Parse numbered steps
            new_steps = []
            for line in plan_body.split("\n"):
                line = line.strip()
                if re.match(r"^\d+[\.\)]\s+", line):
                    new_steps.append(line)

            if not new_steps:
                return

            # Update state
            pending_plan = new_steps
            ag._current_plan = new_steps
            plan_text_full = "\n".join(new_steps)
            ag._plan_text = plan_text_full

            # Emit updated plan event to frontend
            await wsr.send_json({
                "type": "updated_plan",
                "steps": new_steps,
                "plan_text": plan_text_full,
                "plan_file": getattr(ag, "_plan_file_path", None),
                "plan_title": getattr(ag, "_plan_title", "") or "",
            })

            # Also update the checklist
            todos = [
                {"id": str(i + 1), "content": s, "status": "pending"}
                for i, s in enumerate(new_steps)
            ]
            ag._todos = todos
            await wsr.send_json({"type": "todos_updated", "todos": todos})

        def _format_error_for_client(exc: BaseException) -> str:
            """Format an exception for the UI. Avoid showing raw paths or opaque 'Unknown' messages."""
            name = type(exc).__name__
            msg = str(exc).strip()
            if not msg or ".py:" in msg or (len(msg) > 2 and msg[0] == "/" and "/" in msg[1:]):
                return f"{name} (see server logs for details)"
            out = f"{name}: {msg}"
            return out[:500] + "..." if len(out) > 500 else out

        async def _run_task_bg(task_text: str, task_images: Optional[List[Dict[str, Any]]] = None, preserve_snapshots: bool = False, editor_context: Optional[Dict[str, Any]] = None):
            """Run a task (plan or direct mode) in the background. preserve_snapshots=True keeps diff/revert cumulative."""
            nonlocal awaiting_build, awaiting_keep_revert, pending_task, pending_plan, pending_images
            task_start = time.time()

            # Immediate feedback so the user sees activity right away
            await wsr.send_json({"type": "scout_progress", "content": "Preparing\u2026"})

            # Run auto-context, mention resolution, and intent classification in parallel
            async def _auto_ctx_task():
                try:
                    return await asyncio.wait_for(
                        asyncio.to_thread(
                            _assemble_auto_context,
                            _state._working_directory,
                            editor_context,
                            agent.modified_files if hasattr(agent, "modified_files") else None,
                            backend=_state._backend,
                            user_query=task_text,
                        ),
                        timeout=60.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning("Auto-context assembly timed out after 60s")
                    return ""
                except Exception as e:
                    logger.debug(f"Auto-context assembly failed: {e}")
                    return ""

            async def _mentions_task():
                try:
                    return await asyncio.wait_for(
                        asyncio.to_thread(
                            _resolve_mentions, task_text, _state._working_directory, backend=_state._backend
                        ),
                        timeout=10.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning("Mention resolution timed out after 10s")
                    return task_text
                except Exception as e:
                    logger.debug(f"Mention resolution failed: {e}")
                    return task_text

            async def _intent_task():
                try:
                    return await asyncio.wait_for(
                        asyncio.to_thread(classify_intent, task_text, agent.service),
                        timeout=8.0,
                    )
                except (asyncio.TimeoutError, Exception):
                    return {"scout": True, "plan": False, "question": False, "complexity": "complex"}

            auto_ctx, task_text, intent = await asyncio.gather(
                _auto_ctx_task(), _mentions_task(), _intent_task()
            )

            # End the "Preparing" indicator now that pre-processing is done
            await wsr.send_json({"type": "scout_end"})

            try:
                is_question = intent.get("question", False)
                if app_config.plan_phase_enabled and intent.get("plan") and not is_question and not awaiting_build:
                    # Plan phase — inject auto-context into plan task too
                    plan_task = (auto_ctx + "\n\n" + task_text) if auto_ctx else task_text
                    plan_steps = await agent.run_plan(
                        task=plan_task,
                        on_event=on_event,
                        request_question_answer=_request_question_answer,
                        user_images=task_images or [],
                    )
                    if agent._cancelled:
                        return

                    elapsed = round(time.time() - task_start, 1)
                    await wsr.send_json({"type": "phase_end", "content": "plan", "elapsed": elapsed})

                    if plan_steps:
                        pending_task = task_text
                        pending_plan = plan_steps
                        pending_images = list(task_images or [])
                        awaiting_build = True
                        try:
                            await asyncio.to_thread(save_session)
                        except Exception:
                            pass
                    else:
                        await wsr.send_json({"type": "no_plan"})
                    await wsr.send_json({"type": "done", "data": {
                        "tokens": agent.total_tokens,
                        "input_tokens": agent._total_input_tokens,
                        "output_tokens": agent._total_output_tokens,
                    }})
                else:
                    # Direct mode — but first check if the user is asking to execute the pending plan
                    if awaiting_build and pending_plan and not is_question:
                        _exec_kws = ("implement", "execute", "go ahead", "do it", "build",
                                     "apply", "run it", "start building", "proceed")
                        _lower = task_text.lower().strip()
                        if any(kw in _lower for kw in _exec_kws):
                            awaiting_build = False
                            _cancel_ack_sent = False
                            _agent_task = asyncio.create_task(
                                _run_build_bg(pending_task or task_text, pending_plan, pending_images or [], editor_context=editor_context)
                            )
                            return

                    # If user has a pending plan (didn't press Build), inject plan into context so
                    # follow-up questions ("change step 2 to...") have the plan in scope.
                    task_for_run = task_text
                    _is_plan_followup = False
                    if awaiting_build and (pending_plan or getattr(agent, "_plan_text", None)):
                        _is_plan_followup = True
                        plan_text = getattr(agent, "_plan_text", "") or ""
                        if not plan_text and pending_plan:
                            plan_text = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(pending_plan))
                        if plan_text.strip():
                            task_for_run = (
                                "[Current plan (user has not run Build yet; use for context):\n<current_plan>\n"
                                + plan_text.strip()
                                + "\n</current_plan>]\n\n"
                                "IMPORTANT: If the user asks to modify, add, remove, or change any plan steps, "
                                "you MUST output the complete updated plan as a numbered list wrapped in "
                                "<updated_plan> tags. For example:\n"
                                "<updated_plan>\n1. First step\n2. Second step\n</updated_plan>\n"
                                "Always include ALL steps (not just changed ones). "
                                "If the user is just asking a question about the plan, answer normally without the tags.\n\n"
                                "User's message: "
                                + task_text
                            )

                    # Inject auto-context if available
                    if auto_ctx:
                        task_for_run = auto_ctx + "\n\n" + task_for_run

                    # Smart model routing: use fast model for trivial/simple tasks
                    complexity = intent.get("complexity", "complex")
                    original_model = agent.service.model_id
                    if complexity in ("trivial", "simple") and app_config.fast_model:
                        agent.service.model_id = app_config.fast_model
                        logger.info(f"Smart routing: using fast model for {complexity} task")

                    # Smart scout skip: if auto-context already provides structure or semantic context, skip scout
                    enable_scout = intent.get("scout", True)
                    if auto_ctx and (
                        active_file_in_context(auto_ctx)
                        or "<project_structure>" in auto_ctx
                        or "<semantic_context>" in auto_ctx
                    ):
                        enable_scout = False
                        logger.info("Smart scout skip: auto-context provides sufficient context")

                    await wsr.send_json({"type": "phase_start", "content": "direct"})
                    try:
                        await agent.run(
                            task=task_for_run,
                            on_event=on_event,
                            request_approval=dummy_approval,
                            enable_scout=enable_scout,
                            user_images=task_images or [],
                            preserve_snapshots=preserve_snapshots,
                            request_question_answer=_request_question_answer,
                        )
                    finally:
                        # Always restore the original model
                        agent.service.model_id = original_model
                    if agent._cancelled:
                        return

                    # Check if the agent's response contains an updated plan
                    if _is_plan_followup and agent.history:
                        await _detect_and_apply_plan_update(agent, pending_plan)

                    elapsed = round(time.time() - task_start, 1)
                    await wsr.send_json({"type": "phase_end", "content": "direct", "elapsed": elapsed})
                    await wsr.send_json({"type": "done"})

                    # Show cumulative diff and Keep/Revert bar for ALL accumulated changes
                    if agent.modified_files:
                        awaiting_keep_revert = True
                        diffs = generate_diffs()
                        if diffs:
                            await wsr.send_json({"type": "diff", "files": diffs, "cumulative": True})
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.exception("Task error")
                try:
                    await wsr.send_json({"type": "error", "content": _format_error_for_client(exc)})
                except Exception:
                    pass
            finally:
                try:
                    save_session()
                except Exception as save_err:
                    logger.warning("Session save after task failed: %s", save_err)
                try:
                    await _send_status()
                except Exception:
                    pass

        async def _run_build_bg(task_text: str, steps: list, task_images: Optional[List[Dict[str, Any]]] = None, editor_context: Optional[Dict[str, Any]] = None):
            """Run build phase in the background."""
            nonlocal awaiting_keep_revert
            task_start = time.time()
            try:
                # Auto-create todos from plan steps so checklist is populated immediately
                agent._todos = [
                    {"id": str(i + 1), "content": s, "status": "pending"}
                    for i, s in enumerate(steps)
                ]
                await wsr.send_json({"type": "todos_updated", "todos": list(agent._todos)})

                # If the user has an active file open (possibly with edits), let the agent know
                build_task = task_text
                if editor_context:
                    ctx_parts = []
                    af = editor_context.get("activeFile")
                    if af and af.get("path"):
                        ctx_parts.append(f"User currently has {af['path']} open in editor (line {af.get('cursorLine', '?')}).")
                    sel = editor_context.get("selectedText")
                    if sel:
                        ctx_parts.append(f"User has selected text:\n```\n{sel}\n```")
                    of = editor_context.get("openFiles")
                    if of:
                        ctx_parts.append(f"Open files: {', '.join(of[:10])}")
                    if ctx_parts:
                        build_task = "[Editor context: " + " ".join(ctx_parts) + "]\n\n" + build_task

                await wsr.send_json({"type": "phase_start", "content": "build"})
                await agent.run_build(
                    task=build_task,
                    plan_steps=steps,
                    on_event=on_event,
                    request_approval=dummy_approval,
                    user_images=task_images or [],
                    request_question_answer=_request_question_answer,
                )
                if agent._cancelled:
                    return

                elapsed = round(time.time() - task_start, 1)
                await wsr.send_json({"type": "phase_end", "content": "build", "elapsed": elapsed})
                await wsr.send_json({"type": "done"})

                # Show cumulative diff and Keep/Revert bar for ALL accumulated changes
                if agent.modified_files:
                    awaiting_keep_revert = True
                    diffs = generate_diffs()
                    if diffs:
                        await wsr.send_json({"type": "diff", "files": diffs, "cumulative": True})
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.exception("Build error")
                try:
                    await wsr.send_json({"type": "error", "content": _format_error_for_client(exc)})
                except Exception:
                    pass
            finally:
                try:
                    save_session()
                except Exception as save_err:
                    logger.warning("Session save after build failed: %s", save_err)
                try:
                    await _send_status()
                except Exception:
                    pass

        # ── Background file watcher ────────────────────────────
        _file_mtimes: Dict[str, float] = {}
        async def _file_watcher_local():
            """Lightweight polling watcher that detects external file changes (local)."""
            POLL_INTERVAL = 3  # seconds
            IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".bedrock-codex", ".venv", "venv"}
            try:
                # Initial scan
                for root, dirs, files in os.walk(agent_wd):
                    dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith(".")]
                    for fname in files[:200]:  # cap per directory
                        fpath = os.path.join(root, fname)
                        try:
                            _file_mtimes[fpath] = os.path.getmtime(fpath)
                        except OSError:
                            pass

                while True:
                    await asyncio.sleep(POLL_INTERVAL)
                    agent_busy = _agent_task is not None and not _agent_task.done()
                    changed = []
                    for root, dirs, files in os.walk(agent_wd):
                        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith(".")]
                        for fname in files[:200]:
                            fpath = os.path.join(root, fname)
                            try:
                                mtime = os.path.getmtime(fpath)
                                prev = _file_mtimes.get(fpath)
                                if prev is None or mtime > prev + 0.1:
                                    _file_mtimes[fpath] = mtime
                                    if not agent_busy and prev is not None:
                                        changed.append(os.path.relpath(fpath, agent_wd))
                            except OSError:
                                pass

                    for rel in changed[:10]:  # cap events per poll
                        try:
                            await wsr.send_json({"type": "file_changed", "path": rel})
                        except Exception:
                            return
            except asyncio.CancelledError:
                return
            except Exception:
                pass  # watcher failure is non-fatal

        async def _file_watcher_ssh():
            """Polling watcher for SSH backends using remote `find -newer`."""
            POLL_INTERVAL = 5  # slightly longer for SSH to reduce overhead
            REF_FILE = ".bedrock-codex/.watcher_ref"
            PRUNE = "-name .git -prune -o -name node_modules -prune -o -name __pycache__ -prune -o -name .venv -prune -o -name venv -prune -o"
            try:
                # Create the reference marker file on the remote
                await asyncio.to_thread(
                    backend.run_command,
                    f"mkdir -p .bedrock-codex && touch {REF_FILE}", ".", 5,
                )
                # Let the ref file settle so first poll doesn't report everything
                await asyncio.sleep(1)
                await asyncio.to_thread(
                    backend.run_command, f"touch {REF_FILE}", ".", 5,
                )

                while True:
                    await asyncio.sleep(POLL_INTERVAL)
                    agent_busy = _agent_task is not None and not _agent_task.done()
                    if agent_busy:
                        # Touch ref so agent edits don't flood on next poll
                        await asyncio.to_thread(
                            backend.run_command, f"touch {REF_FILE}", ".", 5,
                        )
                        continue

                    out, _, rc = await asyncio.to_thread(
                        backend.run_command,
                        f"find . {PRUNE} -newer {REF_FILE} -type f -print 2>/dev/null | head -20",
                        ".", 10,
                    )
                    # Touch ref immediately so next poll measures from now
                    await asyncio.to_thread(
                        backend.run_command, f"touch {REF_FILE}", ".", 5,
                    )
                    if rc != 0 or not out:
                        continue

                    changed = []
                    for line in out.strip().splitlines():
                        rel = line.strip().lstrip("./")
                        if rel and not rel.startswith(".bedrock-codex/"):
                            changed.append(rel)

                    for rel in changed[:10]:
                        try:
                            await wsr.send_json({"type": "file_changed", "path": rel})
                        except Exception:
                            return
            except asyncio.CancelledError:
                return
            except Exception:
                pass  # watcher failure is non-fatal

        if isinstance(backend, LocalBackend):
            _watcher_task = asyncio.create_task(_file_watcher_local())
        else:
            _watcher_task = asyncio.create_task(_file_watcher_ssh())

        # ── Message loop ───────────────────────────────────────
        await _message_loop()

    except WebSocketDisconnect:
        # Cancel disposable background tasks
        if _save_interval_task and not _save_interval_task.done():
            _save_interval_task.cancel()
        if _watcher_task and not _watcher_task.done():
            _watcher_task.cancel()

        if _agent_task and not _agent_task.done():
            # ── Agent still running: wait for client to reconnect ──
            wsr.ws = None  # sends become silent no-ops while disconnected
            _rc_future: asyncio.Future = asyncio.get_event_loop().create_future()
            _reconnect_sessions[session.session_id] = {"future": _rc_future}
            save_session()
            logger.info("WS disconnected — agent still running (session %s). Waiting for reconnect…", session.session_id)

            _new_ws = None
            _done_event = None
            try:
                _new_ws, _done_event = await asyncio.wait_for(_rc_future, timeout=300)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                logger.info("Reconnect timeout / cancelled (session %s) — cleaning up", session.session_id)
                agent.cancel()
                if _agent_task and not _agent_task.done():
                    _agent_task.cancel()
                save_session()
                _reconnect_sessions.pop(session.session_id, None)
                _state._active_agent = None
                return

            # Reconnected!
            _reconnect_sessions.pop(session.session_id, None)
            ws = _new_ws
            wsr.ws = _new_ws
            logger.info("WS reconnected (session %s)", session.session_id)

            try:
                # Re-send init + replay so the frontend rebuilds the chat
                mcfg = get_model_config(model_config.model_id)
                if is_ssh and _state._ssh_info:
                    display_wd = f"{_state._ssh_info['user']}@{_state._ssh_info['host']}:{_state._ssh_info['directory']}"
                else:
                    display_wd = wd
                await wsr.send_json({
                    "type": "init",
                    "model_name": get_model_name(model_config.model_id),
                    "working_directory": display_wd,
                    "context_window": get_context_window(model_config.model_id),
                    "thinking": supports_thinking(model_config.model_id),
                    "caching": supports_caching(model_config.model_id),
                    "session_id": session.session_id if session else "",
                    "session_name": session.name if session else "default",
                    "message_count": session.message_count if session else 0,
                    "total_tokens": agent.total_tokens,
                    "input_tokens": getattr(agent, "_total_input_tokens", 0),
                    "output_tokens": getattr(agent, "_total_output_tokens", 0),
                    "cache_read": getattr(agent, "_cache_read_tokens", 0),
                    "is_ssh": is_ssh,
                })
                await replay_history()
                _agent_running = _agent_task is not None and not _agent_task.done()

                # If the agent was mid-stream when we disconnected, replay
                # the buffered content so the frontend picks up where it left off.
                if _agent_running:
                    if _current_thinking_text:
                        await wsr.send_json({"type": "thinking_start", "content": ""})
                        await wsr.send_json({"type": "thinking", "content": _current_thinking_text})
                    if _current_text_buffer:
                        await wsr.send_json({"type": "text_start", "content": ""})
                        await wsr.send_json({"type": "text", "content": _current_text_buffer})

                await wsr.send_json({"type": "resumed", "agent_running": _agent_running})
                await _send_status()

                # Restart disposable tasks
                _save_interval_task = asyncio.create_task(_periodic_save_loop())
                if isinstance(backend, LocalBackend):
                    _watcher_task = asyncio.create_task(_file_watcher())

                # Re-enter message loop with the new WebSocket
                await _message_loop()

            except WebSocketDisconnect:
                # Second disconnect — clean up normally
                if _save_interval_task and not _save_interval_task.done():
                    _save_interval_task.cancel()
                if _watcher_task and not _watcher_task.done():
                    _watcher_task.cancel()
                if _agent_task and not _agent_task.done():
                    agent.cancel()
                    _agent_task.cancel()
                save_session()
                logger.info("WS disconnected again after reconnect")
            except Exception as exc:
                if _agent_task and not _agent_task.done():
                    agent.cancel()
                    _agent_task.cancel()
                logger.exception("WS error after reconnect: %s", exc)
                save_session()
            finally:
                if _done_event:
                    _done_event.set()
        else:
            # No running task — normal disconnect
            save_session()
            logger.info("WebSocket disconnected")
        _state._active_agent = None
        if session and session.session_id:
            _active_save_fns.pop(session.session_id, None)
    except Exception as e:
        if _save_interval_task and not _save_interval_task.done():
            _save_interval_task.cancel()
        if _agent_task and not _agent_task.done():
            agent.cancel()
            _agent_task.cancel()
        logger.exception(f"WebSocket error: {e}")
        save_session()
        _state._active_agent = None
        if session and session.session_id:
            _active_save_fns.pop(session.session_id, None)
    finally:
        # Unconditional last-resort save — catches CancelledError (Ctrl+C)
        # and any path that might skip save_session() above.
        try:
            save_session()
        except Exception:
            pass
        _state._active_agent = None
        if session and session.session_id:
            _active_save_fns.pop(session.session_id, None)
