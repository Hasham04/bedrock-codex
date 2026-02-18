"""
Agent event and policy decision data types.
"""

from dataclasses import dataclass
from typing import Dict, Any, Optional


@dataclass
class AgentEvent:
    """Event emitted during agent execution"""
    type: str  # phase_start, tool_call, tool_result, text, thinking, error, done, etc.
    content: str = ""
    data: Optional[Dict[str, Any]] = None


@dataclass
class PolicyDecision:
    """Policy engine decision for requested operation"""
    require_approval: bool = False
    blocked: bool = False
    reason: str = ""