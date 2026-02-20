"""
Context window management for the coding agent.
Handles token estimation, history trimming, tool result compression, and summarization.
"""

import asyncio
import json
import logging
import re
from typing import List, Dict, Any, Optional

from bedrock_service import GenerationConfig
from config import get_context_window
from tools.schemas import NATIVE_EDITOR_NAME, NATIVE_BASH_NAME

from .events import AgentEvent

logger = logging.getLogger(__name__)


class HistoryMixin:
    """Mixin providing context window management: token estimation, trimming,
    compression, and summarization.

    Expects the host class to provide:
    - self.service (BedrockService)
    - self.history (list)
    - self._running_summary (str)
    - self._total_input_tokens, _total_output_tokens (int)
    - self._file_snapshots (dict) via ContextMixin
    """

    # ------------------------------------------------------------------
    # Token estimation
    # ------------------------------------------------------------------

    def _ctx_scale(self, base: int) -> int:
        """Scale a hardcoded limit proportionally to the context window.
        Factor is context_window / 200_000 so all limits stay the same for
        standard 200K models and grow linearly for larger windows.
        Capped at 3x to prevent over-generous limits on very large contexts (1M+)."""
        factor = get_context_window(self.service.model_id) / 200_000
        factor = min(max(factor, 1.0), 3.0)
        return int(base * factor)

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

    def _current_token_estimate(self) -> int:
        """Like _total_history_tokens but includes system prompt overhead."""
        base = self._total_history_tokens()
        # Compute actual system prompt token estimate instead of a flat guess.
        # The effective system prompt can be 5K-20K tokens depending on rules,
        # todos, failure patterns, and reminders.
        try:
            sys_prompt = self._effective_system_prompt(self.system_prompt)
            base += self._estimate_tokens(sys_prompt)
        except Exception:
            base += 4000  # conservative fallback
        return base

    # ------------------------------------------------------------------
    # Tool result compression
    # ------------------------------------------------------------------

    def _extract_file_paths_from_history(self) -> set:
        """Find file paths referenced in the last few messages (working set)."""
        paths = set()
        for msg in self.history[-self._ctx_scale(8):]:
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
        so the model retains understanding of file contents for audit-style work.
        All line/char thresholds scale with context window via _ctx_scale."""
        if len(text) < self._ctx_scale(500):
            return text

        lines = text.split("\n")
        s = self._ctx_scale

        is_file_view = tool_name in ("Read", NATIVE_EDITOR_NAME)
        if is_file_view:
            if is_hot:
                hot_limit = s(60)
                if len(lines) > hot_limit:
                    head_n = s(30)
                    tail_n = s(10)
                    return "\n".join(
                        lines[:head_n]
                        + [f"  ... ({len(lines) - head_n - tail_n} lines omitted, file in working set) ..."]
                        + lines[-tail_n:]
                    )
                return text
            else:
                cold_limit = s(40)
                if len(lines) > cold_limit:
                    structure = []
                    for i, line in enumerate(lines):
                        stripped = line.lstrip("0123456789| ").strip()
                        if any(stripped.startswith(kw) for kw in (
                            "import ", "from ", "class ", "def ", "async def ",
                            "function ", "export ", "const ", "let ", "var ",
                            "pub fn ", "fn ", "struct ", "enum ", "impl ",
                            "type ", "interface ",
                        )):
                            structure.append(lines[i])
                    if structure:
                        head_n = s(20)
                        struct_n = s(50)
                        return (
                            "\n".join(lines[:head_n])
                            + f"\n  ... ({len(lines) - head_n} lines total, showing structure) ...\n"
                            + "\n".join(structure[:struct_n])
                            + "\n  ... (end of structure) ..."
                        )
                    head_n = s(20)
                    tail_n = s(8)
                    return "\n".join(
                        lines[:head_n]
                        + [f"  ... ({len(lines) - head_n - tail_n} lines omitted) ..."]
                        + lines[-tail_n:]
                    )
                return text

        if tool_name == "search":
            search_limit = s(20)
            if len(lines) > search_limit:
                keep = s(15)
                return "\n".join(lines[:keep] + [f"  ... ({len(lines) - keep} more matches) ..."])

        if tool_name in ("Bash", NATIVE_BASH_NAME):
            bash_limit = s(30)
            if len(lines) > bash_limit:
                head_n = s(12)
                tail_n = s(5)
                return "\n".join(
                    lines[:head_n]
                    + [f"  ... ({len(lines) - head_n - tail_n} lines omitted) ..."]
                    + lines[-tail_n:]
                )

        if tool_name in ("list_directory", "Glob"):
            dir_limit = s(40)
            if len(lines) > dir_limit:
                keep = s(30)
                return "\n".join(lines[:keep] + [f"  ... ({len(lines) - keep} more entries) ..."])

        generic_limit = s(1000)
        if len(text) > generic_limit:
            keep_chars = s(600)
            return text[:keep_chars] + f"\n... ({len(text) - keep_chars} chars omitted) ..."

        return text

    # ------------------------------------------------------------------
    # Conversational context preservation
    # ------------------------------------------------------------------

    def _preserve_conversational_context(self, messages: List[Dict[str, Any]]) -> str:
        """Extract recent conversational context that should be preserved during trimming."""
        context_items = []
        pronoun_indicators = ['it', 'that', 'this', 'them', 'those', 'he', 'she', 'they']
        command_indicators = ['run', 'execute', 'try', 'test', 'check', 'start', 'stop']

        conv_horizon = self._ctx_scale(6)
        recent_messages = messages[-conv_horizon:] if len(messages) > conv_horizon else messages

        for i, msg in enumerate(recent_messages):
            content_str = self._extract_text_from_message(msg).strip()
            if not content_str:
                continue

            content_lower = content_str.lower()
            has_pronouns = any(word in content_lower for word in pronoun_indicators)
            has_commands = any(word in content_lower for word in command_indicators)

            if has_pronouns or has_commands:
                role = msg.get('role', 'unknown')
                snippet = content_str[:150]
                context_items.append(f"Recent {role}: {snippet}")

        if context_items:
            keep_items = self._ctx_scale(3)
            return "CONVERSATIONAL CONTEXT:\n" + "\n".join(context_items[-keep_items:])
        return ""

    def _detect_context_loss_risk(self, user_msg: str) -> bool:
        """Detect when a user message might reference lost conversational context."""
        if not user_msg or len(user_msg) > 500:
            return False
        # Only flag context loss if summarization has actually occurred
        if not self._running_summary:
            return False

        pronouns = ['it', 'that', 'this', 'them', 'those']

        sentences = re.split(r'[.!?]+', user_msg)
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            sentence_lower = sentence.lower()
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
        """Detect if the assistant is explicitly signaling task completion."""
        if not assistant_text or len(assistant_text.strip()) < 10:
            return False

        text_lower = assistant_text.lower().strip()

        completion_phrases = [
            "task is complete", "task complete", "completed successfully",
            "all done", "finished", "implementation is complete",
            "ready to go", "should be working now", "fixed the issue",
            "problem is resolved", "issue is resolved", "resolved the problem",
            "changes have been applied", "successfully implemented",
            "task has been completed", "work is done"
        ]

        if any(phrase in text_lower for phrase in completion_phrases):
            return True

        followup_phrases = [
            "let me know if you need", "let me know if there's",
            "feel free to", "if you need any", "anything else",
            "further assistance", "additional help"
        ]

        if any(phrase in text_lower for phrase in followup_phrases):
            return True

        return False

    # ------------------------------------------------------------------
    # Summarization
    # ------------------------------------------------------------------

    async def _summarize_old_messages(self, messages: List[Dict[str, Any]]) -> str:
        """Create a concise summary of old conversation messages.
        Tries an LLM call (Haiku) for quality; falls back to heuristics.
        The LLM call is offloaded to a thread to avoid blocking the event loop."""
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

            context_preservation = self._preserve_conversational_context(messages)
            if context_preservation:
                conversation_text = context_preservation + "\n\n" + conversation_text

            if len(conversation_text) > 30000:
                conversation_text = conversation_text[:15000] + "\n...\n" + conversation_text[-15000:]

            summary_config = GenerationConfig(
                max_tokens=2000,
                enable_thinking=False,
                thinking_budget=0,
                throughput_mode="cross-region",
            )

            def _llm_summarize():
                return self.service.generate_response(
                    messages=[{
                        "role": "user",
                        "content": (
                            "Summarize this coding conversation concisely. "
                            "Preserve: (1) files modified and how, (2) the task goal, "
                            "(3) key decisions, (4) commands that were run and their results, "
                            "(5) any unresolved issues or next steps.\n\n"
                            f"Conversation:\n{conversation_text}"
                        ),
                    }],
                    system_prompt=(
                        "You are a conversation summarizer for a coding assistant. "
                        "Produce a clear, structured summary preserving all technical details. "
                        "Keep the summary under 600 words."
                    ),
                    model_id="us.anthropic.claude-3-5-haiku-20241022-v1:0",
                    config=summary_config,
                )

            result = await asyncio.to_thread(_llm_summarize)
            if result.content and result.content.strip():
                logger.info(f"LLM summarized {len(messages)} messages into {len(result.content)} chars")
                return f"[Running summary of earlier conversation]\n{result.content.strip()}"
        except Exception as e:
            logger.debug(f"LLM summary failed ({e}), using heuristic")

        return self._summarize_old_messages_heuristic(messages)

    def _summarize_old_messages_heuristic(self, messages: List[Dict[str, Any]]) -> str:
        """Fallback: heuristic summary when LLM is unavailable."""
        summary_parts: list = []
        tool_calls_summary: dict = {}

        for msg in messages:
            role = msg.get("role", "?")
            content = msg.get("content", "")

            if isinstance(content, str):
                if role == "user" and len(content) > 20:
                    summary_parts.append(f"User asked: {content[:200]}")
                elif role == "assistant" and len(content) > 20:
                    summary_parts.append(f"Assistant replied: {content[:200]}")
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "tool_use":
                            name = block.get("name", "?")
                            tool_calls_summary[name] = tool_calls_summary.get(name, 0) + 1
                        elif block.get("type") == "text" and role == "assistant":
                            text = block.get("text", "")
                            if len(text) > 20:
                                summary_parts.append(f"Assistant: {text[:200]}")

        result_parts = [f"[Summary of {len(messages)} earlier messages]"]
        if tool_calls_summary:
            tools_str = ", ".join(f"{n}×{c}" for n, c in sorted(tool_calls_summary.items(), key=lambda x: -x[1]))
            result_parts.append(f"Tools used: {tools_str}")
        result_parts.extend(summary_parts[-6:])

        return "\n".join(result_parts)

    # ------------------------------------------------------------------
    # History trimming
    # ------------------------------------------------------------------

    async def _trim_history(self) -> None:
        """Multi-tier context management inspired by Cursor's approach.

        Tier 0: Compress large tool results inline
        Tier 1: Drop thinking blocks from old messages
        Tier 2: Summarize oldest messages into running summary
        Tier 3: Emergency — aggressive trimming
        """
        context_window = get_context_window(self.service.model_id)

        tier1_limit = int(context_window * 0.55)
        tier2_limit = int(context_window * 0.65)
        tier3_limit = int(context_window * 0.80)

        current = self._total_history_tokens()

        if current <= tier1_limit:
            return

        # ── Tier 0: Compress large tool results inline ────────────
        hot_paths = self._extract_file_paths_from_history()
        for msg in self.history[:-2]:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for j, block in enumerate(content):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    text = block.get("content", "")
                    if isinstance(text, str) and len(text) > self._ctx_scale(400):
                        tool_name = self._find_tool_name_for_result(
                            block.get("tool_use_id", ""), self.history.index(msg)
                        )
                        is_hot = any(p in text for p in hot_paths)
                        compressed = self._compress_tool_result(text, tool_name, is_hot)
                        if len(compressed) < len(text):
                            content[j] = {**block, "content": compressed}
        current = self._total_history_tokens()

        if current <= tier1_limit:
            return

        # ── Tier 1: Drop thinking blocks from older messages ──────
        logger.info(f"Context tier 1: dropping thinking from old messages (~{current:,} tokens)")
        thinking_horizon = self._ctx_scale(4)
        for msg in self.history[:-thinking_horizon]:
            content = msg.get("content")
            if isinstance(content, list):
                new_content = [
                    b for b in content
                    if not (isinstance(b, dict) and b.get("type") == "thinking")
                ]
                if len(new_content) < len(content):
                    msg["content"] = new_content
        # Deduplicate system-injected messages (verification hints, strategy
        # escalations) that accumulate across iterations in older history.
        seen_system_texts: set = set()
        for msg in self.history[:-thinking_horizon]:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            deduped = []
            for b in content:
                if (isinstance(b, dict) and b.get("type") == "text"
                        and isinstance(b.get("text", ""), str)
                        and b["text"].startswith("[System]")):
                    if b["text"] in seen_system_texts:
                        continue
                    seen_system_texts.add(b["text"])
                deduped.append(b)
            if len(deduped) < len(content):
                msg["content"] = deduped

        current = self._total_history_tokens()

        if current <= tier2_limit:
            return

        # ── Tier 2: Summarize oldest messages into running summary ──
        logger.info(f"Context tier 2: summarizing (~{current:,} tokens > {tier2_limit:,})")

        summarize_horizon = self._ctx_scale(6)
        for msg in self.history[:-summarize_horizon]:
            content = msg.get("content")
            if isinstance(content, list):
                msg["content"] = [
                    b for b in content
                    if not (isinstance(b, dict) and b.get("type") == "thinking")
                ]

        ratio = current / tier2_limit
        if ratio > 3:
            keep_last = min(self._ctx_scale(10), len(self.history))
        elif ratio > 1.5:
            keep_last = min(self._ctx_scale(14), len(self.history))
        else:
            keep_last = min(self._ctx_scale(18), len(self.history))

        keep_first = 1
        if len(self.history) > keep_first + keep_last:
            old_messages = self.history[keep_first:-keep_last]
            summary = await self._summarize_old_messages(old_messages)

            if self._running_summary:
                summary = self._running_summary + "\n\n" + summary
            # Cap running summary to prevent unbounded growth over many tier 2 passes
            max_summary_len = self._ctx_scale(3000)
            if len(summary) > max_summary_len:
                summary = summary[-max_summary_len:]
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

        for msg in self.history[:-1]:
            content = msg.get("content")
            if isinstance(content, list):
                msg["content"] = [
                    b for b in content
                    if not (isinstance(b, dict) and b.get("type") == "thinking")
                ]
        current = self._total_history_tokens()

        trim_threshold = self._ctx_scale(100)
        trim_keep = self._ctx_scale(80)
        str_threshold = self._ctx_scale(500)
        str_keep = self._ctx_scale(200)

        if current > tier3_limit:
            for msg in self.history[:-1]:
                content = msg.get("content")
                if isinstance(content, list):
                    for j, block in enumerate(content):
                        if isinstance(block, dict):
                            for key in ("content", "text"):
                                val = block.get(key, "")
                                if isinstance(val, str) and len(val) > trim_threshold:
                                    content[j] = {**block, key: val[:trim_keep] + " (trimmed)"}
                elif isinstance(content, str) and len(content) > str_threshold:
                    msg["content"] = content[:str_keep] + " (trimmed)"
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
                    msg["content"] = [
                        b for b in content
                        if not (isinstance(b, dict) and b.get("type") == "thinking")
                    ]
                    for j, block in enumerate(msg["content"]):
                        if isinstance(block, dict):
                            for key in ("content", "text"):
                                val = block.get(key, "")
                                if isinstance(val, str) and len(val) > trim_threshold:
                                    msg["content"][j] = {**block, key: val[:trim_keep] + " (trimmed)"}
            current = self._total_history_tokens()

        logger.info(f"Context tier 3 done: ~{current:,} tokens, {len(self.history)} messages")

        # Validate structure after emergency trim: first message must be
        # role=user, and no two consecutive messages may share the same role
        # (unless the second is a tool_result following an assistant tool_use).
        if self.history and self.history[0].get("role") != "user":
            self.history.insert(0, {
                "role": "user",
                "content": self._running_summary or "(session context)",
            })
        self._repair_history()

    # ------------------------------------------------------------------
    # History repair
    # ------------------------------------------------------------------

    def _repair_history(self) -> None:
        """Validate and repair conversation history before each API call.

        Fixes orphaned tool_use blocks that don't have matching tool_result
        in the next message. This can happen after stream failures, context
        trimming, or session restore.
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

            tool_use_ids = set()
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tid = block.get("id", "")
                    if tid:
                        tool_use_ids.add(tid)

            if not tool_use_ids:
                i += 1
                continue

            next_idx = i + 1
            if next_idx >= len(self.history):
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
                continue

            next_msg = self.history[next_idx]
            next_content = next_msg.get("content", [])

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
                    for mid in missing:
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
                        f"for {len(missing)} orphaned tool_use blocks"
                    )
                    # Skip past the newly inserted message to avoid re-processing
                    i += 2
                    continue

            i += 1

        if repaired:
            logger.info(f"History repaired. {len(self.history)} messages.")

    def _find_tool_name_for_result(self, tool_use_id: str, before_idx: int) -> str:
        """Walk backwards to find the tool name for a given tool_use_id."""
        for idx in range(before_idx - 1, -1, -1):
            msg = self.history[idx]
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if (isinstance(block, dict)
                            and block.get("type") == "tool_use"
                            and block.get("id") == tool_use_id):
                        return block.get("name", "")
        return ""
