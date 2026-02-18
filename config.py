"""
Configuration module for Bedrock Codex.
Handles all environment variables, model specifications, and application settings.
"""

import os
from dataclasses import dataclass
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv, set_key

# Load environment variables from .env file
load_dotenv()


@dataclass
class AWSConfig:
    """AWS-specific configuration"""
    region: str = os.getenv("AWS_REGION", "us-east-1")
    access_key_id: str = os.getenv("AWS_ACCESS_KEY_ID", "")
    secret_access_key: str = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    session_token: str = os.getenv("AWS_SESSION_TOKEN", "")
    profile_name: str = os.getenv("AWS_PROFILE", "")
    
    def has_explicit_credentials(self) -> bool:
        return bool(self.access_key_id and self.secret_access_key)
    
    def has_session_token(self) -> bool:
        return bool(self.session_token)
    
    def has_profile(self) -> bool:
        return bool(self.profile_name)


@dataclass
class ModelConfig:
    """Model-specific configuration"""
    model_id: str = os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-opus-4-6-v1")
    max_tokens: int = int(os.getenv("MAX_TOKENS", "128000"))
    temperature: Optional[float] = float(os.getenv("TEMPERATURE", "1")) if os.getenv("TEMPERATURE") else None
    top_p: Optional[float] = float(os.getenv("TOP_P", "")) if os.getenv("TOP_P") else None
    top_k: Optional[int] = int(os.getenv("TOP_K", "")) if os.getenv("TOP_K") else None
    throughput_mode: str = os.getenv("THROUGHPUT_MODE", "cross-region")
    
    # Extended thinking settings
    enable_thinking: bool = os.getenv("ENABLE_THINKING", "true").lower() == "true"
    thinking_budget: int = int(os.getenv("THINKING_BUDGET", "120000"))
    # Deep (fixed-budget) vs adaptive thinking. Default: deep for predictable reasoning depth.
    use_adaptive_thinking: bool = os.getenv("USE_ADAPTIVE_THINKING", "false").lower() == "true"
    adaptive_thinking_effort: str = os.getenv("ADAPTIVE_THINKING_EFFORT", "high")


@dataclass
class AppConfig:
    """Application-specific configuration"""
    title: str = "Bedrock Codex"
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    debug_mode: bool = os.getenv("DEBUG_MODE", "false").lower() == "true"
    working_directory: str = os.getenv("WORKING_DIRECTORY", ".")
    max_tool_iterations: int = int(os.getenv("MAX_TOOL_ITERATIONS", "200"))
    auto_approve_reads: bool = os.getenv("AUTO_APPROVE_READS", "true").lower() == "true"
    # Stream recovery settings
    stream_max_retries: int = int(os.getenv("STREAM_MAX_RETRIES", "3"))
    stream_retry_backoff: float = float(os.getenv("STREAM_RETRY_BACKOFF", "2"))
    # Scout sub-agent settings
    scout_enabled: bool = os.getenv("SCOUT_ENABLED", "true").lower() == "true"
    scout_model: str = os.getenv("SCOUT_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    scout_max_iterations: int = int(os.getenv("SCOUT_MAX_ITERATIONS", "8"))
    # Plan-then-Build phase
    plan_phase_enabled: bool = os.getenv("PLAN_PHASE_ENABLED", "true").lower() == "true"
    # YOLO mode — auto-approve all operations including shell commands
    auto_approve_commands: bool = os.getenv("AUTO_APPROVE_COMMANDS", "false").lower() == "true"
    # Fast model for simple tasks (smart routing)
    fast_model: str = os.getenv("FAST_MODEL", "us.anthropic.claude-sonnet-4-20250514-v1:0")
    # Refine user task into output spec + constraints before planning (Cursor-style)
    task_refinement_enabled: bool = os.getenv("TASK_REFINEMENT_ENABLED", "false").lower() == "true"
    # Enforce structured reasoning traces in user-visible responses
    enforce_reasoning_trace: bool = os.getenv("ENFORCE_REASONING_TRACE", "true").lower() == "true"
    # Deterministic verification gate before final "done"
    deterministic_verification_gate: bool = os.getenv("DETERMINISTIC_VERIFICATION_GATE", "true").lower() == "true"
    # Run targeted tests in deterministic gate when test files are discovered
    deterministic_verification_run_tests: bool = os.getenv("DETERMINISTIC_VERIFICATION_RUN_TESTS", "true").lower() == "true"
    # Language/framework-aware verification orchestrator
    verification_orchestrator_enabled: bool = os.getenv("VERIFICATION_ORCHESTRATOR_ENABLED", "true").lower() == "true"
    # Human review gate before build execution starts
    human_review_mode: bool = os.getenv("HUMAN_REVIEW_MODE", "false").lower() == "true"
    # Policy engine for risky operations
    policy_engine_enabled: bool = os.getenv("POLICY_ENGINE_ENABLED", "true").lower() == "true"
    block_destructive_commands: bool = os.getenv("BLOCK_DESTRUCTIVE_COMMANDS", "true").lower() == "true"
    # Learning loop from failures
    learning_loop_enabled: bool = os.getenv("LEARNING_LOOP_ENABLED", "true").lower() == "true"
    # Manager-worker planning assistance (parallel worker insights)
    parallel_subagents_enabled: bool = os.getenv("PARALLEL_SUBAGENTS_ENABLED", "true").lower() == "true"
    parallel_subagents_max_workers: int = int(os.getenv("PARALLEL_SUBAGENTS_MAX_WORKERS", "3"))
    # Stream command output incrementally while command runs
    live_command_streaming: bool = os.getenv("LIVE_COMMAND_STREAMING", "true").lower() == "true"
    # Session checkpoints and rewind support for risky batches
    session_checkpoints_enabled: bool = os.getenv("SESSION_CHECKPOINTS_ENABLED", "true").lower() == "true"
    # Test impact selection: run likely impacted tests before full suite
    test_impact_selection_enabled: bool = os.getenv("TEST_IMPACT_SELECTION_ENABLED", "true").lower() == "true"
    test_run_full_after_impact: bool = os.getenv("TEST_RUN_FULL_AFTER_IMPACT", "true").lower() == "true"
    # Enterprise: semantic codebase index (Cursor-style)
    codebase_index_enabled: bool = os.getenv("CODEBASE_INDEX_ENABLED", "true").lower() == "true"
    embedding_model_id: str = os.getenv("EMBEDDING_MODEL_ID", "cohere.embed-english-v3")


# ============================================================
# Model Specifications -- Anthropic Claude on Bedrock
# All models support tool_use which is required for the agent loop.
# Non-Anthropic models (Titan, Llama, Mistral) are excluded as they
# do not support tool_use via InvokeModel.
# ============================================================
AVAILABLE_MODELS: List[Dict[str, Any]] = [
    # ----- Claude 4.6 family (latest) -----
    {
        "id": "us.anthropic.claude-opus-4-6-v1",
        "base_id": "anthropic.claude-opus-4-6-v1",
        "name": "Claude Opus 4.6 (Latest)",
        "description": "Most intelligent model for agents and coding, 200K ctx, 128K output",
        "provider": "anthropic",
        "context_window": 200000,
        "max_output_tokens": 128000,
        "default_max_tokens": 128000,
        "supports_both_sampling": False,
        "requires_profile": True,
        "throughput_options": ["cross-region"],
        "supports_extended_context": True,
        "supports_thinking": True,
        "supports_adaptive_thinking": True,
        "thinking_max_budget": 128000,
        "default_thinking_budget": 120000,
        "supports_caching": True,
        "cache_min_tokens": 1024,
        "cache_ttl_options": ["5m"],
    },
    # ----- Claude 4.5 family -----
    {
        "id": "us.anthropic.claude-opus-4-5-20251101-v1:0",
        "base_id": "anthropic.claude-opus-4-5-20251101-v1:0",
        "name": "Claude Opus 4.5",
        "description": "Flagship model with extended thinking, 200K ctx, 128K output",
        "provider": "anthropic",
        "context_window": 200000,
        "max_output_tokens": 128000,
        "default_max_tokens": 128000,
        "supports_both_sampling": False,
        "requires_profile": True,
        "throughput_options": ["cross-region"],
        "supports_extended_context": True,
        "supports_thinking": True,
        "supports_adaptive_thinking": False,
        "thinking_max_budget": 128000,
        "default_thinking_budget": 120000,
        "supports_caching": True,
        "cache_min_tokens": 4096,
        "cache_ttl_options": ["5m", "1h"],
    },
    {
        "id": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "base_id": "anthropic.claude-sonnet-4-5-20250929-v1:0",
        "name": "Claude Sonnet 4.5",
        "description": "Best for coding and complex agents, 200K ctx, 64K output",
        "provider": "anthropic",
        "context_window": 200000,
        "max_output_tokens": 64000,
        "default_max_tokens": 64000,
        "supports_both_sampling": False,
        "requires_profile": True,
        "throughput_options": ["cross-region"],
        "supports_extended_context": True,
        "supports_thinking": True,
        "supports_adaptive_thinking": False,
        "thinking_max_budget": 64000,
        "default_thinking_budget": 60000,
        "supports_caching": True,
        "cache_min_tokens": 1024,
        "cache_ttl_options": ["5m", "1h"],
    },
    {
        "id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "base_id": "anthropic.claude-haiku-4-5-20251001-v1:0",
        "name": "Claude Haiku 4.5",
        "description": "Fastest with near-frontier intelligence, 200K ctx, 64K output",
        "provider": "anthropic",
        "context_window": 200000,
        "max_output_tokens": 64000,
        "default_max_tokens": 64000,
        "supports_both_sampling": False,
        "requires_profile": True,
        "throughput_options": ["cross-region"],
        "supports_extended_context": True,
        "supports_thinking": True,
        "supports_adaptive_thinking": False,
        "thinking_max_budget": 64000,
        "default_thinking_budget": 60000,
        "supports_caching": True,
        "cache_min_tokens": 4096,
        "cache_ttl_options": ["5m", "1h"],
    },
    # ----- Claude 4.x family -----
    {
        "id": "us.anthropic.claude-opus-4-1-20250805-v1:0",
        "base_id": "anthropic.claude-opus-4-1-20250805-v1:0",
        "name": "Claude Opus 4.1",
        "description": "Highly capable with extended thinking, 200K ctx, 128K output",
        "provider": "anthropic",
        "context_window": 200000,
        "max_output_tokens": 128000,
        "default_max_tokens": 128000,
        "supports_both_sampling": False,
        "requires_profile": True,
        "throughput_options": ["cross-region"],
        "supports_extended_context": True,
        "supports_thinking": True,
        "supports_adaptive_thinking": False,
        "thinking_max_budget": 128000,
        "default_thinking_budget": 120000,
        "supports_caching": True,
        "cache_min_tokens": 1024,
        "cache_ttl_options": ["5m"],
    },
    {
        "id": "us.anthropic.claude-opus-4-20250514-v1:0",
        "base_id": "anthropic.claude-opus-4-20250514-v1:0",
        "name": "Claude Opus 4",
        "description": "First Claude 4 Opus with extended thinking, 200K ctx",
        "provider": "anthropic",
        "context_window": 200000,
        "max_output_tokens": 128000,
        "default_max_tokens": 128000,
        "supports_both_sampling": False,
        "requires_profile": True,
        "throughput_options": ["cross-region"],
        "supports_extended_context": True,
        "supports_thinking": True,
        "supports_adaptive_thinking": False,
        "thinking_max_budget": 128000,
        "default_thinking_budget": 120000,
        "supports_caching": True,
        "cache_min_tokens": 1024,
        "cache_ttl_options": ["5m"],
    },
    {
        "id": "us.anthropic.claude-sonnet-4-20250514-v1:0",
        "base_id": "anthropic.claude-sonnet-4-20250514-v1:0",
        "name": "Claude Sonnet 4",
        "description": "Balanced performance with extended thinking, 200K ctx, 64K output",
        "provider": "anthropic",
        "context_window": 200000,
        "max_output_tokens": 64000,
        "default_max_tokens": 64000,
        "supports_both_sampling": False,
        "requires_profile": True,
        "throughput_options": ["cross-region"],
        "supports_extended_context": True,
        "supports_thinking": True,
        "supports_adaptive_thinking": False,
        "thinking_max_budget": 64000,
        "default_thinking_budget": 60000,
        "supports_caching": True,
        "cache_min_tokens": 1024,
        "cache_ttl_options": ["5m"],
    },
    # ----- Claude 3.x family -----
    {
        "id": "us.anthropic.claude-3-7-sonnet-20250219-v1:0",
        "base_id": "anthropic.claude-3-7-sonnet-20250219-v1:0",
        "name": "Claude 3.7 Sonnet",
        "description": "Extended thinking Sonnet, 200K ctx, 16K output",
        "provider": "anthropic",
        "context_window": 200000,
        "max_output_tokens": 16000,
        "default_max_tokens": 16000,
        "supports_both_sampling": False,
        "requires_profile": True,
        "throughput_options": ["cross-region"],
        "supports_extended_context": True,
        "supports_thinking": True,
        "supports_adaptive_thinking": False,
        "thinking_max_budget": 64000,
        "default_thinking_budget": 16000,
        "supports_caching": True,
        "cache_min_tokens": 1024,
        "cache_ttl_options": ["5m"],
    },
    {
        "id": "anthropic.claude-3-5-haiku-20241022-v1:0",
        "base_id": "anthropic.claude-3-5-haiku-20241022-v1:0",
        "name": "Claude 3.5 Haiku",
        "description": "Fast and efficient, 200K ctx, 8K output",
        "provider": "anthropic",
        "context_window": 200000,
        "max_output_tokens": 8192,
        "default_max_tokens": 8192,
        "supports_both_sampling": True,
        "requires_profile": False,
        "throughput_options": ["on-demand"],
        "supports_extended_context": True,
        "supports_thinking": False,
        "supports_adaptive_thinking": False,
        "thinking_max_budget": 0,
        "default_thinking_budget": 0,
        "supports_caching": True,
        "cache_min_tokens": 2048,
        "cache_ttl_options": ["5m"],
    },
    {
        "id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "base_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "name": "Claude 3.5 Sonnet v2",
        "description": "Solid performance, 200K ctx, 8K output",
        "provider": "anthropic",
        "context_window": 200000,
        "max_output_tokens": 8192,
        "default_max_tokens": 8192,
        "supports_both_sampling": True,
        "requires_profile": False,
        "throughput_options": ["on-demand"],
        "supports_extended_context": True,
        "supports_thinking": False,
        "supports_adaptive_thinking": False,
        "thinking_max_budget": 0,
        "default_thinking_budget": 0,
        "supports_caching": True,
        "cache_min_tokens": 1024,
        "cache_ttl_options": ["5m"],
    },
]


# Create global config instances
aws_config = AWSConfig()
model_config = ModelConfig()
app_config = AppConfig()


def get_model_by_id(model_id: str) -> Optional[Dict[str, Any]]:
    """Get model configuration by ID"""
    for model in AVAILABLE_MODELS:
        if model["id"] == model_id or model.get("base_id") == model_id:
            return model
    return None


def get_model_name(model_id: str) -> str:
    """Get the display name for a model ID"""
    model = get_model_by_id(model_id)
    return model["name"] if model else model_id


def get_model_config(model_id: str) -> Dict[str, Any]:
    """Get the full configuration for a model. For unknown model IDs returns a minimal
    fallback dict. Callers should use .get(key, sensible_default) for any key they need."""
    model = get_model_by_id(model_id)
    if model:
        return model
    # Sensible fallback for unknown Anthropic models
    return {
        "id": model_id,
        "base_id": model_id,
        "name": model_id,
        "provider": "anthropic",
        "context_window": 200000,
        "max_output_tokens": 128000,
        "default_max_tokens": 128000,
        "supports_both_sampling": False,
        "requires_profile": True,
        "throughput_options": ["cross-region"],
        "supports_extended_context": True,
        "supports_thinking": True,
        "supports_adaptive_thinking": False,
        "thinking_max_budget": 128000,
        "default_thinking_budget": 120000,
        "supports_caching": True,
        "cache_min_tokens": 1024,
        "cache_ttl_options": ["5m"],
    }


def get_context_window(model_id: str) -> int:
    return get_model_config(model_id).get("context_window", 200000)


def get_max_output_tokens(model_id: str) -> int:
    return get_model_config(model_id).get("max_output_tokens", 4096)


def get_default_max_tokens(model_id: str) -> int:
    return get_model_config(model_id).get("default_max_tokens", 4096)


def supports_both_sampling(model_id: str) -> bool:
    return get_model_config(model_id).get("supports_both_sampling", True)


def requires_inference_profile(model_id: str) -> bool:
    return get_model_config(model_id).get("requires_profile", False)


def get_throughput_options(model_id: str) -> List[str]:
    return get_model_config(model_id).get("throughput_options", ["on-demand"])


def supports_thinking(model_id: str) -> bool:
    """Check if model supports extended thinking"""
    return get_model_config(model_id).get("supports_thinking", False)


def get_thinking_max_budget(model_id: str) -> int:
    """Get maximum thinking budget for a model"""
    return get_model_config(model_id).get("thinking_max_budget", 0)


def get_default_thinking_budget(model_id: str) -> int:
    """Get default thinking budget for a model"""
    return get_model_config(model_id).get("default_thinking_budget", 0)


def get_provider(model_id: str) -> str:
    return get_model_config(model_id).get("provider", "anthropic")


def supports_adaptive_thinking(model_id: str) -> bool:
    """Check if model supports adaptive thinking (Opus 4.6+)"""
    return get_model_config(model_id).get("supports_adaptive_thinking", False)


def supports_caching(model_id: str) -> bool:
    """Check if model supports prompt caching"""
    return get_model_config(model_id).get("supports_caching", False)


def get_cache_min_tokens(model_id: str) -> int:
    """Get minimum tokens per cache checkpoint"""
    return get_model_config(model_id).get("cache_min_tokens", 1024)


def get_cache_ttl_options(model_id: str) -> List[str]:
    """Get supported cache TTL options"""
    return get_model_config(model_id).get("cache_ttl_options", ["5m"])


def get_credentials_info() -> str:
    if aws_config.has_profile():
        return f"Using AWS profile: {aws_config.profile_name}"
    elif aws_config.has_explicit_credentials():
        if aws_config.has_session_token():
            return "Using temporary credentials (with session token)"
        return "Using explicit credentials"
    return "Using default credential chain"