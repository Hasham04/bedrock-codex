"""
Bedrock Codex - A coding agent engine powered by Amazon Bedrock.
Terminal UI built with Textual + Rich.
"""

import asyncio
import argparse
import difflib
import json
import logging
import os
import re
import sys
import time
from typing import Optional, Dict, List

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Header, Footer, Input, Static, Collapsible
from textual.reactive import reactive
from textual.timer import Timer
from textual import on, work

from rich.text import Text
from rich.table import Table
from rich.markdown import Markdown
from rich.markup import escape as rich_escape

from bedrock_service import BedrockService, BedrockError
from agent import CodingAgent, AgentEvent, BUILD_SYSTEM_PROMPT, classify_intent
from sessions import SessionStore, Session
from config import (
    get_model_name, get_model_config, model_config, app_config,
    supports_thinking, supports_adaptive_thinking, supports_caching,
)

# Configure logging to file so it doesn't interfere with the TUI
logging.basicConfig(
    filename="bedrock_codex.log",
    level=getattr(logging, app_config.log_level),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# ============================================================
# Constants
# ============================================================

SPINNER_FRAMES = ["\u28f7", "\u28ef", "\u28df", "\u287f", "\u28bf", "\u28fb", "\u28fd", "\u28fe", "\u28f7", "\u28ef"]

TOOL_ICONS = {
    "read_file":      "\U0001f4c4 ",
    "write_file":     "\u270f\ufe0f ",
    "edit_file":      "\U0001f527 ",
    "run_command":    "\u25b6 ",
    "search":         "\U0001f50d ",
    "list_directory": "\U0001f4c2 ",
    "glob_find":      "\U0001f50e ",
}

TOOL_DANGER = {"write_file", "edit_file", "run_command"}

# Lines to show before collapsing
COLLAPSE_LINE_THRESHOLD = 8
COLLAPSE_CHAR_THRESHOLD = 400


# ============================================================
# TUI Application
# ============================================================

class BedrockCodexApp(App):
    """Bedrock Codex - Coding Agent TUI"""

    TITLE = "Bedrock Codex"
    ALLOW_SELECT = True  # Enable native text selection — click and drag to select, copies to clipboard

    CSS = """
    Screen {
        background: #0d1117;
    }

    #output-scroll {
        height: 1fr;
        border: none;
        padding: 1 2;
        scrollbar-size: 1 1;
        scrollbar-color: #30363d;
        scrollbar-color-hover: #484f58;
        scrollbar-color-active: #6e7681;
    }

    #output-scroll > Static {
        width: 100%;
        height: auto;
        margin: 0 0;
    }

    #output-scroll > Collapsible {
        width: 100%;
        height: auto;
        margin: 0 0 0 3;
    }

    #output-scroll > Collapsible > Contents {
        height: auto;
        padding: 0 1;
    }

    Collapsible.-collapsed > Contents {
        display: none;
    }

    CollapsibleTitle {
        color: #6e7681;
        background: transparent;
        padding: 0;
        height: 1;
    }

    CollapsibleTitle:hover {
        color: #c9d1d9;
        background: #161b22;
    }

    .thinking-live {
        color: #8b949e;
        margin: 0 0 0 3;
        padding: 0 1;
        border-left: tall #6e40c9;
        height: auto;
        max-height: 20;
        overflow-y: auto;
    }

    .text-stream {
        margin: 0 0 0 0;
        padding: 0 0;
        height: auto;
    }

    .user-msg {
        margin: 1 0 0 0;
        padding: 0 0;
        height: auto;
    }

    .agent-msg {
        margin: 0 0 0 0;
        padding: 0 0;
        height: auto;
    }

    .tool-line {
        margin: 0 0 0 3;
        height: auto;
    }

    .spacer {
        height: 1;
    }

    #user-input {
        dock: bottom;
        margin: 0 2 1 2;
        border: tall #30363d;
        background: #161b22;
        color: #c9d1d9;
        padding: 0 1;
    }

    #user-input:focus {
        border: tall #58a6ff;
    }

    #status-bar {
        dock: bottom;
        height: 1;
        background: #161b22;
        color: #6e7681;
        padding: 0 2;
    }

    Header {
        background: #010409;
        color: #f0f6fc;
        dock: top;
        height: 1;
    }

    Footer {
        background: #161b22;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "cancel_or_quit", "Cancel / Quit", priority=True),
        Binding("ctrl+l", "clear_screen", "Clear"),
        Binding("ctrl+r", "reset_conversation", "Reset"),
        Binding("ctrl+y", "copy_last", "Copy last response", show=False),
    ]

    is_running = reactive(False)
    awaiting_approval = reactive(False)

    def __init__(self, working_directory: str = ".", **kwargs):
        super().__init__(**kwargs)
        self.working_directory = os.path.abspath(working_directory)
        self._approval_future: Optional[asyncio.Future] = None
        self._thinking_content = ""
        self._thinking_widget: Optional[Static] = None
        self._current_text = ""
        self._text_widget: Optional[Static] = None  # live-streaming text widget
        self._bedrock_service: Optional[BedrockService] = None
        self._agent: Optional[CodingAgent] = None
        self._spinner_idx = 0
        self._spinner_timer: Optional[Timer] = None
        self._task_start_time: Optional[float] = None
        self._iteration_count = 0
        self._widget_counter = 0
        # Session persistence
        self._store = SessionStore()
        self._session: Optional[Session] = None
        # Plan-then-build state
        self._pending_task: Optional[str] = None
        self._pending_plan: Optional[list] = None
        self.awaiting_build = False
        self.awaiting_keep_revert = False
        self._plan_step_widgets: Dict[int, Static] = {}
        self._plan_steps_done: set = set()
        self._last_tool_call: Optional[dict] = None  # for matching tool_result to plan steps
        self._last_response: str = ""  # last assistant text response for /copy
        self._last_error: str = ""     # last error message (separate so it doesn't overwrite response)
        self._output_history: list = []  # all outputs: [(label, text), ...] for /copy N
        self._scout_widget: Optional[Static] = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="status-bar")
        yield VerticalScroll(id="output-scroll")
        yield Input(
            placeholder=" \u276f What would you like me to do?  (/help for commands)",
            id="user-input",
        )
        yield Footer()

    # ============================================================
    # Output helpers -- write to the scroll area
    # ============================================================

    def _next_id(self, prefix: str = "out") -> str:
        self._widget_counter += 1
        return f"{prefix}-{self._widget_counter}"

    def _log(self, renderable) -> None:
        """Append a renderable to the output scroll area."""
        scroll = self.query_one("#output-scroll", VerticalScroll)
        widget = Static(renderable, id=self._next_id())
        scroll.mount(widget)
        scroll.scroll_end(animate=False)

    def _log_collapsible(
        self,
        title: str,
        full_content: str,
        preview: str = "",
        collapsed: bool = True,
        style: str = "#586e75",
    ) -> None:
        """Append a collapsible section to the output. Click the title to expand."""
        scroll = self.query_one("#output-scroll", VerticalScroll)
        body = Static(Text(full_content, style=style), id=self._next_id("body"))
        coll = Collapsible(
            body,
            title=title,
            collapsed=collapsed,
            id=self._next_id("coll"),
        )
        scroll.mount(coll)
        scroll.scroll_end(animate=False)

    def _mark_plan_step(self, tool_name: str, tool_input: dict, success: bool) -> None:
        """Try to match a completed tool call to a plan step and update its widget."""
        if not hasattr(self, "_plan_step_widgets") or not self._plan_step_widgets:
            return
        if not success:
            return
        if not hasattr(self, "_plan_steps_done"):
            self._plan_steps_done: set = set()

        # Extract the relevant file/pattern from the tool call
        targets = []
        if "path" in tool_input:
            targets.append(tool_input["path"])
        if "command" in tool_input:
            targets.append(tool_input["command"])
        if "pattern" in tool_input:
            targets.append(tool_input["pattern"])

        if not targets:
            return

        for idx, widget in self._plan_step_widgets.items():
            if idx in self._plan_steps_done:
                continue
            try:
                step_text = str(widget.renderable).lower()
                for target in targets:
                    # Match if any part of the target appears in the step text
                    t = target.lower()
                    # Also match just the filename
                    basename = os.path.basename(t)
                    if t in step_text or basename in step_text:
                        self._plan_steps_done.add(idx)
                        # Update the widget: ○ -> ✓
                        clean = re.sub(r"^\d+[\.\)]\s*", "", step_text)
                        clean = re.sub(r"^[-*]\s*", "", clean)
                        # Get original text from the renderable
                        orig_text = str(widget.renderable)
                        new_text = orig_text.replace("\u25cb", "\u2713")
                        widget.update(
                            Text.from_markup(
                                new_text.replace("#6e7681", "#3fb950").replace("#c9d1d9", "#3fb950")
                            )
                        )
                        break
            except Exception:
                pass

    def _clear_output(self) -> None:
        """Remove all children from the output scroll area."""
        scroll = self.query_one("#output-scroll", VerticalScroll)
        scroll.remove_children()

    # ============================================================
    # Lifecycle
    # ============================================================

    def on_mount(self) -> None:
        """Initialize services on mount, auto-load last session"""
        self._init_services()
        self._load_or_create_session()
        self._update_status()
        self._show_welcome()
        self.query_one("#user-input", Input).focus()

    def _show_welcome(self):
        """Display a clean, minimal welcome screen"""
        model_name = get_model_name(model_config.model_id)
        model_cfg = get_model_config(model_config.model_id)
        thinking_mode = "adaptive" if supports_adaptive_thinking(model_config.model_id) else (
            "enabled" if model_config.enable_thinking and supports_thinking(model_config.model_id) else "disabled"
        )
        caching = "on" if supports_caching(model_config.model_id) else "off"

        session_name = self._session.name if self._session else "none"
        is_resumed = bool(self._session and self._session.history)
        msg_count = self._session.message_count if self._session else 0

        # Build a minimal welcome
        lines = []
        lines.append(Text.from_markup(
            "\n[bold #58a6ff]bedrock[/bold #58a6ff][bold #f0f6fc] codex[/bold #f0f6fc]"
        ))
        lines.append(Text(""))

        detail_parts = [
            f"[#8b949e]{model_name}[/#8b949e]",
            f"[#8b949e]thinking: {thinking_mode}[/#8b949e]",
            f"[#8b949e]cache: {caching}[/#8b949e]",
        ]
        lines.append(Text.from_markup("  ".join(detail_parts)))

        ctx = model_cfg.get("context_window", 0)
        out = model_cfg.get("max_output_tokens", 0)
        lines.append(Text.from_markup(
            f"[#6e7681]context: {ctx:,}  output: {out:,}  dir: {self.working_directory}[/#6e7681]"
        ))

        if is_resumed:
            lines.append(Text.from_markup(
                f"[#6e7681]session: {session_name} (resumed, {msg_count} messages)[/#6e7681]"
            ))
        else:
            lines.append(Text.from_markup(
                f"[#6e7681]session: {session_name}[/#6e7681]"
            ))

        lines.append(Text(""))
        lines.append(Text.from_markup(
            "[#484f58]Type a task to begin  \u00b7  /help for commands  \u00b7  Ctrl+C to cancel[/#484f58]"
        ))
        lines.append(Text(""))

        for line in lines:
            self._log(line)

    def _init_services(self):
        """Initialize Bedrock service and agent"""
        try:
            self._bedrock_service = BedrockService()
            self._agent = CodingAgent(
                bedrock_service=self._bedrock_service,
                working_directory=self.working_directory,
                max_iterations=int(os.getenv("MAX_TOOL_ITERATIONS", "50")),
            )
        except BedrockError as e:
            self._log(Text.from_markup(
                f"\n   [bold #f85149]\u2717 Failed to initialize: {rich_escape(str(e))}[/bold #f85149]"
            ))

    # ============================================================
    # Session Persistence
    # ============================================================

    def _load_or_create_session(self):
        existing = self._store.get_latest(self.working_directory)
        if existing and existing.history:
            self._session = existing
            if self._agent:
                self._agent.from_dict({
                    "history": existing.history,
                    "token_usage": existing.token_usage,
                })
            logger.info(f"Resumed session: {existing.name} ({existing.session_id})")
        else:
            self._session = self._store.create_session(
                working_directory=self.working_directory,
                model_id=model_config.model_id,
            )
            logger.info("Created new session")

    def _save_session(self):
        if not self._session or not self._agent:
            return
        agent_data = self._agent.to_dict()
        self._session.history = agent_data["history"]
        self._session.token_usage = agent_data["token_usage"]
        self._session.model_id = model_config.model_id
        self._session.working_directory = os.path.abspath(self.working_directory)
        try:
            self._store.save(self._session)
        except Exception as e:
            logger.error(f"Failed to save session: {e}")

    def _switch_to_session(self, session: Session):
        self._session = session
        if self._agent:
            self._agent.from_dict({
                "history": session.history,
                "token_usage": session.token_usage,
            })

    # ============================================================
    # Status Bar
    # ============================================================

    def _update_status(self):
        status = self.query_one("#status-bar", Static)
        model_name = get_model_name(model_config.model_id)
        tokens = f"{self._agent.total_tokens:,}" if self._agent else "0"

        parts = [model_name, f"tokens: {tokens}"]

        if self._agent and (self._agent._cache_read_tokens > 0 or self._agent._cache_write_tokens > 0):
            cr = self._agent._cache_read_tokens
            cw = self._agent._cache_write_tokens
            parts.append(f"cache \u2191{cr:,} \u2193{cw:,}")

        if self._session:
            sn = self._session.name
            if len(sn) > 20:
                sn = sn[:18] + "\u2026"
            parts.append(sn)

        if self.awaiting_keep_revert:
            file_count = len(self._agent.modified_files) if self._agent else 0
            parts.append(f"\u2194 keep/revert ({file_count} files)")
        elif self.awaiting_build:
            done = len(getattr(self, "_plan_steps_done", set()))
            total = len(getattr(self, "_plan_step_widgets", {}))
            parts.append(f"\u25b6 build? ({total} steps)")
        elif self.is_running:
            frame = SPINNER_FRAMES[self._spinner_idx % len(SPINNER_FRAMES)]
            elapsed = ""
            if self._task_start_time:
                secs = int(time.time() - self._task_start_time)
                elapsed = f" {secs}s"
            parts.append(f"{frame}{elapsed}")

        status.update(" \u00b7 ".join(parts))

    def _start_spinner(self):
        self._spinner_idx = 0
        self._task_start_time = time.time()
        self._iteration_count = 0
        self._spinner_timer = self.set_interval(0.1, self._tick_spinner)

    def _stop_spinner(self):
        if self._spinner_timer:
            self._spinner_timer.stop()
            self._spinner_timer = None
        self._task_start_time = None

    def _tick_spinner(self):
        self._spinner_idx += 1
        self._update_status()

    # ============================================================
    # Input Handling
    # ============================================================

    @on(Input.Submitted, "#user-input")
    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        input_widget = self.query_one("#user-input", Input)
        input_widget.value = ""

        # Handle keep/revert after build — empty Enter means "keep"
        if self.awaiting_keep_revert:
            response = text.lower().strip()
            if response in ("keep", "k", "y", "yes", "accept", "a", ""):
                self.awaiting_keep_revert = False
                if self._agent:
                    files = list(self._agent.modified_files.keys())
                    self._agent.clear_snapshots()
                    count = len(files)
                    self._log(Text.from_markup(
                        f"   [bold #3fb950]\u2713 Kept {count} file{'s' if count != 1 else ''}[/bold #3fb950]"
                    ))
                input_widget.placeholder = " \u276f What would you like me to do?  (/help for commands)"
                self._update_status()
                input_widget.focus()
            elif response in ("revert", "r", "undo", "n", "no", "reject"):
                self.awaiting_keep_revert = False
                if self._agent:
                    reverted = self._agent.revert_all()
                    count = len(reverted)
                    self._log(Text.from_markup(
                        f"   [bold #f85149]\u21ba Reverted {count} file{'s' if count != 1 else ''}[/bold #f85149]"
                    ))
                    for rpath in reverted:
                        rel = os.path.relpath(rpath, self._agent.working_directory)
                        self._log(Text.from_markup(
                            f"     [#6e7681]\u2192 {rich_escape(rel)}[/#6e7681]"
                        ))
                input_widget.placeholder = " \u276f What would you like me to do?  (/help for commands)"
                self._update_status()
                input_widget.focus()
            else:
                self._log(Text.from_markup(
                    "   [#e3b341]Type 'keep' or 'revert'[/#e3b341]"
                ))
            return

        # Handle build confirmation — empty Enter means "build"
        if self.awaiting_build and self._pending_plan is not None:
            response = text.lower().strip()
            if response in ("b", "build", "y", "yes", ""):
                self.awaiting_build = False
                input_widget.placeholder = " \u276f Building..."
                self._run_build()
            elif response in ("n", "no", "cancel", "c"):
                self.awaiting_build = False
                self._pending_task = None
                self._pending_plan = None
                self._log(Text.from_markup("   [#6e7681]Plan discarded.[/#6e7681]\n"))
                input_widget.placeholder = " \u276f What would you like me to do?  (/help for commands)"
                self._update_status()
            else:
                # Treat as a modification to the task — re-plan with feedback
                self.awaiting_build = False
                new_task = f"{self._pending_task}\n\nAdditional instructions: {text}"
                self._pending_task = None
                self._pending_plan = None
                self._run_task(new_task)
            return

        # Empty text with no special state — ignore
        if not text:
            return

        if text.startswith("/"):
            await self._handle_command(text)
            return

        if self.is_running:
            self._log(Text("   Agent is busy \u2014 Ctrl+C to cancel", style="italic #e3b341"))
            return

        self._run_task(text)

    async def _handle_command(self, command: str):
        parts = command.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/help":
            tbl = Table.grid(padding=(0, 2))
            tbl.add_column(style="bold #58a6ff")
            tbl.add_column(style="#8b949e")
            tbl.add_row("/help", "Show this help")
            tbl.add_row("/clear", "Clear the screen")
            tbl.add_row("/reset", "Reset conversation")
            tbl.add_row("/cd <path>", "Change directory")
            tbl.add_row("/model", "Model info")
            tbl.add_row("/tokens", "Token usage")
            tbl.add_row("", "")
            tbl.add_row("/sessions", "List sessions")
            tbl.add_row("/new [name]", "New session")
            tbl.add_row("/switch <name>", "Switch session")
            tbl.add_row("/rename <name>", "Rename session")
            tbl.add_row("/delete <name>", "Delete session")
            tbl.add_row("", "")
            tbl.add_row("/quit", "Exit")
            tbl.add_row("", "")
            tbl.add_row("Ctrl+C", "Cancel / Quit")
            tbl.add_row("Ctrl+L", "Clear screen")
            tbl.add_row("Ctrl+R", "Reset conversation")
            tbl.add_row("", "")
            tbl.add_row("[bold #58a6ff]Copying[/]", "")
            tbl.add_row("Ctrl+Y", "Copy last AI response")
            tbl.add_row("/copy", "Copy last AI response")
            tbl.add_row("/copy list", "Browse all outputs (pick by number)")
            tbl.add_row("/copy N", "Copy output #N from list")
            tbl.add_row("/copy error", "Copy last error")
            tbl.add_row("/copy all", "Copy entire conversation")
            if app_config.plan_phase_enabled:
                tbl.add_row("", "")
                tbl.add_row("[bold #d2a8ff]Plan/Build[/]", "Agent plans first, then waits for approval")
                tbl.add_row("  Enter/build", "Execute the plan")
                tbl.add_row("  no/cancel", "Discard the plan")
                tbl.add_row("  <text>", "Add feedback, re-plan")
            tbl.add_row("", "")
            tbl.add_row("[bold #3fb950]Keep/Revert[/]", "After build, review diff of all changes")
            tbl.add_row("  Enter/keep", "Accept all changes")
            tbl.add_row("  revert", "Undo all changes")
            self._log(Text(""))
            self._log(tbl)

        elif cmd == "/clear":
            self._clear_output()

        elif cmd == "/reset":
            if self._agent:
                self._agent.reset()
            self._session = self._store.create_session(
                working_directory=self.working_directory,
                model_id=model_config.model_id,
            )
            self._save_session()
            self._log(Text("   Conversation reset. New session started.", style="#3fb950"))
            self._update_status()

        elif cmd == "/cd":
            if arg:
                new_dir = os.path.abspath(os.path.join(self.working_directory, arg))
                if os.path.isdir(new_dir):
                    self.working_directory = new_dir
                    if self._agent:
                        self._agent.working_directory = new_dir
                        self._agent.system_prompt = BUILD_SYSTEM_PROMPT.format(working_directory=new_dir)
                    self._log(Text(f"   \u2713 {new_dir}", style="#3fb950"))
                    self._update_status()
                else:
                    self._log(Text(f"   \u2717 Not a directory: {new_dir}", style="#f85149"))
            else:
                self._log(Text(f"   {self.working_directory}", style="#8b949e"))

        elif cmd == "/model":
            model_cfg = get_model_config(model_config.model_id)
            tbl = Table.grid(padding=(0, 2))
            tbl.add_column(style="bold #8b949e", justify="right")
            tbl.add_column(style="#c9d1d9")
            tbl.add_row("Name", get_model_name(model_config.model_id))
            tbl.add_row("ID", model_config.model_id)
            tbl.add_row("Context", f"{model_cfg.get('context_window', 0):,}")
            tbl.add_row("Max Output", f"{model_cfg.get('max_output_tokens', 0):,}")
            tbl.add_row("Thinking", "adaptive" if supports_adaptive_thinking(model_config.model_id)
                         else ("enabled" if supports_thinking(model_config.model_id) else "no"))
            tbl.add_row("Budget", f"{model_cfg.get('thinking_max_budget', 0):,}")
            tbl.add_row("Caching", "yes" if supports_caching(model_config.model_id) else "no")
            tbl.add_row("Throughput", model_config.throughput_mode)
            self._log(Text(""))
            self._log(tbl)

        elif cmd == "/tokens":
            if self._agent:
                cache_read = self._agent._cache_read_tokens
                cache_write = self._agent._cache_write_tokens
                inp = self._agent._total_input_tokens
                out = self._agent._total_output_tokens
                total = self._agent.total_tokens

                tbl = Table.grid(padding=(0, 2))
                tbl.add_column(style="bold #8b949e", justify="right")
                tbl.add_column(style="#c9d1d9", justify="right")
                tbl.add_row("Input", f"{inp:,}")
                tbl.add_row("Output", f"{out:,}")
                tbl.add_row("Total", f"{total:,}")

                if cache_read > 0 or cache_write > 0:
                    tbl.add_row("", "")
                    tbl.add_row("Cache \u2191", f"{cache_read:,}")
                    tbl.add_row("Cache \u2193", f"{cache_write:,}")
                    if inp > 0:
                        eff = (inp - cache_read) + cache_read * 0.1 + cache_write * 0.25
                        pct = max(0, (1 - eff / inp)) * 100
                        tbl.add_row("Savings", f"~{pct:.0f}%")
                self._log(Text(""))
                self._log(tbl)
            else:
                self._log(Text("   No agent initialized.", style="#e3b341"))

        # ---- Session management commands ----

        elif cmd == "/sessions":
            sessions = self._store.list_sessions(self.working_directory)
            if not sessions:
                self._log(Text("   No sessions found.", style="#8b949e"))
            else:
                tbl = Table(padding=(0, 1), expand=False, box=None, show_header=True,
                            header_style="bold #8b949e")
                tbl.add_column("", width=2)
                tbl.add_column("Name", style="#c9d1d9")
                tbl.add_column("Msgs", style="#8b949e", justify="right")
                tbl.add_column("Tokens", style="#8b949e", justify="right")
                tbl.add_column("Updated", style="#6e7681")

                for sess in sessions:
                    is_current = (self._session and sess.session_id == self._session.session_id)
                    marker = "\u25cf" if is_current else " "
                    marker_style = "#58a6ff" if is_current else "#6e7681"
                    updated = sess.updated_at[:16].replace("T", " ") if sess.updated_at else "?"
                    tbl.add_row(
                        Text(marker, style=marker_style),
                        sess.name,
                        str(sess.message_count),
                        f"{sess.total_tokens:,}",
                        updated,
                    )
                self._log(Text(""))
                self._log(tbl)

        elif cmd == "/new":
            self._save_session()
            name = arg if arg else "default"
            if self._agent:
                self._agent.reset()
            self._session = self._store.create_session(
                working_directory=self.working_directory,
                model_id=model_config.model_id,
                name=name,
            )
            self._save_session()
            self._log(Text(f"   \u2713 New session: {name}", style="#3fb950"))
            self._update_status()

        elif cmd == "/switch":
            if not arg:
                self._log(Text("   Usage: /switch <name>", style="#e3b341"))
            else:
                target = self._store.find_by_name(self.working_directory, arg)
                if not target:
                    self._log(Text(f"   \u2717 Session not found: {arg}", style="#f85149"))
                    self._log(Text("   Use /sessions to list.", style="#6e7681"))
                else:
                    self._save_session()
                    self._switch_to_session(target)
                    msg_count = target.message_count
                    self._log(Text(
                        f"   \u2713 Switched to: {target.name} ({msg_count} messages)",
                        style="#3fb950",
                    ))
                    self._update_status()

        elif cmd == "/rename":
            if not arg:
                self._log(Text("   Usage: /rename <new name>", style="#e3b341"))
            elif self._session:
                old_name = self._session.name
                self._session = self._store.rename(self._session, arg)
                self._log(Text(f"   \u2713 {old_name} \u2192 {arg}", style="#3fb950"))
                self._update_status()

        elif cmd == "/delete":
            if not arg:
                self._log(Text("   Usage: /delete <name>", style="#e3b341"))
            else:
                target = self._store.find_by_name(self.working_directory, arg)
                if not target:
                    self._log(Text(f"   \u2717 Session not found: {arg}", style="#f85149"))
                elif self._session and target.session_id == self._session.session_id:
                    self._log(Text("   \u2717 Cannot delete current session. Switch first.", style="#f85149"))
                else:
                    self._store.delete(target.session_id)
                    self._log(Text(f"   \u2713 Deleted: {target.name}", style="#3fb950"))

        elif cmd == "/copy":
            # arg is already parsed from _handle_command's split
            if arg == "list" or arg == "ls":
                # Show recent outputs so user can pick one
                if not self._output_history:
                    self._log(Text("   No outputs yet", style="#6e7681"))
                else:
                    recent = self._output_history[-10:]
                    offset = len(self._output_history) - len(recent)
                    self._log(Text.from_markup("\n   [bold #58a6ff]Recent outputs[/] [#6e7681](use /copy N to copy one)[/#6e7681]"))
                    for i, (label, text) in enumerate(recent):
                        idx = offset + i + 1
                        preview = text[:70].replace("\n", " ")
                        self._log(Text.from_markup(
                            f"   [#58a6ff]{idx:>3}[/#58a6ff] [#8b949e]{label:>10}[/#8b949e]  {rich_escape(preview)}"
                        ))

            elif arg == "all":
                # Copy entire conversation
                all_text = "\n\n---\n\n".join(
                    f"[{label}]\n{text}" for label, text in self._output_history
                )
                if all_text:
                    self._copy_text(all_text)
                    self._log(Text.from_markup(
                        f"   [#3fb950]\u2713 Copied all outputs[/#3fb950] [#6e7681]({len(self._output_history)} items, {len(all_text)} chars)[/#6e7681]"
                    ))
                else:
                    self._log(Text("   No outputs yet", style="#6e7681"))

            elif arg == "error":
                if self._last_error:
                    self._copy_text(self._last_error)
                    self._log(Text.from_markup(
                        f"   [#3fb950]\u2713 Copied error[/#3fb950] [#6e7681]({len(self._last_error)} chars)[/#6e7681]"
                    ))
                else:
                    self._log(Text("   No errors to copy", style="#6e7681"))

            elif arg.isdigit():
                # Copy specific output by number
                idx = int(arg) - 1
                if 0 <= idx < len(self._output_history):
                    label, text = self._output_history[idx]
                    self._copy_text(text)
                    self._log(Text.from_markup(
                        f"   [#3fb950]\u2713 Copied #{arg} ({label})[/#3fb950] [#6e7681]({len(text)} chars)[/#6e7681]"
                    ))
                else:
                    self._log(Text.from_markup(
                        f"   [#6e7681]No output #{arg}. Use /copy list to see available.[/#6e7681]"
                    ))

            else:
                # Default: copy last response, fall back to error
                if self._last_response:
                    self._copy_text(self._last_response)
                    self._log(Text.from_markup(
                        f"   [#3fb950]\u2713 Copied last response[/#3fb950] [#6e7681]({len(self._last_response)} chars)[/#6e7681]"
                    ))
                elif self._last_error:
                    self._copy_text(self._last_error)
                    self._log(Text.from_markup(
                        f"   [#3fb950]\u2713 Copied last error[/#3fb950] [#6e7681]({len(self._last_error)} chars)[/#6e7681]"
                    ))
                else:
                    self._log(Text("   Nothing to copy yet. Use /copy list to browse.", style="#6e7681"))

        elif cmd == "/quit":
            self._save_session()
            self.exit()

        else:
            self._log(Text(f"   Unknown command: {cmd} \u2014 try /help", style="#e3b341"))

    # ============================================================
    # Agent Execution
    # ============================================================

    @work(thread=False)
    async def _run_task(self, task: str) -> None:
        if not self._agent:
            self._log(Text.from_markup(
                "   [bold #f85149]\u2717 Agent not initialized. Check AWS credentials.[/bold #f85149]"
            ))
            return

        input_widget = self.query_one("#user-input", Input)

        # Auto-name session from first task
        if self._session and self._session.name == "default" and not self._session.history:
            self._session = self._store.auto_name_session(self._session, task)

        # Show user message — clean, no box
        self._log(Text(""))
        self._log(Text.from_markup(f"[bold #f0f6fc]\u276f [/bold #f0f6fc][#c9d1d9]{rich_escape(task)}[/#c9d1d9]"))

        self.is_running = True
        self._start_spinner()
        self._update_status()

        # Reset plan tracking for the new task
        self._plan_step_widgets = {}
        self._plan_steps_done = set()
        self._last_tool_call = None

        try:
            intent = classify_intent(task, self._agent.service)
            if app_config.plan_phase_enabled and intent.get("plan"):
                # --- Plan phase: generate plan, show it, wait for user ---
                plan_steps = await self._agent.run_plan(
                    task=task,
                    on_event=self._handle_agent_event,
                )

                self._stop_spinner()
                self.is_running = False

                if plan_steps and not self._agent._cancelled:
                    # Store for build phase and prompt user
                    self._pending_task = task
                    self._pending_plan = plan_steps
                    self.awaiting_build = True

                    self._log(Text.from_markup(
                        "\n   [bold #d2a8ff]Press Enter or type 'build' to execute[/bold #d2a8ff]"
                        "[#6e7681]  \u00b7  'no' to discard  \u00b7  or type changes[/#6e7681]"
                    ))

                    input_widget.placeholder = " build / no / type feedback"
                    self._update_status()
                    input_widget.focus()
                    return  # wait for user input
                else:
                    self._log(Text.from_markup(
                        "   [#6e7681]No plan generated.[/#6e7681]"
                    ))
            else:
                # --- Direct mode: no plan gate ---
                await self._agent.run(
                    task=task,
                    on_event=self._handle_agent_event,
                    request_approval=self._handle_approval_request,
                )
        except Exception as e:
            logger.exception("Agent task error")
            self._log(Text.from_markup(
                f"\n   [bold #f85149]\u2717 {rich_escape(str(e))}[/bold #f85149]"
            ))
        finally:
            if not self.awaiting_build:
                self.is_running = False
                self.awaiting_approval = False
                self._stop_spinner()
                self._save_session()
                self._update_status()
                # Show diff / keep-revert if files were modified
                self._show_diff_and_prompt(input_widget)

    @work(thread=False)
    async def _run_build(self) -> None:
        """Execute the pending plan after user confirmation."""
        if not self._agent or not self._pending_task or not self._pending_plan:
            return

        input_widget = self.query_one("#user-input", Input)
        task = self._pending_task
        plan = self._pending_plan
        self._pending_task = None
        self._pending_plan = None

        self.is_running = True
        self._start_spinner()
        self._update_status()

        try:
            await self._agent.run_build(
                task=task,
                plan_steps=plan,
                on_event=self._handle_agent_event,
                request_approval=self._handle_approval_request,
            )
        except Exception as e:
            logger.exception("Build phase error")
            self._log(Text.from_markup(
                f"\n   [bold #f85149]\u2717 {rich_escape(str(e))}[/bold #f85149]"
            ))
        finally:
            self.is_running = False
            self.awaiting_approval = False
            self._stop_spinner()
            self._save_session()
            self._update_status()

        # Show diff and keep/revert prompt if files were modified
        self._show_diff_and_prompt(input_widget)

    def _show_diff_and_prompt(self, input_widget: Input) -> None:
        """After build, show git-style diffs and prompt for keep/revert."""
        if not self._agent:
            input_widget.placeholder = " \u276f What would you like me to do?  (/help for commands)"
            input_widget.focus()
            return

        modified = self._agent.modified_files
        if not modified:
            # No files were modified — nothing to keep/revert
            self._agent.clear_snapshots()
            input_widget.placeholder = " \u276f What would you like me to do?  (/help for commands)"
            input_widget.focus()
            return

        scroll = self.query_one("#output-scroll", VerticalScroll)

        # --- Diff header ---
        file_count = len(modified)
        self._log(Text(""))
        self._log(Text.from_markup(
            f"   [bold #d2a8ff]\u2500\u2500\u2500 changes [/][#6e7681]\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500[/]"
        ))
        self._log(Text.from_markup(
            f"   [#c9d1d9]{file_count} file{'s' if file_count != 1 else ''} changed[/#c9d1d9]"
        ))

        # --- Per-file diffs ---
        for abs_path, original_content in modified.items():
            rel_path = os.path.relpath(abs_path, self._agent.working_directory)

            # Read current (modified) content
            try:
                if os.path.exists(abs_path):
                    with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                        current_content = f.read()
                else:
                    current_content = ""  # file was deleted somehow
            except Exception:
                current_content = ""

            old_lines = (original_content or "").splitlines(keepends=True)
            new_lines = current_content.splitlines(keepends=True)

            if original_content is None:
                label = "new file"
                label_color = "#3fb950"
            elif not current_content:
                label = "deleted"
                label_color = "#f85149"
            else:
                label = "modified"
                label_color = "#d29922"

            # Generate unified diff
            diff_lines = list(difflib.unified_diff(
                old_lines, new_lines,
                fromfile=f"a/{rel_path}",
                tofile=f"b/{rel_path}",
                lineterm="",
            ))

            if not diff_lines:
                continue  # no actual changes

            # Count additions and deletions
            additions = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
            deletions = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))

            # Build colorized diff text
            colored_diff = Text()
            for line in diff_lines:
                line_str = line.rstrip("\n")
                if line_str.startswith("+++") or line_str.startswith("---"):
                    colored_diff.append(line_str + "\n", style="bold #8b949e")
                elif line_str.startswith("@@"):
                    colored_diff.append(line_str + "\n", style="#79c0ff")
                elif line_str.startswith("+"):
                    colored_diff.append(line_str + "\n", style="#3fb950")
                elif line_str.startswith("-"):
                    colored_diff.append(line_str + "\n", style="#f85149")
                else:
                    colored_diff.append(line_str + "\n", style="#6e7681")

            # Collapsible diff per file — click to expand the full diff
            title = f"{rel_path}  ({label})  +{additions} -{deletions}"
            body_widget = Static(colored_diff, id=self._next_id("diff-body"))
            coll = Collapsible(
                body_widget,
                title=title,
                collapsed=True,
                id=self._next_id("diff-coll"),
            )
            scroll.mount(coll)

        self._log(Text(""))
        self._log(Text.from_markup(
            "   [bold #3fb950]keep[/bold #3fb950][#6e7681] \u00b7 accept all changes    "
            "[/][bold #f85149]revert[/bold #f85149][#6e7681] \u00b7 undo everything[/#6e7681]"
        ))

        self.awaiting_keep_revert = True
        input_widget.placeholder = " keep / revert"
        self._update_status()
        input_widget.focus()
        scroll.scroll_end(animate=False)

    async def _handle_agent_event(self, event: AgentEvent) -> None:
        scroll = self.query_one("#output-scroll", VerticalScroll)

        # --- Phase events (plan / build) ---
        if event.type == "phase_start":
            phase = event.content  # "plan" or "build"
            if phase == "plan":
                self._log(Text(""))
                self._log(Text.from_markup(
                    "   [bold #d2a8ff]\u2500\u2500\u2500 plan [/][#6e7681]\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500[/]"
                ))
            elif phase == "build":
                self._log(Text(""))
                self._log(Text.from_markup(
                    "   [bold #79c0ff]\u2500\u2500\u2500 build [/][#6e7681]\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500[/]"
                ))

        elif event.type == "phase_plan":
            # Display plan steps as TODO items
            steps = event.data.get("steps", []) if event.data else []
            if not steps:
                steps = [l.strip() for l in event.content.strip().split("\n") if l.strip()]

            self._plan_step_widgets = {}  # id -> Static widget for live updates

            # Store plan text for /copy
            self._last_response = "\n".join(steps)
            self._record_output("plan", self._last_response)

            for i, step in enumerate(steps):
                # Strip the leading number/bullet if present for cleaner display
                clean = re.sub(r"^\d+[\.\)]\s*", "", step)
                clean = re.sub(r"^[-*]\s*", "", clean)

                wid = self._next_id("plan-step")
                w = Static(
                    Text.from_markup(
                        f"   [#6e7681]\u25cb[/#6e7681]  [#c9d1d9]{rich_escape(clean)}[/#c9d1d9]"
                    ),
                    id=wid,
                )
                scroll.mount(w)
                self._plan_step_widgets[i] = w

            self._log(Text(""))
            scroll.scroll_end(animate=False)

        elif event.type == "phase_end":
            phase = event.content
            if phase == "build":
                # Mark any remaining plan steps as done
                if hasattr(self, "_plan_step_widgets"):
                    for idx, w in self._plan_step_widgets.items():
                        try:
                            current = w.renderable
                            if isinstance(current, Text) and "\u25cb" in str(current):
                                # Still pending — mark as done
                                txt = str(current).replace("\u25cb", "\u2713")
                                w.update(Text.from_markup(
                                    txt.replace("#6e7681", "#3fb950").replace("#c9d1d9", "#3fb950")
                                ))
                        except Exception:
                            pass

                self._log(Text(""))
                self._log(Text.from_markup(
                    "   [#3fb950]\u2713 build complete[/#3fb950]"
                ))
                self._log(Text.from_markup(
                    "   [#6e7681]\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500[/#6e7681]"
                ))

        # --- Scout sub-agent events (shown as a single updating line) ---
        elif event.type == "scout_start":
            self._scout_widget = Static(
                Text.from_markup(
                    f"   [#8957e5]\u25cf[/#8957e5] [#8b949e]scanning codebase\u2026[/#8b949e]"
                ),
                id=self._next_id("scout"),
            )
            scroll.mount(self._scout_widget)
            scroll.scroll_end(animate=False)

        elif event.type == "scout_progress":
            # Update the scout line in place — just show what it's reading now
            if hasattr(self, "_scout_widget") and self._scout_widget:
                short = event.content
                if len(short) > 60:
                    short = short[:57] + "\u2026"
                self._scout_widget.update(Text.from_markup(
                    f"   [#8957e5]\u25cf[/#8957e5] [#8b949e]scanning\u2026 {rich_escape(short)}[/#8b949e]"
                ))
                scroll.scroll_end(animate=False)

        elif event.type == "scout_end":
            if hasattr(self, "_scout_widget") and self._scout_widget:
                self._scout_widget.update(Text.from_markup(
                    f"   [#8957e5]\u25cf[/#8957e5] [#8b949e]codebase scanned[/#8b949e]"
                ))
                self._scout_widget = None

        # --- Thinking (streams live, then collapses) ---
        elif event.type == "thinking_start":
            self._thinking_content = ""
            self._thinking_widget = Static(
                Text("\u25cf thinking\u2026", style="italic #6e40c9"),
                id=self._next_id("think"),
                classes="thinking-live",
            )
            scroll.mount(self._thinking_widget)
            scroll.scroll_end(animate=False)

        elif event.type == "thinking":
            self._thinking_content += event.content
            if self._thinking_widget:
                lines = self._thinking_content.split("\n")
                if len(lines) > 20:
                    visible = "\n".join(lines[-20:])
                    display = f"\u2026 ({len(lines) - 20} earlier lines)\n{visible}"
                else:
                    display = self._thinking_content
                self._thinking_widget.update(
                    Text(f"\u25cf thinking\u2026\n{display}", style="#8b949e")
                )
                scroll.scroll_end(animate=False)

        elif event.type == "thinking_end":
            if self._thinking_widget:
                self._thinking_widget.remove()
                self._thinking_widget = None

            if self._thinking_content:
                full_text = self._thinking_content.strip()
                line_count = len(full_text.split("\n"))
                title = f"\u25cf thought for {line_count} lines \u2014 click to expand"
                self._log_collapsible(
                    title=title,
                    full_content=full_text,
                    collapsed=True,
                    style="#8b949e",
                )

        # --- Text (streams live, character by character) ---
        elif event.type == "text_start":
            self._current_text = ""
            self._text_widget = Static(
                Text("\u258c", style="#58a6ff"),
                id=self._next_id("text"),
                classes="text-stream",
            )
            scroll.mount(self._text_widget)
            scroll.scroll_end(animate=False)

        elif event.type == "text":
            self._current_text += event.content
            if self._text_widget:
                # Render live markdown-ish text with a blinking cursor
                try:
                    md = Markdown(self._current_text)
                    self._text_widget.update(md)
                except Exception:
                    self._text_widget.update(
                        Text(self._current_text + "\u258c", style="#c9d1d9")
                    )
                scroll.scroll_end(animate=False)

        elif event.type == "text_end":
            if self._text_widget:
                # Final render without cursor
                if self._current_text.strip():
                    self._last_response = self._current_text.strip()
                    self._record_output("response", self._last_response)
                    try:
                        md = Markdown(self._last_response)
                        self._text_widget.update(md)
                    except Exception:
                        self._text_widget.update(
                            Text(self._last_response, style="#c9d1d9")
                        )
                else:
                    self._text_widget.remove()
                self._text_widget = None

        # --- Tool Calls ---
        elif event.type == "tool_call":
            data = event.data or {}
            tool_name = data.get("name", "?")
            tool_input = data.get("input", {})
            icon = TOOL_ICONS.get(tool_name, "\u2022 ")
            is_danger = tool_name in TOOL_DANGER

            if tool_name == "read_file":
                path = rich_escape(tool_input.get("path", "?"))
                desc = f"[bold]{path}[/bold]"
            elif tool_name == "write_file":
                path = rich_escape(tool_input.get("path", "?"))
                content = tool_input.get("content", "")
                lc = content.count("\n") + 1
                desc = f"[bold]{path}[/bold] [#6e7681]({lc} lines)[/#6e7681]"
            elif tool_name == "edit_file":
                path = rich_escape(tool_input.get("path", "?"))
                desc = f"[bold]{path}[/bold]"
            elif tool_name == "run_command":
                cmd_str = rich_escape(tool_input.get("command", "?"))
                if len(cmd_str) > 100:
                    cmd_str = cmd_str[:97] + "\u2026"
                desc = f"[bold #e3b341]$ {cmd_str}[/bold #e3b341]"
            elif tool_name == "search":
                pattern = rich_escape(tool_input.get("pattern", "?"))
                desc = f"[bold]{pattern}[/bold]"
            elif tool_name == "list_directory":
                path = rich_escape(tool_input.get("path", "."))
                desc = f"[bold]{path}[/bold]"
            elif tool_name == "glob_find":
                pattern = rich_escape(tool_input.get("pattern", "?"))
                desc = f"[bold]{pattern}[/bold]"
            else:
                desc = f"[bold]{rich_escape(tool_name)}[/bold] {rich_escape(json.dumps(tool_input)[:60])}"

            color = "#f0883e" if is_danger else "#3fb950"
            self._log(Text.from_markup(f"   [{color}]{icon}{desc}[/{color}]"))
            self._iteration_count += 1
            self._last_tool_call = {"name": tool_name, "input": tool_input}

        # --- Tool Results (expandable) ---
        elif event.type == "tool_result":
            data = event.data or {}
            success = data.get("success", False)
            result_text = event.content
            tool_label = data.get("tool_name", "tool")
            self._record_output(tool_label, result_text)
            result_lines = result_text.split("\n")
            line_count = len(result_lines)
            char_count = len(result_text)

            ok = "\u2713" if success else "\u2717"
            ok_color = "#3fb950" if success else "#f85149"
            content_style = "#6e7681" if success else "#f85149"

            if line_count > COLLAPSE_LINE_THRESHOLD or char_count > COLLAPSE_CHAR_THRESHOLD:
                first_line = result_lines[0][:80] if result_lines else ""
                title = f"   {ok} {first_line}  ({line_count} lines)"
                self._log_collapsible(
                    title=title,
                    full_content=result_text,
                    collapsed=True,
                    style=content_style,
                )
            else:
                short = result_text[:300]
                if char_count > 300:
                    short += "\u2026"
                self._log(Text(f"   {ok} {short}", style=content_style))

            # Update plan step tracking
            if self._last_tool_call:
                self._mark_plan_step(
                    self._last_tool_call["name"],
                    self._last_tool_call["input"],
                    success,
                )
                self._last_tool_call = None

        # --- Stream recovery events ---
        elif event.type == "stream_recovering":
            if self._thinking_widget:
                self._thinking_widget.remove()
                self._thinking_widget = None
                self._thinking_content = ""
            if self._text_widget:
                self._text_widget.remove()
                self._text_widget = None
            self._current_text = ""
            self._log(Text.from_markup(
                f"   [#e3b341]\u26a0 {rich_escape(event.content)}[/#e3b341]"
            ))

        elif event.type == "stream_retry":
            self._log(Text.from_markup(
                f"   [#58a6ff]\u21bb {rich_escape(event.content)}[/#58a6ff]"
            ))

        elif event.type == "stream_failed":
            self._last_error = event.content
            self._record_output("error", event.content)
            self._log(Text.from_markup(
                f"   [bold #f85149]\u26a0 {rich_escape(event.content)}[/bold #f85149]"
            ))

        # --- Errors ---
        elif event.type == "error":
            self._last_error = event.content
            self._record_output("error", event.content)
            self._log(Text.from_markup(
                f"\n   [bold #f85149]\u2717 {rich_escape(event.content)}[/bold #f85149]"
            ))

        # --- Done ---
        elif event.type == "done":
            elapsed = ""
            if self._task_start_time:
                secs = round(time.time() - self._task_start_time, 1)
                elapsed = f"{secs}s"
                if self._iteration_count > 0:
                    elapsed += f" \u00b7 {self._iteration_count} tool calls"
            if elapsed:
                self._log(Text.from_markup(
                    f"\n   [#484f58]{elapsed}[/#484f58]"
                ))
            self._update_status()

        elif event.type == "cancelled":
            self._log(Text.from_markup("\n   [#e3b341]cancelled[/#e3b341]"))

    # ============================================================
    # Approval
    # ============================================================

    async def _handle_approval_request(
        self, tool_name: str, description: str, inputs: dict
    ) -> bool:
        """Auto-approve all operations. User reviews via keep/revert after build."""
        return True

    # ============================================================
    # Actions
    # ============================================================

    def action_cancel_or_quit(self) -> None:
        if self.awaiting_approval and self._approval_future:
            self._approval_future.set_result(False)
        elif self.is_running and self._agent:
            self._agent.cancel()
            self._log(Text.from_markup("   [italic #e3b341]cancelling\u2026[/italic #e3b341]"))
        else:
            self._save_session()
            self.exit()

    def action_clear_screen(self) -> None:
        self._clear_output()

    def action_reset_conversation(self) -> None:
        if self._agent and not self.is_running:
            self._agent.reset()
            self._session = self._store.create_session(
                working_directory=self.working_directory,
                model_id=model_config.model_id,
            )
            self._save_session()
            self._log(Text("   \u2713 Conversation reset.", style="#3fb950"))
            self._update_status()

    def _record_output(self, label: str, text: str) -> None:
        """Record an output for /copy history. Keeps last 50."""
        self._output_history.append((label, text))
        if len(self._output_history) > 50:
            self._output_history = self._output_history[-50:]

    def _copy_text(self, text: str) -> None:
        """Copy text to system clipboard. Uses pbcopy (macOS) as primary,
        with OSC 52 as fallback for remote/SSH terminals."""
        import subprocess, platform
        copied = False

        # Primary: pbcopy on macOS — reliable, always overwrites clipboard
        if platform.system() == "Darwin":
            try:
                result = subprocess.run(
                    ["pbcopy"],
                    input=text.encode("utf-8"),
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                )
                copied = result.returncode == 0
            except Exception:
                pass

        # Fallback: xclip / xsel on Linux
        if not copied:
            for cmd in [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]:
                try:
                    result = subprocess.run(
                        cmd,
                        input=text.encode("utf-8"),
                        stderr=subprocess.DEVNULL,
                        timeout=3,
                    )
                    if result.returncode == 0:
                        copied = True
                        break
                except Exception:
                    continue

        # Last resort: OSC 52 escape sequence (works in iTerm2, kitty, some SSH)
        if not copied:
            try:
                self.copy_to_clipboard(text)
            except Exception:
                pass

    def action_copy_last(self) -> None:
        """Copy the most recent output (response, error, tool result — whatever came last)."""
        if self._output_history:
            label, text = self._output_history[-1]
            self._copy_text(text)
            self._log(Text.from_markup(
                f"   [#3fb950]\u2713 Copied last {rich_escape(label)}[/#3fb950] [#6e7681]({len(text)} chars)[/#6e7681]"
            ))
        else:
            self._log(Text("   Nothing to copy yet", style="#6e7681"))


# ============================================================
# Entry Point
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Bedrock Codex - Coding Agent Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                    Run in current directory
  python main.py -d ~/my-project    Run in a specific project directory
  python main.py --no-thinking      Disable extended thinking
        """,
    )
    parser.add_argument(
        "-d", "--directory",
        default=".",
        help="Working directory for the agent (default: current directory)",
    )
    parser.add_argument(
        "--no-thinking",
        action="store_true",
        help="Disable extended thinking",
    )

    args = parser.parse_args()

    if args.no_thinking:
        os.environ["ENABLE_THINKING"] = "false"

    working_dir = os.path.abspath(args.directory)
    if not os.path.isdir(working_dir):
        print(f"Error: {working_dir} is not a directory")
        sys.exit(1)

    app = BedrockCodexApp(working_directory=working_dir)
    app.run()


if __name__ == "__main__":
    main()
