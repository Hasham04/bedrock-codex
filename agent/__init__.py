"""
Agent package - modular coding agent implementation.

This package contains the core coding agent functionality split into logical modules:
- events: AgentEvent and PolicyDecision data types
- prompts: System prompt templates and composition
- intent: Intent classification for task routing
- plan: Plan parsing and extraction utilities
- verification: Policy engine and failure pattern learning
- context: Session state and context management
- execution: Core agent loop and parallel tool execution
- history: Token estimation, trimming, and history repair
- scout: Fast codebase exploration with cheap models
- recovery: Error recovery, confidence assessment, verification caching
- planning: Plan generation and quality assessment
- building: Build/run orchestration and post-build verification
- progressive_verification: Multi-stage verification pipeline
- core: Main CodingAgent orchestrator class (slim)
"""

# Core classes and data types
from .core import CodingAgent
from .events import AgentEvent, PolicyDecision

# Mixins
from .context import ContextMixin
from .verification import VerificationMixin
from .execution import ExecutionMixin
from .history import HistoryMixin
from .scout import ScoutMixin
from .recovery import RecoveryMixin
from .planning import PlanningMixin
from .building import BuildMixin
from .progressive_verification import ProgressiveVerificationMixin

# Intent classification and planning
from .intent import classify_intent, needs_planning, CLASSIFY_SYSTEM
from .plan import _strip_plan_preamble, _extract_plan

# Prompt system
from .prompts import (
    _compose_system_prompt,
    _format_build_system_prompt,
    _detect_project_language,
    AVAILABLE_TOOL_NAMES,
    SCOUT_TOOL_NAMES,
    SCOUT_TOOL_DISPLAY_NAMES,
    PHASE_MODULES,
    LANG_MODULES,
)

__all__ = [
    # Main agent class
    "CodingAgent",

    # Data types
    "AgentEvent",
    "PolicyDecision",

    # Mixins
    "ContextMixin",
    "VerificationMixin",
    "ExecutionMixin",
    "HistoryMixin",
    "ScoutMixin",
    "RecoveryMixin",
    "PlanningMixin",
    "BuildMixin",
    "ProgressiveVerificationMixin",

    # Intent classification
    "classify_intent",
    "needs_planning",
    "CLASSIFY_SYSTEM",

    # Plan utilities
    "_strip_plan_preamble",
    "_extract_plan",

    # Prompt system
    "_compose_system_prompt",
    "_format_build_system_prompt",
    "_detect_project_language",
    "AVAILABLE_TOOL_NAMES",
    "SCOUT_TOOL_NAMES",
    "SCOUT_TOOL_DISPLAY_NAMES",
    "PHASE_MODULES",
    "LANG_MODULES",
]
