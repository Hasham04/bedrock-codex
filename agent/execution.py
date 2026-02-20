"""
Execution engine for the coding agent.
Contains the core agent loop and parallel tool execution logic.
"""

import asyncio
import json
import logging
import os
import queue
import re
import threading
import time
from collections import defaultdict
from typing import List, Dict, Any, Optional, Callable, Awaitable

from bedrock_service import GenerationConfig, BedrockError
from tools import (
    TOOL_DEFINITIONS, SAFE_TOOLS, execute_tool, ToolResult, ASK_USER_QUESTION_DEFINITION,
    NATIVE_EDITOR_NAME, NATIVE_BASH_NAME, NATIVE_WEB_SEARCH_NAME, EDITOR_WRITE_COMMANDS,
)
from config import app_config, get_context_window

from .events import AgentEvent

logger = logging.getLogger(__name__)
_PATH_RE = re.compile(r'"path"\s*:\s*"([^"]+)"')


# ---------------------------------------------------------------------------
# Native tool classification helpers
# ---------------------------------------------------------------------------

def _is_file_write_tool(tu: Dict[str, Any]) -> bool:
    """True if this tool_use performs a file-modifying operation."""
    name = tu.get("name", "")
    if name == NATIVE_EDITOR_NAME:
        return tu.get("input", {}).get("command") in EDITOR_WRITE_COMMANDS
    return name == "symbol_edit"


def _is_file_read_tool(tu: Dict[str, Any]) -> bool:
    """True if this tool_use is a read-only file view."""
    return (tu.get("name", "") == NATIVE_EDITOR_NAME
            and tu.get("input", {}).get("command") == "view")


def _is_bash_tool(name: str) -> bool:
    return name == NATIVE_BASH_NAME


def _get_tool_path(tu: Dict[str, Any]) -> str:
    """Extract the file path from a tool_use block (works for native + custom tools)."""
    return tu.get("input", {}).get("path", "?")


class ExecutionMixin:
    """Mixin providing the core agent loop and tool execution capabilities.
    
    Expects the host class to provide:
    - self.service (BedrockService)
    - self.backend (Backend)
    - self.working_directory (str)
    - self.history (list)
    - self.max_iterations (int)
    - self._cancelled (bool)
    - self._file_snapshots (dict) via ContextMixin
    - self._snapshot_file() via ContextMixin
    - self._policy_decision() via VerificationMixin
    - self._record_failure_pattern() via VerificationMixin
    - self._todos (list) via ContextMixin
    - self._memory (dict) via ContextMixin
    - self._approved_commands (set) via ContextMixin
    - self._total_input_tokens, _total_output_tokens, etc. via ContextMixin
    - self._history_len_at_last_call (int) via core
    - self._consecutive_stream_errors (int) via core
    - self._last_stream_error_sig (str) via core
    """

    async def _agent_loop(
        self,
        on_event: Callable[[AgentEvent], Awaitable[None]],
        request_approval: Callable[[str, str, Dict], Awaitable[bool]],
        config: Optional[GenerationConfig] = None,
        request_question_answer: Optional[Callable[..., Awaitable[str]]] = None,
    ):
        """Core streaming agent loop with tool execution."""
        if app_config.codebase_index_enabled and hasattr(self.service, "embed_texts"):
            try:
                from codebase_index import set_embed_fn
                set_embed_fn(self.service.embed_texts)
            except Exception:
                pass
        gen_config = config or self._default_config()
        iteration = 0
        reasoning_trace_repairs = 0

        while iteration < self.max_iterations and not self._cancelled:
            iteration += 1

            # Check for user guidance injected mid-task
            guidance = self._consume_guidance()
            if guidance:
                self.history.append({
                    "role": "user",
                    "content": (
                        f"[USER GUIDANCE — mid-task correction from the user. "
                        f"Incorporate this into your current work immediately.]\n\n{guidance}"
                    ),
                })
                await on_event(AgentEvent(
                    type="guidance_applied",
                    content=guidance,
                ))

            # Soft limit: when approaching max iterations, tell the model to wrap up
            soft_limit = int(self.max_iterations * 0.85)
            if iteration == soft_limit:
                self.history.append({
                    "role": "user",
                    "content": (
                        f"[SYSTEM] You have used {iteration} of {self.max_iterations} iterations. "
                        "You are approaching the limit. Please wrap up your current task — "
                        "summarize what you've done so far and what remains, then stop."
                    ),
                })

            # Trim history if approaching context window limit
            await self._trim_history()

            # Validate history — fix orphaned tool_use blocks
            self._repair_history()

            # -----------------------------------------------------------
            # Stream with retry — recovers from connection drops
            # -----------------------------------------------------------
            max_retries = app_config.stream_max_retries
            retry_backoff = app_config.stream_retry_backoff
            stream_succeeded = False
            _guidance_interrupted = False

            # Snapshot token counters so we can rollback on retry
            snapshot_input = self._total_input_tokens
            snapshot_output = self._total_output_tokens
            snapshot_cache_read = self._cache_read_tokens
            snapshot_cache_write = self._cache_write_tokens

            last_stop_reason: Optional[str] = None
            should_auto_continue = False  # max_tokens cut-off: next iteration will send continuation
            for attempt in range(1, max_retries + 1):
                try:
                    # Reset per-attempt accumulators
                    assistant_content = []
                    current_tool_use = None
                    tool_use_json_parts: List[str] = []
                    current_text = ""
                    current_thinking = ""
                    current_thinking_signature: Optional[str] = None
                    server_tool_block: Optional[Dict[str, Any]] = None
                    web_search_result_block: Optional[Dict[str, Any]] = None

                    # Rollback token counters to pre-attempt snapshot
                    self._total_input_tokens = snapshot_input
                    self._total_output_tokens = snapshot_output
                    self._cache_read_tokens = snapshot_cache_read
                    self._cache_write_tokens = snapshot_cache_write

                    if attempt > 1:
                        # Tell the UI we are retrying
                        await on_event(AgentEvent(
                            type="stream_retry",
                            content=f"Connection lost — retrying ({attempt}/{max_retries})...",
                            data={"attempt": attempt, "max_retries": max_retries},
                        ))

                    chunk_queue: queue.Queue = queue.Queue()

                    build_tools = (TOOL_DEFINITIONS + [ASK_USER_QUESTION_DEFINITION]) if request_question_answer else TOOL_DEFINITIONS

                    def _stream_producer():
                        """Run the sync generator in a background thread, forwarding chunks to the queue."""
                        try:
                            for c in self.service.generate_response_stream(
                                messages=self.history,
                                system_prompt=self._effective_system_prompt(self.system_prompt),
                                model_id=None,
                                config=gen_config,
                                tools=build_tools,
                            ):
                                chunk_queue.put(c)
                            chunk_queue.put(None)  # sentinel: stream complete
                        except Exception as exc:
                            chunk_queue.put(exc)

                    producer_thread = threading.Thread(target=_stream_producer, daemon=True)
                    producer_thread.start()

                    # Consume chunks from the queue in the async loop
                    loop = asyncio.get_event_loop()
                    _guidance_interrupted = False
                    while True:
                        if self._cancelled:
                            break
                        if self._guidance_interrupt:
                            _guidance_interrupted = True
                            break

                        chunk = await loop.run_in_executor(None, chunk_queue.get)

                        if chunk is None:
                            break  # stream complete
                        if isinstance(chunk, Exception):
                            raise chunk

                        chunk_type = chunk.get("type", "")
                        content = chunk.get("content", "")

                        # --- Thinking events (with continuity) ---
                        if chunk_type == "thinking_start":
                            current_thinking = ""
                            current_thinking_signature = None
                            await on_event(AgentEvent(type="thinking_start"))
                        elif chunk_type == "thinking":
                            current_thinking += content
                            await on_event(AgentEvent(type="thinking", content=content))
                        elif chunk_type == "thinking_end":
                            # Capture signature for thinking continuity
                            current_thinking_signature = chunk.get("signature")
                            # Preserve thinking block in assistant content for multi-turn continuity
                            thinking_block: Dict[str, Any] = {
                                "type": "thinking",
                                "thinking": current_thinking,
                            }
                            if current_thinking_signature:
                                thinking_block["signature"] = current_thinking_signature
                            assistant_content.append(thinking_block)
                            await on_event(AgentEvent(type="thinking_end"))

                        # --- Text events ---
                        elif chunk_type == "text_start":
                            current_text = ""
                            await on_event(AgentEvent(type="text_start"))
                        elif chunk_type == "text":
                            current_text += content
                            await on_event(AgentEvent(type="text", content=content))
                        elif chunk_type == "text_end":
                            if current_text:
                                assistant_content.append({"type": "text", "text": current_text})
                                # Track plan step progress from assistant text
                                new_step = self._detect_plan_step(current_text)
                                if new_step is not None:
                                    await on_event(AgentEvent(
                                        type="plan_step_progress",
                                        content=str(new_step),
                                        data={
                                            "step": new_step,
                                            "total": len(self._current_plan) if self._current_plan else 0,
                                        },
                                    ))
                            await on_event(AgentEvent(type="text_end"))

                        # --- Tool use events ---
                        elif chunk_type == "tool_use_start":
                            current_tool_use = chunk.get("data", {})
                            tool_use_json_parts = []
                            _tool_input_bytes = 0
                            _tool_input_path = ""
                            await on_event(AgentEvent(
                                type="tool_use_start",
                                content=current_tool_use.get("name", ""),
                                data={
                                    "id": current_tool_use.get("id", ""),
                                    "name": current_tool_use.get("name", ""),
                                },
                            ))
                        elif chunk_type == "tool_use_delta":
                            tool_use_json_parts.append(content)
                            _tool_input_bytes += len(content)
                            if not _tool_input_path and _tool_input_bytes < 1000 and current_tool_use:
                                partial = "".join(tool_use_json_parts)
                                m = _PATH_RE.search(partial)
                                if m:
                                    _tool_input_path = m.group(1)
                            if _tool_input_bytes % 2000 < len(content):
                                await on_event(AgentEvent(
                                    type="tool_input_delta",
                                    content="",
                                    data={
                                        "id": current_tool_use.get("id", "") if current_tool_use else "",
                                        "bytes": _tool_input_bytes,
                                        "path": _tool_input_path,
                                    },
                                ))
                        elif chunk_type == "tool_use_end":
                            if current_tool_use:
                                try:
                                    input_json = json.loads("".join(tool_use_json_parts))
                                except json.JSONDecodeError:
                                    input_json = {}

                                tool_block = {
                                    "type": "tool_use",
                                    "id": current_tool_use.get("id", ""),
                                    "name": current_tool_use.get("name", ""),
                                    "input": input_json,
                                }
                                assistant_content.append(tool_block)

                                await on_event(AgentEvent(
                                    type="tool_call",
                                    content=current_tool_use.get("name", ""),
                                    data={
                                        "id": current_tool_use.get("id", ""),
                                        "name": current_tool_use.get("name", ""),
                                        "input": input_json,
                                    },
                                ))
                                current_tool_use = None

                        # --- Server-side tool events (web_search) ---
                        elif chunk_type == "server_tool_use_start":
                            server_tool_data = chunk.get("data", {})
                            server_tool_block = {
                                "type": "server_tool_use",
                                "id": server_tool_data.get("id", ""),
                                "name": server_tool_data.get("name", ""),
                                "input": server_tool_data.get("input", {}),
                            }
                            await on_event(AgentEvent(
                                type="server_tool_use",
                                content=server_tool_data.get("name", ""),
                                data=server_tool_data,
                            ))
                        elif chunk_type == "server_tool_use_end":
                            if server_tool_block:
                                assistant_content.append(server_tool_block)
                                server_tool_block = None
                        elif chunk_type == "web_search_result":
                            ws_data = chunk.get("data", {})
                            web_search_result_block = {
                                "type": "web_search_tool_result",
                                "tool_use_id": ws_data.get("tool_use_id", ""),
                                "content": ws_data.get("content", []),
                            }
                            await on_event(AgentEvent(
                                type="web_search_result",
                                content="",
                                data=ws_data,
                            ))
                        elif chunk_type == "web_search_result_end":
                            if web_search_result_block:
                                assistant_content.append(web_search_result_block)
                                web_search_result_block = None

                        # --- Usage / cache metrics ---
                        elif chunk_type == "usage_start":
                            usage = chunk.get("usage", {})
                            self._total_input_tokens += usage.get("input_tokens", 0)
                            self._cache_read_tokens += usage.get("cache_read_input_tokens", 0)
                            self._cache_write_tokens += usage.get("cache_creation_input_tokens", 0)

                        elif chunk_type == "message_end":
                            usage = chunk.get("usage", {})
                            self._total_output_tokens += usage.get("output_tokens", 0)
                            last_stop_reason = chunk.get("stop_reason") or None

                    producer_thread.join(timeout=5)

                    # If guidance arrived mid-stream, discard partial response and restart
                    if _guidance_interrupted:
                        assistant_content = []
                        tool_calls = []
                        current_text = ""
                        current_tool_use = None
                        await on_event(AgentEvent(
                            type="guidance_interrupt",
                            content="Guidance received — restarting with your correction.",
                        ))
                        break  # exit retry loop — will be caught below

                    stream_succeeded = True
                    self._history_len_at_last_call = len(self.history)
                    self._consecutive_stream_errors = 0
                    self._last_stream_error_sig = ""
                    break  # exit retry loop — stream completed

                except (BedrockError, Exception) as stream_err:
                    producer_thread.join(timeout=2)

                    # Determine if this error is retryable (connection/timeout/throttle/token limit)
                    err_str = str(stream_err).lower()
                    retryable_keywords = [
                        "timeout", "timed out", "connection", "reset by peer",
                        "broken pipe", "eof", "throttl", "serviceunav",
                        "read timeout", "endpoint url", "connect timeout",
                        "network", "socket", "aborted",
                        "max_tokens", "token limit", "ran out of tokens", "output length",
                        "context length", "input length",
                    ]
                    is_retryable = any(kw in err_str for kw in retryable_keywords)

                    if not is_retryable or attempt >= max_retries:
                        # Non-retryable error or exhausted retries
                        if attempt >= max_retries and is_retryable:
                            err_msg = f"Stream failed after {max_retries} retries: {stream_err}"
                        else:
                            err_msg = str(stream_err)
                        logger.error(f"Stream error (attempt {attempt}): {stream_err}")

                        # Rollback: clean up history so it's valid for the API.
                        # The API requires every tool_use to have a matching
                        # tool_result in the immediately following user message.

                        # Track recurring errors to prevent unbounded rollbacks
                        error_sig = err_str[:200]
                        if error_sig == self._last_stream_error_sig:
                            self._consecutive_stream_errors += 1
                        else:
                            self._consecutive_stream_errors = 1
                            self._last_stream_error_sig = error_sig

                        rollback_count = 0

                        if self._consecutive_stream_errors >= 3:
                            # Same error 3+ times — rollbacks aren't helping.
                            # Use repair (inserts dummy tool_results) instead
                            # of popping more messages.
                            logger.warning(
                                f"Recurring stream error ({self._consecutive_stream_errors}x) "
                                "— repairing history instead of rolling back"
                            )
                            self._repair_history()
                        else:
                            # Normal rollback: remove trailing user + orphaned
                            # assistant tool_use from this turn only
                            if (self.history
                                    and self.history[-1].get("role") == "user"):
                                self.history.pop()
                                rollback_count += 1

                            if self.history:
                                last = self.history[-1]
                                if last.get("role") == "assistant":
                                    content = last.get("content", [])
                                    has_orphan_tool_use = (
                                        isinstance(content, list)
                                        and any(
                                            isinstance(b, dict)
                                            and b.get("type") == "tool_use"
                                            for b in content
                                        )
                                    )
                                    if has_orphan_tool_use:
                                        self.history.pop()
                                        rollback_count += 1

                        logger.info(
                            f"Rolled back {rollback_count} messages after stream "
                            f"failure ({len(self.history)} remain, "
                            f"consecutive={self._consecutive_stream_errors})"
                        )

                        # Restore token counters to pre-attempt snapshot
                        self._total_input_tokens = snapshot_input
                        self._total_output_tokens = snapshot_output
                        self._cache_read_tokens = snapshot_cache_read
                        self._cache_write_tokens = snapshot_cache_write

                        # Single event with full error — no double display
                        self._record_failure_pattern(
                            "stream_failure",
                            err_msg[:1200],
                            {"attempt": attempt, "max_retries": max_retries},
                        )
                        if any(phrase in err_str for phrase in ("token", "max_tokens", "length limit", "context")):
                            user_msg = (
                                "Response hit a length limit. Conversation was compacted. "
                                "Re-send your message or break the task into smaller steps."
                            )
                            try:
                                await self._trim_history()
                            except Exception:
                                pass
                        else:
                            user_msg = f"Streaming error: {err_msg}\n\nYour message was rolled back — you can re-send it."
                        await on_event(AgentEvent(type="stream_failed", content=user_msg))
                        stream_succeeded = False
                        break

                    # Retryable — wait and try again
                    wait_secs = retry_backoff * (2 ** (attempt - 1))  # exponential: 2s, 4s, 8s …
                    logger.warning(
                        f"Stream error (attempt {attempt}/{max_retries}), "
                        f"retrying in {wait_secs:.1f}s: {stream_err}"
                    )

                    # Notify UI about the retry — this clears partial output
                    await on_event(AgentEvent(
                        type="stream_recovering",
                        content=f"Connection lost — retrying in {wait_secs:.0f}s...",
                        data={"attempt": attempt, "wait_seconds": wait_secs},
                    ))

                    await asyncio.sleep(wait_secs)
                    continue  # next attempt

            if not stream_succeeded:
                break  # exit the outer agent loop

            if self._cancelled:
                await on_event(AgentEvent(type="cancelled"))
                break

            # Guidance interrupt — discard partial response, loop back so _consume_guidance picks it up
            if _guidance_interrupted:
                continue

            # Add assistant message to history (includes thinking blocks for continuity)
            if assistant_content:
                self.history.append({"role": "assistant", "content": assistant_content})

            # Check for tool calls
            tool_uses = [b for b in assistant_content if b.get("type") == "tool_use"]

            # Response was cut off by max_tokens — continue next iteration (user never sees "ran out of tokens")
            if not tool_uses and last_stop_reason in ("max_tokens", "length"):
                self.history.append({
                    "role": "user",
                    "content": (
                        "[SYSTEM] Your previous response was cut off due to length. "
                        "Continue from where you left off. If you were mid tool call, complete it. "
                        "If you were explaining, briefly summarize progress and continue the task."
                    ),
                })
                await on_event(AgentEvent(
                    type="stream_recovering",
                    content="Continuing automatically...",
                ))
                should_auto_continue = True

            if should_auto_continue:
                continue  # next while iteration: stream again with continuation user message

            if not tool_uses:
                assistant_text = self._extract_assistant_text(assistant_content)
                
                # Before declaring the task "done", check if the assistant is actually
                # signaling completion vs just giving a conversational response
                if not self._assistant_signals_completion(assistant_text):
                    # Assistant gave a conversational response without tools but didn't
                    # explicitly signal task completion. This suggests the user may be
                    # asking about something new or the assistant misunderstood.
                    # Give the user a chance to respond instead of auto-completing.
                    break  # Exit the agent loop and wait for user input

                # Hard gate: if we just processed tool results, require structured
                # user-visible reasoning trace before final completion.
                if (
                    app_config.enforce_reasoning_trace
                    and self._last_user_message_has_tool_results()
                    and not self._has_structured_reasoning_trace(assistant_text)
                ):
                    if reasoning_trace_repairs < 2:
                        reasoning_trace_repairs += 1
                        self.history.append({
                            "role": "user",
                            "content": (
                                "[SYSTEM] Before finishing, provide a structured reasoning trace using these exact headings:\n"
                                "- What I learned\n"
                                "- Why it matters\n"
                                "- Decision\n"
                                "- Next actions\n"
                                "- Verification status\n\n"
                                "Then conclude."
                            ),
                        })
                        await on_event(AgentEvent(
                            type="stream_recovering",
                            content="Requesting structured reasoning trace before completion...",
                        ))
                        continue

                # Deterministic verification gate before done (max 2 attempts)
                if not hasattr(self, '_verification_gate_attempts'):
                    self._verification_gate_attempts = 0
                if (
                    app_config.deterministic_verification_gate
                    and self._file_snapshots
                    and not self._deterministic_verification_done
                    and self._verification_gate_attempts < 2
                ):
                    # Only verify files that still exist on disk
                    existing_snapshots = {k: v for k, v in self._file_snapshots.items() if os.path.isfile(k)}
                    if not existing_snapshots:
                        # All tracked files were deleted — nothing to verify
                        self._deterministic_verification_done = True
                    else:
                        gate_ok, gate_summary = await self._run_deterministic_verification_gate(on_event)
                        self._verification_gate_attempts += 1
                        if not gate_ok and self._verification_gate_attempts < 2:
                            self.history.append({
                                "role": "user",
                                "content": (
                                    "[SYSTEM] Verification found issues. Try to fix them, but if the issues "
                                    "are pre-existing or unrelated to your changes, just confirm the task is "
                                    "complete and move on. Do NOT loop — one fix attempt only.\n\n"
                                    + gate_summary
                                ),
                            })
                            await on_event(AgentEvent(
                                type="stream_recovering",
                                content="Verification found issues — one fix attempt...",
                            ))
                            continue
                        # Either passed or exhausted attempts — proceed
                        self._deterministic_verification_done = True
                        self.history.append({
                            "role": "user",
                            "content": (
                                "[SYSTEM] Verification complete:\n\n"
                                + gate_summary
                                + "\n\nProvide final completion update and finish."
                            ),
                        })
                        continue

                # No tool calls — agent is done
                ctx_est = self._current_token_estimate()
                ctx_window = get_context_window(self.service.model_id)
                await on_event(AgentEvent(
                    type="done",
                    data={
                        "input_tokens": self._total_input_tokens,
                        "output_tokens": self._total_output_tokens,
                        "cache_read_tokens": self._cache_read_tokens,
                        "context_usage_pct": round(ctx_est / ctx_window * 100) if ctx_window else 0,
                    },
                ))
                break

            # Execute tools — parallel when possible
            tool_results = await self._execute_tools_parallel(
                tool_uses, on_event, request_approval, request_question_answer=request_question_answer
            )

            # Cap tool results before they enter history (prevention > cure)
            capped_results = self._cap_tool_results(tool_results)

            # Post-edit verification nudge: if any write tools were used,
            # append a system hint reminding the model to verify its changes.
            write_tool_uses = [tu for tu in tool_uses if _is_file_write_tool(tu)]
            if write_tool_uses:
                modified_files = [p for p in (_get_tool_path(tu) for tu in write_tool_uses) if p != "?"]
                if not modified_files:
                    modified_files = ["(unknown path)"]
                files_str = ", ".join(modified_files)
                verify_hint = {
                    "type": "text",
                    "text": (
                        f"[System] You just modified: {files_str}. "
                        "Verify your changes: re-read the modified sections to confirm edits applied correctly. "
                        "Run lint_file on each changed file to catch any syntax errors or issues. "
                        "Fix any problems before proceeding to the next step."
                    ),
                }
                capped_results.append(verify_hint)

            # Strategy escalation: detect repeated failures and suggest alternative approaches
            escalation = self._suggest_strategy_escalation(capped_results)
            if escalation:
                capped_results.append({
                    "type": "text",
                    "text": f"[STRATEGY HINT]\n{escalation}",
                })
                await on_event(AgentEvent(
                    type="strategy_escalation",
                    content=escalation,
                ))

            # Inject any pending user guidance alongside tool results
            mid_guidance = self._consume_guidance()
            if mid_guidance:
                capped_results.append({
                    "type": "text",
                    "text": (
                        f"[USER GUIDANCE — mid-task correction from the user. "
                        f"Incorporate this into your current work immediately.]\n\n{mid_guidance}"
                    ),
                })
                await on_event(AgentEvent(type="guidance_applied", content=mid_guidance))

            self.history.append({"role": "user", "content": capped_results})

        if iteration >= self.max_iterations:
            await on_event(AgentEvent(
                type="error",
                content=f"Reached maximum iterations ({self.max_iterations}). Stopping.",
            ))


    async def _execute_tools_parallel(
        self,
        tool_uses: List[Dict[str, Any]],
        on_event: Callable[[AgentEvent], Awaitable[None]],
        request_approval: Callable[[str, str, Dict], Awaitable[bool]],
        request_question_answer: Optional[Callable[..., Awaitable[str]]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Execute a batch of tool calls, running safe (read-only) tools in parallel
        and dangerous (write) tools after collecting approvals.

        Returns a list of tool_result dicts ready for the conversation history.
        """
        loop = asyncio.get_event_loop()
        results_by_id: Dict[str, Dict[str, Any]] = {}

        original_tool_uses = tool_uses
        _special_tools = ("TodoWrite", "TodoRead", "MemoryWrite", "MemoryRead", "AskUserQuestion")
        todo_calls = [tu for tu in tool_uses if tu.get("name") == "TodoWrite"]
        todo_read_calls = [tu for tu in tool_uses if tu.get("name") == "TodoRead"]
        memory_calls = [tu for tu in tool_uses if tu.get("name") in ("MemoryWrite", "MemoryRead")]
        ask_calls = [tu for tu in tool_uses if tu.get("name") == "AskUserQuestion"]
        rest_calls = [tu for tu in tool_uses if tu.get("name") not in _special_tools]

        for tu in todo_calls:
            inp = tu.get("input") or {}
            raw = list(inp.get("todos") or [])
            # Normalize to { id, content, status } for our UI/persistence (SDK schema uses content, status; id optional)
            self._todos = [
                {"id": t.get("id") or str(i), "content": t.get("content", ""), "status": t.get("status", "pending")}
                for i, t in enumerate(raw, 1)
            ]
            lines = [f"Todos updated ({len(self._todos)} items)."]
            for t in self._todos:
                lines.append(f"  [{t.get('status', 'pending')}] {t.get('content', '')}")
            content = "\n".join(lines)
            results_by_id[tu["id"]] = {
                "type": "tool_result",
                "tool_use_id": tu["id"],
                "content": content,
                "is_error": False,
            }
            await on_event(AgentEvent(
                type="tool_result",
                content=content,
                data={"tool_name": "TodoWrite", "tool_use_id": tu["id"], "success": True, "todos": list(self._todos)},
            ))
            await on_event(AgentEvent(type="todos_updated", content="", data={"todos": list(self._todos)}))

        for tu in todo_read_calls:
            # Return current todos as JSON for the model (same shape as TodoWrite: id, content, status)
            todos_list = list(self._todos)
            if not todos_list:
                content = "No todos yet. Use TodoWrite to create a task list."
            else:
                content = json.dumps(todos_list, indent=2)
            results_by_id[tu["id"]] = {
                "type": "tool_result",
                "tool_use_id": tu["id"],
                "content": content,
                "is_error": False,
            }
            await on_event(AgentEvent(
                type="tool_result",
                content=content,
                data={"tool_name": "TodoRead", "tool_use_id": tu["id"], "success": True},
            ))

        _MEMORY_VALUE_CAP = 10_000  # chars per value to avoid abuse

        for tu in memory_calls:
            inp = tu.get("input") or {}
            name = tu.get("name", "")
            if name == "MemoryWrite":
                key = (inp.get("key") or "").strip()
                if not key:
                    content = "Error: key is required and cannot be empty."
                    is_err = True
                else:
                    value = inp.get("value", "")
                    if isinstance(value, str):
                        pass
                    else:
                        value = json.dumps(value) if value is not None else ""
                    value = (value or "")[: _MEMORY_VALUE_CAP]
                    self._memory[key] = value
                    content = f"Stored key '{key}'."
                    is_err = False
            else:
                # MemoryRead
                key = (inp.get("key") or "").strip()
                if key:
                    val = self._memory.get(key)
                    if val is None:
                        content = f"No value stored for key '{key}'."
                    else:
                        content = val
                    is_err = False
                else:
                    if not self._memory:
                        content = "No facts stored yet. Use MemoryWrite to store key-value facts."
                    else:
                        lines = [f"{k}: {v}" for k, v in sorted(self._memory.items())]
                        content = "\n".join(lines)
                    is_err = False
            results_by_id[tu["id"]] = {
                "type": "tool_result",
                "tool_use_id": tu["id"],
                "content": content,
                "is_error": is_err,
            }
            await on_event(AgentEvent(
                type="tool_result",
                content=content,
                data={"tool_name": name, "tool_use_id": tu["id"], "success": not is_err},
            ))

        for tu in ask_calls:
            inp = tu.get("input") or {}
            question = inp.get("question") or ""
            context = inp.get("context") or ""
            options = inp.get("options")
            if isinstance(options, list):
                options = [str(o) for o in options]
            else:
                options = None
            if request_question_answer:
                try:
                    answer = await request_question_answer(question, context, tu["id"], options=options)
                except Exception as e:
                    answer = f"Error asking user: {e}"
            else:
                answer = "No question callback; proceeding with best assumption."
            results_by_id[tu["id"]] = {
                "type": "tool_result",
                "tool_use_id": tu["id"],
                "content": answer,
                "is_error": False,
            }

        tool_uses = rest_calls

        async def _run_command_with_streaming(tool_id: str, tool_input: Dict[str, Any]) -> ToolResult:
            """Run command with live output events when enabled."""
            command = tool_input.get("command", "")
            timeout = int(tool_input.get("timeout", 30) or 30)

            if not app_config.live_command_streaming:
                return await loop.run_in_executor(
                    None, lambda: execute_tool(NATIVE_BASH_NAME, tool_input, self.working_directory, backend=self.backend, extra_context={"todos": self._todos})
                )

            partial_sent = {"value": False}

            def _on_output(chunk: str, is_stderr: bool) -> None:
                if not chunk:
                    return
                try:
                    asyncio.run_coroutine_threadsafe(
                        on_event(AgentEvent(
                            type="command_output",
                            content=chunk,
                            data={
                                "tool_use_id": tool_id,
                                "is_stderr": bool(is_stderr),
                            },
                        )),
                        loop,
                    )
                except Exception:
                    pass

                # Partial failure signal for quicker UX feedback
                if not partial_sent["value"] and re.search(r"(error|failed|traceback|exception)", chunk, flags=re.IGNORECASE):
                    partial_sent["value"] = True
                    try:
                        asyncio.run_coroutine_threadsafe(
                            on_event(AgentEvent(
                                type="command_partial_failure",
                                content="Potential failure detected in command output.",
                                data={"tool_use_id": tool_id},
                            )),
                            loop,
                        )
                    except Exception:
                        pass

            def _exec_stream() -> ToolResult:
                stdout, stderr, rc = self.backend.run_command_stream(
                    command,
                    cwd=".",
                    timeout=timeout,
                    on_output=_on_output,
                )
                parts = []
                if stdout:
                    parts.append(stdout)
                if stderr:
                    parts.append(f"[stderr]\n{stderr}")
                output = "\n".join(parts) if parts else "(no output)"
                if rc != 0:
                    output = f"[exit code: {rc}]\n{output}"

                if len(output) > 20000:
                    lines_out = output.split("\n")
                    if len(lines_out) > 200:
                        output = "\n".join(lines_out[:100]) + f"\n\n... [{len(lines_out) - 150} lines truncated] ...\n\n" + "\n".join(lines_out[-50:])
                    else:
                        output = output[:10000] + "\n\n... [truncated] ...\n\n" + output[-5000:]

                return ToolResult(
                    success=rc == 0,
                    output=output,
                    error=None if rc == 0 else f"Command exited with code {rc}",
                )

            return await loop.run_in_executor(None, _exec_stream)

        # Partition into safe and dangerous
        safe_calls = []
        dangerous_calls = []
        for tu in tool_uses:
            name = tu["name"]
            if name in SAFE_TOOLS:
                safe_calls.append(tu)
            else:
                dangerous_calls.append(tu)

        # ---- 1. Run all safe tools concurrently ----
        # NOTE: tool_call events are already emitted by the streaming loop
        # in _agent_loop, so we skip emitting them here to avoid duplicates.
        if safe_calls:
            # Dedup: if multiple reads target the same file (no offset), share the result
            _dedup_reads: Dict[str, asyncio.Future] = {}

            async def _run_safe(tu: Dict[str, Any]) -> tuple:
                name = tu["name"]
                inp = tu["input"]

                # File cache: return cached content for view commands (no range) if file hasn't been modified
                if _is_file_read_tool(tu) and not inp.get("view_range"):
                    path = inp.get("path", "")
                    cache_key = self._file_cache_key(path)

                    # Dedup within the same batch (by resolved path for backend consistency)
                    resolved = self.backend.resolve_path(path)
                    if resolved in _dedup_reads:
                        cached_result = await _dedup_reads[resolved]
                        return tu, cached_result

                    # Check file cache
                    if cache_key in self._file_cache:
                        cached_content, _ = self._file_cache[cache_key]
                        if resolved not in self._file_snapshots:
                            return tu, ToolResult(success=True, output=cached_content)

                result = await loop.run_in_executor(
                    None, lambda _tu=tu: execute_tool(_tu["name"], _tu["input"], self.working_directory, backend=self.backend, extra_context={"todos": self._todos})
                )

                # Cache successful full-file views
                if _is_file_read_tool(tu) and result.success and not inp.get("view_range"):
                    path = inp.get("path", "")
                    cache_key = self._file_cache_key(path)
                    self._file_cache[cache_key] = (result.output, time.time())

                return tu, result

            safe_results = await asyncio.gather(*[_run_safe(tu) for tu in safe_calls])

            for tu, result in safe_results:
                result_text = result.output if result.success else (result.error or "Unknown error")
                await on_event(AgentEvent(
                    type="tool_result",
                    content=result_text,
                    data={
                        "tool_name": tu["name"],
                        "tool_use_id": tu["id"],
                        "success": result.success,
                    },
                ))
                results_by_id[tu["id"]] = {
                    "type": "tool_result",
                    "tool_use_id": tu["id"],
                    "content": result_text,
                    "is_error": not result.success,
                }
                if not result.success:
                    self._record_failure_pattern(
                        "safe_tool_failure",
                        result_text[:1000],
                        {"tool_name": tu["name"], "tool_input": tu.get("input", {})},
                    )

        # ---- 2. Handle dangerous tools ----
        #   File writes: generally revertible, but policy engine may force approval.
        #   Commands: require explicit approval unless configured otherwise.
        if dangerous_calls:
            file_write_calls = [
                tu for tu in dangerous_calls if _is_file_write_tool(tu)
            ]
            command_calls = [
                tu for tu in dangerous_calls if not _is_file_write_tool(tu)
            ]

            # Policy engine + explicit approvals for risky file writes
            filtered_file_writes: List[Dict[str, Any]] = []
            for tu in file_write_calls:
                decision = self._policy_decision(tu["name"], tu["input"])
                if decision.blocked:
                    msg = f"Blocked by policy engine: {decision.reason or 'Operation is not allowed.'}"
                    results_by_id[tu["id"]] = {
                        "type": "tool_result",
                        "tool_use_id": tu["id"],
                        "content": msg,
                        "is_error": True,
                    }
                    await on_event(AgentEvent(
                        type="tool_rejected",
                        content=tu["name"],
                        data={"tool_use_id": tu["id"], "reason": decision.reason, "policy_blocked": True},
                    ))
                    self._record_failure_pattern("policy_block", msg, {"tool_name": tu["name"], "input": tu["input"]})
                    continue

                if decision.require_approval:
                    if self.was_previously_approved(tu["name"], tu["input"]):
                        await on_event(AgentEvent(
                            type="auto_approved",
                            content=tu["name"],
                            data={"tool_input": tu["input"], "policy_reason": decision.reason},
                        ))
                    else:
                        desc = self._format_tool_description(tu["name"], tu["input"])
                        if decision.reason:
                            desc += f"\n\nPolicy note: {decision.reason}"
                        approved = await request_approval(tu["name"], desc, tu["input"])
                        if not approved:
                            results_by_id[tu["id"]] = {
                                "type": "tool_result",
                                "tool_use_id": tu["id"],
                                "content": "User rejected this operation.",
                                "is_error": True,
                            }
                            await on_event(AgentEvent(
                                type="tool_rejected",
                                content=tu["name"],
                                data={"tool_use_id": tu["id"], "reason": decision.reason},
                            ))
                            continue
                        self.remember_approval(tu["name"], tu["input"])
                filtered_file_writes.append(tu)
            file_write_calls = filtered_file_writes

            # --- Phase A: file writes (auto-approved, revertible) ---
            if file_write_calls:
                # Snapshot all files BEFORE any writes
                for tu in file_write_calls:
                    self._snapshot_file(tu["name"], tu["input"])

                # Group by resolved path so same-file edits serialize (backend-agnostic)
                file_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
                for tu in file_write_calls:
                    path = tu["input"].get("path", "")
                    abs_path = self.backend.resolve_path(path)
                    file_groups[abs_path].append(tu)

                # Session checkpoint before risky file batch
                cp_id = self._create_session_checkpoint(
                    label=f"before_file_batch:{len(file_write_calls)}",
                    target_paths=list(file_groups.keys()),
                )
                if cp_id:
                    await on_event(AgentEvent(
                        type="checkpoint_created",
                        content="Checkpoint",
                        data={"checkpoint_id": cp_id, "label": "before_file_batch"},
                    ))

                async def _run_file_group(
                    calls: List[Dict[str, Any]],
                ) -> List[tuple]:
                    """Serial writes to one file; stop on first failure.
                    Includes auto-lint after edits and auto-retry on edit failure."""
                    results = []
                    for tu in calls:
                        result = await loop.run_in_executor(
                            None, lambda _tu=tu: execute_tool(_tu["name"], _tu["input"], self.working_directory, backend=self.backend, extra_context={"todos": self._todos})
                        )

                        # ── Auto-retry on edit failure ──
                        # If str_replace failed with "not found" or "multiple occurrences",
                        # re-read the file and include content in the error for immediate retry
                        _is_str_replace = (
                            tu["name"] == NATIVE_EDITOR_NAME
                            and tu["input"].get("command") == "str_replace"
                        )
                        if not result.success and _is_str_replace:
                            err = result.error or ""
                            if "not found" in err.lower() or "occurrences" in err.lower():
                                path = tu["input"].get("path", "")
                                try:
                                    fresh = await loop.run_in_executor(
                                        None, lambda: execute_tool(
                                            NATIVE_EDITOR_NAME, {"command": "view", "path": path},
                                            self.working_directory, backend=self.backend,
                                            extra_context={"todos": self._todos},
                                        )
                                    )
                                    if fresh.success:
                                        content = fresh.output
                                        ctx_factor = max(get_context_window(self.service.model_id) / 200_000, 1.0)
                                        auto_read_cap = int(8000 * ctx_factor)
                                        auto_read_lines = int(150 * ctx_factor)
                                        if len(content) > auto_read_cap:
                                            lines = content.split("\n")
                                            content = "\n".join(lines[:auto_read_lines]) + f"\n... ({len(lines) - auto_read_lines} lines omitted)"
                                        result = ToolResult(
                                            success=False,
                                            output="",
                                            error=(
                                                f"{err}\n\n"
                                                f"[Auto-read] Current file content:\n{content}\n\n"
                                                "Retry with the correct old_string from the content above."
                                            ),
                                        )
                                except Exception:
                                    pass  # fall through with original error

                        # ── Auto-lint after successful file write ──
                        if result.success and _is_file_write_tool(tu):
                            path = tu["input"].get("path", "")
                            try:
                                lint_result = await loop.run_in_executor(
                                    None, lambda: execute_tool(
                                        "lint_file", {"path": path},
                                        self.working_directory, backend=self.backend,
                                        extra_context={"todos": self._todos},
                                    )
                                )
                                if lint_result.success and lint_result.output:
                                    lint_out = lint_result.output.strip()
                                    # Only append if there are actual errors (not "no issues")
                                    if lint_out and "no issues" not in lint_out.lower() and "no errors" not in lint_out.lower() and "looks good" not in lint_out.lower():
                                        result = ToolResult(
                                            success=True,
                                            output=(
                                                f"{result.output}\n\n"
                                                f"[Auto-lint] Errors detected:\n{lint_out}\n"
                                                "Fix these lint errors."
                                            ),
                                        )
                            except Exception:
                                pass  # lint failure is non-fatal

                            # Invalidate file cache after successful write
                            self._file_cache.pop(self._file_cache_key(path), None)

                            # If this was a created file (snapshot was None), store content
                            # so Revert can bring the file back if the agent later deletes it
                            _is_create = (tu["name"] == NATIVE_EDITOR_NAME
                                          and tu["input"].get("command") == "create")
                            if _is_create and path:
                                abs_path = self.backend.resolve_path(path)
                                if self._file_snapshots.get(abs_path) is None:
                                    written = tu["input"].get("file_text", "") or tu["input"].get("content", "")
                                    if isinstance(written, str) and len(written) < 1_000_000:
                                        try:
                                            written.encode("utf-8")
                                            self._file_snapshots[abs_path] = {"created": True, "content": written}
                                        except (UnicodeDecodeError, UnicodeEncodeError):
                                            pass

                        results.append((tu, result))
                        if not result.success:
                            # Abort remaining edits to this file — they
                            # rely on content that didn't change as expected.
                            for remaining in calls[calls.index(tu) + 1:]:
                                results.append((remaining, ToolResult(
                                    success=False,
                                    output="",
                                    error="Skipped: earlier edit to same file failed.",
                                )))
                            break
                    return results

                # Different files in parallel; return_exceptions so one
                # group's failure doesn't swallow results from others.
                group_results = await asyncio.gather(
                    *[_run_file_group(g) for g in file_groups.values()],
                    return_exceptions=True,
                )

                for group in group_results:
                    if isinstance(group, BaseException):
                        logger.error(f"File group error: {group}")
                        continue
                    for tu, result in group:
                        result_text = result.output if result.success else (
                            result.error or "Unknown error"
                        )
                        await on_event(AgentEvent(
                            type="tool_result",
                            content=result_text,
                            data={
                                "tool_name": tu["name"],
                                "tool_use_id": tu["id"],
                                "success": result.success,
                            },
                        ))
                        results_by_id[tu["id"]] = {
                            "type": "tool_result",
                            "tool_use_id": tu["id"],
                            "content": result_text,
                            "is_error": not result.success,
                        }
                        if not result.success:
                            self._record_failure_pattern(
                                "file_edit_failure",
                                result_text[:1200],
                                {"tool_name": tu["name"], "tool_input": tu.get("input", {})},
                            )

            # --- Phase B: commands — require approval (irreversible) ---
            # In YOLO mode, auto-approve all commands
            for tu in command_calls:
                tool_name = tu["name"]
                tool_input = tu["input"]
                tool_id = tu["id"]
                decision = self._policy_decision(tool_name, tool_input)

                if decision.blocked:
                    blocked_msg = f"Blocked by policy engine: {decision.reason or 'Operation is not allowed.'}"
                    results_by_id[tool_id] = {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": blocked_msg,
                        "is_error": True,
                    }
                    await on_event(AgentEvent(
                        type="tool_rejected",
                        content=tool_name,
                        data={"tool_use_id": tool_id, "reason": decision.reason, "policy_blocked": True},
                    ))
                    self._record_failure_pattern("policy_block", blocked_msg, {"tool_name": tool_name, "tool_input": tool_input})
                    continue

                # Check YOLO mode, approval memory, or ask for approval
                if decision.require_approval:
                    if self.was_previously_approved(tool_name, tool_input):
                        await on_event(AgentEvent(
                            type="auto_approved",
                            content=tool_name,
                            data={"tool_input": tool_input, "policy_reason": decision.reason},
                        ))
                    else:
                        description = self._format_tool_description(tool_name, tool_input)
                        if decision.reason:
                            description += f"\n\nPolicy note: {decision.reason}"
                        approved = await request_approval(tool_name, description, tool_input)
                        if not approved:
                            results_by_id[tool_id] = {
                                "type": "tool_result",
                                "tool_use_id": tool_id,
                                "content": "User rejected this operation.",
                                "is_error": True,
                            }
                            await on_event(AgentEvent(
                                type="tool_rejected",
                                content=tool_name,
                                data={"tool_use_id": tool_id},
                            ))
                            continue
                        self.remember_approval(tool_name, tool_input)
                elif app_config.auto_approve_commands:
                    await on_event(AgentEvent(
                        type="auto_approved",
                        content=tool_name,
                        data={"tool_input": tool_input, "yolo": True},
                    ))
                elif self.was_previously_approved(tool_name, tool_input):
                    await on_event(AgentEvent(
                        type="auto_approved",
                        content=tool_name,
                        data={"tool_input": tool_input},
                    ))
                else:
                    description = self._format_tool_description(tool_name, tool_input)
                    approved = await request_approval(tool_name, description, tool_input)

                    if not approved:
                        results_by_id[tool_id] = {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": "User rejected this operation.",
                            "is_error": True,
                        }
                        await on_event(AgentEvent(
                            type="tool_rejected",
                            content=tool_name,
                            data={"tool_use_id": tool_id},
                        ))
                        continue

                    self.remember_approval(tool_name, tool_input)

                # Emit command_start for bash so the UI shows "running"
                if _is_bash_tool(tool_name):
                    await on_event(AgentEvent(
                        type="command_start",
                        content=tool_input.get("command", "?"),
                        data={"tool_use_id": tool_id},
                    ))

                # Session checkpoint before risky command batches
                cp_id = self._create_session_checkpoint(
                    label=f"before_command:{tool_name}",
                    target_paths=list(self._file_snapshots.keys()),
                )
                if cp_id:
                    await on_event(AgentEvent(
                        type="checkpoint_created",
                        content="Checkpoint",
                        data={"checkpoint_id": cp_id, "label": f"before_command:{tool_name}"},
                    ))

                cmd_start = time.time()
                if _is_bash_tool(tool_name):
                    result = await _run_command_with_streaming(tool_id, tool_input)
                else:
                    result = await loop.run_in_executor(
                        None, lambda: execute_tool(tool_name, tool_input, self.working_directory, backend=self.backend, extra_context={"todos": self._todos})
                    )
                cmd_duration = round(time.time() - cmd_start, 1)

                result_text = result.output if result.success else (
                    result.error or "Unknown error"
                )
                if not result.success and self._session_checkpoints:
                    last_cp = self._session_checkpoints[-1].get("id", "latest")
                    result_text += f"\n\n[checkpoint] You can rewind with checkpoint id: {last_cp}"

                # Extract exit code from bash output
                exit_code = None
                if _is_bash_tool(tool_name):
                    ec_match = re.search(r"\[exit code: (\d+)\]", result_text)
                    if ec_match:
                        exit_code = int(ec_match.group(1))
                    elif result.success:
                        exit_code = 0

                event_data: Dict[str, Any] = {
                    "tool_name": tool_name,
                    "tool_use_id": tool_id,
                    "success": result.success,
                    "duration": cmd_duration,
                }
                if exit_code is not None:
                    event_data["exit_code"] = exit_code

                await on_event(AgentEvent(
                    type="tool_result",
                    content=result_text,
                    data=event_data,
                ))
                results_by_id[tool_id] = {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": result_text,
                    "is_error": not result.success,
                }
                if not result.success:
                    self._record_failure_pattern(
                        "command_failure",
                        result_text[:1200],
                        {"tool_name": tool_name, "tool_input": tool_input},
                    )

        return [results_by_id[tu["id"]] for tu in original_tool_uses if tu["id"] in results_by_id]


    def _format_tool_description(self, name: str, inputs: Dict) -> str:
        """Format a human-readable description of a tool call for approval"""
        if name == NATIVE_EDITOR_NAME:
            cmd = inputs.get("command", "")
            path = inputs.get("path", "?")
            if cmd == "create":
                content = inputs.get("file_text", "")
                line_count = content.count("\n") + 1
                return f"Create {path} ({line_count} lines)"
            elif cmd == "str_replace":
                return f"Edit {path}: replace string"
            elif cmd == "insert":
                return f"Insert into {path} at line {inputs.get('insert_line', '?')}"
            elif cmd == "view":
                return f"View {path}"
            return f"Editor: {cmd} {path}"
        elif name == "symbol_edit":
            return (
                f"Symbol edit {inputs.get('path', '?')}: "
                f"{inputs.get('symbol', '?')} ({inputs.get('kind', 'all')})"
            )
        elif name == NATIVE_BASH_NAME:
            return f"Run: {inputs.get('command', '?')}"
        elif name == "plan_review":
            step_count = len(inputs.get("plan_steps", []) or [])
            return f"Review and approve plan execution ({step_count} steps)"
        return f"{name}({json.dumps(inputs)[:200]})"

    def _find_tool_name_for_result(self, tool_use_id: str, before_idx: int) -> str:
        """Look backwards in history to find which tool produced a given result."""
        for i in range(before_idx, -1, -1):
            content = self.history[i].get("content", [])
            if isinstance(content, list):
                for block in content:
                    if (isinstance(block, dict)
                            and block.get("type") == "tool_use"
                            and block.get("id") == tool_use_id):
                        return block.get("name", "")
        return ""


    def _adaptive_result_cap(self) -> int:
        """Return the max chars per tool result based on how full the context is.
        Base caps scale proportionally with context window size so larger
        windows (e.g. 1M tokens) get much more generous limits."""
        context_window = get_context_window(self.service.model_id)
        current = self._current_token_estimate()
        usage = current / context_window if context_window > 0 else 0
        factor = max(context_window / 200_000, 1.0)
        def _s(base: int) -> int:
            return int(base * factor)

        if usage < 0.25:
            return _s(50000)
        elif usage < 0.40:
            return _s(30000)
        elif usage < 0.55:
            return _s(20000)
        elif usage < 0.70:
            return _s(14000)
        else:
            return _s(8000)


    def _cap_tool_results(self, tool_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Microcompaction: cap tool result content at ingestion so context stays manageable.
        Enterprise-grade: large outputs are head/tail + explicit instruction to use Read with offset/limit for full content."""
        cap = self._adaptive_result_cap()
        capped = []
        for result in tool_results:
            text = result.get("content", "")
            if isinstance(text, str) and len(text) > cap:
                lines = text.split("\n")
                if len(lines) > 50:
                    head_n = max(20, cap // 400)
                    tail_n = max(10, cap // 800)
                    head = "\n".join(lines[:head_n])
                    tail = "\n".join(lines[-tail_n:])
                    text = (
                        "[Large output — excerpt below. Use Read with offset/limit for full content.]\n\n"
                        + head
                        + f"\n\n... ({len(lines) - head_n - tail_n} lines omitted) ...\n\n"
                        + tail
                    )
                else:
                    text = text[:cap - 200] + "\n... (truncated; use Read with offset/limit for full content) ..."
                if len(text) > cap:
                    text = text[:cap] + "\n... (excerpt capped) ..."
                capped.append({**result, "content": text})
            else:
                capped.append(result)
        return capped

