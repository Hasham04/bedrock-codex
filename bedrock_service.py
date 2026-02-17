"""
Amazon Bedrock service module.
Handles all interactions with AWS Bedrock API including extended thinking.
"""

import boto3
import json
import logging
from typing import Generator, List, Dict, Optional, Any
from botocore.exceptions import ClientError, NoCredentialsError, PartialCredentialsError
from dataclasses import dataclass, field
from config import (
    aws_config, 
    model_config, 
    get_credentials_info,
    get_model_config,
    get_context_window,
    get_max_output_tokens,
    supports_both_sampling,
    requires_inference_profile,
    supports_thinking,
    supports_adaptive_thinking,
    supports_caching,
    get_cache_ttl_options,
    get_thinking_max_budget,
    get_default_thinking_budget
)
import os
from dotenv import load_dotenv, set_key


logger = logging.getLogger(__name__)
env_path = '.env'

class BedrockError(Exception):
    """Custom exception for Bedrock service errors"""
    pass


@dataclass
class GenerationConfig:
    """Configuration for a single generation request"""
    max_tokens: int = 16000
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    stop_sequences: Optional[List[str]] = None
    
    # Throughput settings
    throughput_mode: str = "cross-region"
    
    # Extended thinking settings
    enable_thinking: bool = True
    thinking_budget: int = 10000
    # Adaptive thinking (Claude 4.6+). If None, use model/project default.
    use_adaptive_thinking: Optional[bool] = None
    # Effort hint for adaptive thinking: low|medium|high|max
    adaptive_thinking_effort: Optional[str] = None
    
    # Whether to stream thinking content
    stream_thinking: bool = True


@dataclass
class ThinkingBlock:
    """Represents a thinking block from the response"""
    thinking: str = ""
    thinking_signature: Optional[str] = None


@dataclass
class ToolUseBlock:
    """Represents a tool_use block from the response"""
    id: str = ""
    name: str = ""
    input: Dict = field(default_factory=dict)


@dataclass
class GenerationResult:
    """Result from a generation request"""
    content: str = ""
    thinking: Optional[ThinkingBlock] = None
    tool_uses: List[Any] = field(default_factory=list)
    content_blocks: List[Dict] = field(default_factory=list)
    stop_reason: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0


class BedrockService:
    """
    Service class for Amazon Bedrock interactions.
    Supports extended thinking capabilities.
    """
    
    def __init__(
        self,
        model_id: Optional[str] = None,
        region: Optional[str] = None
    ):
        self.model_id = model_id or model_config.model_id
        self.region = region or aws_config.region

        self.client = self._create_client()
        logger.info(f"BedrockService initialized with model: {self.model_id}")
    
    def _create_client(self) -> Any:
        """Create and configure the Bedrock runtime client"""
        load_dotenv(env_path, override=True)
        try:
            session_kwargs = {"region_name": self.region}
            
            if aws_config.has_profile():
                session_kwargs["profile_name"] = aws_config.profile_name
            elif aws_config.has_explicit_credentials():
                session_kwargs["aws_access_key_id"] = aws_config.access_key_id
                session_kwargs["aws_secret_access_key"] = aws_config.secret_access_key
                if aws_config.has_session_token():
                    session_kwargs["aws_session_token"] = aws_config.session_token
            
            session = boto3.Session(**session_kwargs)
            return session.client("bedrock-runtime")
            
        except NoCredentialsError:
            raise BedrockError("AWS credentials not configured.")
        except Exception as e:
            raise BedrockError(f"Failed to initialize Bedrock client: {e}")
    
    def refresh_credentials(
        self,
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
        session_token: Optional[str] = None
    ):
        """Refresh the client with new credentials"""
        try:
            session_kwargs = {"region_name": self.region}
            
            if access_key_id and secret_access_key:
                session_kwargs["aws_access_key_id"] = access_key_id
                session_kwargs["aws_secret_access_key"] = secret_access_key
                if session_token:
                    session_kwargs["aws_session_token"] = session_token
            
            session = boto3.Session(**session_kwargs)
            self.client = session.client("bedrock-runtime")
            logger.info("Credentials refreshed successfully")
            set_key(env_path, "AWS_ACCESS_KEY_ID", access_key_id)
            set_key(env_path, "AWS_SECRET_ACCESS_KEY", secret_access_key)
            set_key(env_path, "AWS_SESSION_TOKEN", session_token)
            load_dotenv(env_path, override=True)
        except Exception as e:
            raise BedrockError(f"Failed to refresh credentials: {e}")
    
    def _get_model_identifier(self, model_id: str, config: GenerationConfig) -> str:
        """Get the appropriate model identifier based on throughput mode"""
        if config.throughput_mode == "cross-region":
            if model_id.startswith(("us.", "eu.", "ap.")):
                return model_id
            elif requires_inference_profile(model_id):
                region_prefix = "us" if self.region.startswith("us-") else "eu" if self.region.startswith("eu-") else "us"
                return f"{region_prefix}.{model_id}"
        
        model_config_data = get_model_config(model_id)
        return model_config_data.get("base_id", model_id)
    
    def _get_provider_from_model(self, model_id: str) -> str:
        """Determine the provider from model ID"""
        mid = model_id
        if mid.startswith(("us.", "eu.", "ap.")):
            mid = mid.split(".", 1)[1]
        
        if mid.startswith("anthropic"):
            return "anthropic"
        elif mid.startswith("amazon"):
            return "amazon"
        elif mid.startswith("meta"):
            return "meta"
        elif mid.startswith("mistral"):
            return "mistral"
        return "anthropic"
    
    def _format_messages_anthropic(
        self,
        messages: List[Dict],
        system_prompt: Optional[str],
        model_id: str,
        config: GenerationConfig,
        tools: Optional[List[Dict]] = None
    ) -> Dict[str, Any]:
        """Format request body for Anthropic Claude models with thinking, tool_use, and prompt caching"""
        formatted_messages = []
        use_cache = supports_caching(model_id)

        def _ensure_non_empty_content(role: str, content: Any, index: int) -> Any:
            """API requires non-empty content for all messages except optional final assistant."""
            if isinstance(content, str):
                if (content or "").strip():
                    return content
                return "(no content)"
            if isinstance(content, list):
                if not content:
                    return [{"type": "text", "text": "(no content)"}]
                out = []
                for b in content:
                    if not isinstance(b, dict):
                        out.append(b)
                        continue
                    if b.get("type") == "text":
                        t = b.get("text") or ""
                        if t.strip():
                            out.append(b)
                        else:
                            out.append({**b, "text": "(no content)"})
                    else:
                        out.append(b)
                return out if out else [{"type": "text", "text": "(no content)"}]
            return content if content is not None else [{"type": "text", "text": "(no content)"}]

        for i, msg in enumerate(messages):
            if msg["role"] != "system":
                raw = msg.get("content")
                formatted_messages.append({
                    "role": msg["role"],
                    "content": _ensure_non_empty_content(msg["role"], raw, i)
                })
        
        # --- Conversation-level prompt caching ---
        # Add cache_control breakpoint on the last user message so all
        # prior messages are cached by the API between turns. This is the
        # single biggest latency/cost optimisation for long conversations.
        if use_cache and len(formatted_messages) >= 3:
            ttl_options = get_cache_ttl_options(model_id)
            msg_cache_ctrl: Dict[str, Any] = {"type": "ephemeral"}
            if "1h" in ttl_options:
                msg_cache_ctrl["ttl"] = "1h"
            
            # Find the second-to-last user message (the last one that won't change)
            # We skip the very last message because it's the new turn.
            target_idx = None
            for i in range(len(formatted_messages) - 2, -1, -1):
                if formatted_messages[i]["role"] == "user":
                    target_idx = i
                    break
            
            if target_idx is not None:
                content = formatted_messages[target_idx]["content"]
                # Content can be a string or a list of blocks
                if isinstance(content, str):
                    formatted_messages[target_idx]["content"] = [{
                        "type": "text",
                        "text": content,
                        "cache_control": msg_cache_ctrl,
                    }]
                elif isinstance(content, list):
                    # Add cache_control to the last block in the content
                    content_copy = [dict(b) if isinstance(b, dict) else b for b in content]
                    if content_copy:
                        last_block = content_copy[-1]
                        if isinstance(last_block, dict):
                            last_block["cache_control"] = msg_cache_ctrl
                    formatted_messages[target_idx]["content"] = content_copy
        
        max_output = get_max_output_tokens(model_id)
        effective_max_tokens = min(config.max_tokens, max_output)
        
        thinking_supported = supports_thinking(model_id)
        use_thinking = thinking_supported and config.enable_thinking
        use_adaptive = supports_adaptive_thinking(model_id) and use_thinking
        
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": effective_max_tokens,
            "messages": formatted_messages
        }
        
        # --- Thinking configuration ---
        if use_thinking:
            # Prefer adaptive thinking for Claude 4.6 when enabled in config.
            use_adaptive_cfg = config.use_adaptive_thinking
            if use_adaptive_cfg is None:
                use_adaptive_cfg = model_config.use_adaptive_thinking

            if use_adaptive and use_adaptive_cfg:
                body["thinking"] = {"type": "adaptive"}
                effort = (config.adaptive_thinking_effort or model_config.adaptive_thinking_effort or "").strip().lower()
                if effort in {"low", "medium", "high", "max"}:
                    # Anthropic-style effort hint for adaptive thinking.
                    # If your Bedrock region/model rejects this field, disable via env:
                    # USE_ADAPTIVE_THINKING=false
                    body["output_config"] = {"effort": effort}
                logger.info(
                    "Adaptive thinking enabled%s",
                    f" (effort={effort})" if effort else "",
                )
            else:
                max_thinking_budget = get_thinking_max_budget(model_id)
                thinking_budget = min(config.thinking_budget, max_thinking_budget)

                # thinking_budget must be strictly less than max_tokens, and we need
                # room for the actual response text (at least 4K tokens)
                max_allowed = effective_max_tokens - 4000
                if thinking_budget > max_allowed:
                    thinking_budget = max(max_allowed, 1000)

                body["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": thinking_budget
                }
                logger.info(f"Extended thinking enabled with budget: {thinking_budget} tokens")
        else:
            if config.temperature is not None:
                body["temperature"] = config.temperature
            elif config.top_p is not None:
                body["top_p"] = config.top_p
            else:
                body["temperature"] = 1.0
            
            if config.top_k is not None:
                body["top_k"] = config.top_k
        
        if config.stop_sequences:
            body["stop_sequences"] = config.stop_sequences
        
        # --- System prompt with prompt caching ---
        if system_prompt:
            if use_cache:
                # Determine TTL: use 1h if model supports it, else 5m (default)
                ttl_options = get_cache_ttl_options(model_id)
                cache_ctrl: Dict[str, Any] = {"type": "ephemeral"}
                if "1h" in ttl_options:
                    cache_ctrl["ttl"] = "1h"
                
                body["system"] = [
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": cache_ctrl,
                    }
                ]
            else:
                body["system"] = system_prompt
        
        # --- Tools with prompt caching on the last tool ---
        if tools:
            if use_cache and len(tools) > 0:
                # Deep copy the last tool and add cache_control
                cached_tools = [dict(t) for t in tools]
                ttl_options = get_cache_ttl_options(model_id)
                cache_ctrl_tools: Dict[str, Any] = {"type": "ephemeral"}
                if "1h" in ttl_options:
                    cache_ctrl_tools["ttl"] = "1h"
                cached_tools[-1] = {**cached_tools[-1], "cache_control": cache_ctrl_tools}
                body["tools"] = cached_tools
            else:
                body["tools"] = tools
        
        logger.debug(f"Request body keys: {list(body.keys())}, caching: {use_cache}, adaptive: {use_adaptive}")
        
        return body
    
    def _format_request_body(
        self,
        messages: List[Dict],
        system_prompt: Optional[str],
        model_id: str,
        config: GenerationConfig,
        tools: Optional[List[Dict]] = None
    ) -> Dict[str, Any]:
        """Format the request body (Anthropic-only since all models are Claude)"""
        return self._format_messages_anthropic(
            messages, system_prompt, model_id, config, tools=tools
        )
    
    def _parse_response(self, response_body: Dict, model_id: str) -> GenerationResult:
        """Parse the Anthropic response body, extracting thinking, content, and tool_use blocks"""
        result = GenerationResult()
        
        try:
            content_blocks = response_body.get("content", [])
            
            for block in content_blocks:
                block_type = block.get("type", "")
                
                if block_type == "thinking":
                    result.thinking = ThinkingBlock(
                        thinking=block.get("thinking", ""),
                        thinking_signature=block.get("thinking_signature")
                    )
                elif block_type == "text":
                    result.content += block.get("text", "")
                    result.content_blocks.append(block)
                elif block_type == "tool_use":
                    tool_block = ToolUseBlock(
                        id=block.get("id", ""),
                        name=block.get("name", ""),
                        input=block.get("input", {})
                    )
                    result.tool_uses.append(tool_block)
                    result.content_blocks.append(block)
            
            usage = response_body.get("usage", {})
            result.input_tokens = usage.get("input_tokens", 0)
            result.output_tokens = usage.get("output_tokens", 0)
            result.stop_reason = response_body.get("stop_reason")
                    
        except (KeyError, IndexError) as e:
            logger.error(f"Error parsing response: {e}")
            raise BedrockError(f"Failed to parse model response: {e}")
        
        return result
    
    def generate_response(
        self,
        messages: List[Dict],
        system_prompt: Optional[str] = None,
        model_id: Optional[str] = None,
        config: Optional[GenerationConfig] = None,
        tools: Optional[List[Dict]] = None
    ) -> GenerationResult:
        """
        Generate a response using Amazon Bedrock.
        Returns a GenerationResult with content, optional thinking, and optional tool_use blocks.
        """
        current_model = model_id or self.model_id
        gen_config = config or GenerationConfig()
        
        try:
            model_identifier = self._get_model_identifier(current_model, gen_config)
            request_body = self._format_request_body(
                messages, system_prompt, current_model, gen_config, tools=tools
            )
            
            logger.info(f"Invoking model: {model_identifier}")
            
            response = self.client.invoke_model(
                modelId=model_identifier,
                body=json.dumps(request_body),
                contentType="application/json",
                accept="application/json"
            )
            
            response_body = json.loads(response["body"].read())
            return self._parse_response(response_body, current_model)
            
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', 'Unknown')
            error_message = e.response.get('Error', {}).get('Message', str(e))
            logger.error(f"Bedrock API error: {error_code} - {error_message}")
            
            if error_code in ['ExpiredTokenException', 'InvalidSignatureException']:
                raise BedrockError("AWS credentials expired. Please refresh.")
            
            if "thinking" in error_message.lower():
                raise BedrockError(
                    f"Thinking configuration error: {error_message}. "
                    f"Try adjusting thinking budget or disabling thinking."
                )
            
            raise BedrockError(f"Bedrock API error: {error_message}")
    
    def generate_response_stream(
        self,
        messages: List[Dict],
        system_prompt: Optional[str] = None,
        model_id: Optional[str] = None,
        config: Optional[GenerationConfig] = None,
        tools: Optional[List[Dict]] = None
    ) -> Generator[Dict[str, Any], None, None]:
        """
        Generate a streaming response using Amazon Bedrock.
        Yields dictionaries with 'type' and 'content'.
        Types: thinking_start, thinking, thinking_end, text_start, text, text_end,
               tool_use_start, tool_use_delta, tool_use_end, message_end
        """
        current_model = model_id or self.model_id
        gen_config = config or GenerationConfig()
        
        try:
            model_identifier = self._get_model_identifier(current_model, gen_config)
            request_body = self._format_request_body(
                messages, system_prompt, current_model, gen_config, tools=tools
            )
            
            logger.info(f"Streaming from model: {model_identifier}")
            
            response = self.client.invoke_model_with_response_stream(
                modelId=model_identifier,
                body=json.dumps(request_body),
                contentType="application/json",
                accept="application/json"
            )
            
            current_block_type = "text"
            current_thinking_signature = None
            
            for event in response["body"]:
                chunk = json.loads(event["chunk"]["bytes"])
                event_type = chunk.get("type", "")
                
                if event_type == "content_block_start":
                    block = chunk.get("content_block", {})
                    current_block_type = block.get("type", "text")
                    
                    if current_block_type == "thinking":
                        yield {"type": "thinking_start", "content": ""}
                    elif current_block_type == "text":
                        yield {"type": "text_start", "content": ""}
                    elif current_block_type == "tool_use":
                        yield {
                            "type": "tool_use_start",
                            "content": "",
                            "data": {
                                "id": block.get("id", ""),
                                "name": block.get("name", ""),
                            }
                        }
                
                elif event_type == "content_block_delta":
                    delta = chunk.get("delta", {})
                    delta_type = delta.get("type", "")
                    
                    if delta_type == "thinking_delta":
                        thinking_text = delta.get("thinking", "")
                        if thinking_text:
                            yield {"type": "thinking", "content": thinking_text}
                    elif delta_type == "signature_delta":
                        # Accumulate thinking signature for continuity
                        sig = delta.get("signature", "")
                        if sig:
                            current_thinking_signature = (current_thinking_signature or "") + sig
                    elif delta_type == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            yield {"type": "text", "content": text}
                    elif delta_type == "input_json_delta":
                        partial = delta.get("partial_json", "")
                        if partial:
                            yield {"type": "tool_use_delta", "content": partial}
                
                elif event_type == "content_block_stop":
                    if current_block_type == "thinking":
                        yield {
                            "type": "thinking_end",
                            "content": "",
                            "signature": current_thinking_signature,
                        }
                        current_thinking_signature = None
                    elif current_block_type == "text":
                        yield {"type": "text_end", "content": ""}
                    elif current_block_type == "tool_use":
                        yield {"type": "tool_use_end", "content": ""}
                
                elif event_type == "message_start":
                    # Extract input token usage from message_start (includes cache metrics)
                    msg_usage = chunk.get("message", {}).get("usage", {})
                    if msg_usage:
                        yield {
                            "type": "usage_start",
                            "content": "",
                            "usage": msg_usage,
                        }
                
                elif event_type == "message_delta":
                    usage = chunk.get("usage", {})
                    yield {
                        "type": "message_end",
                        "content": "",
                        "usage": usage,
                        "stop_reason": chunk.get("delta", {}).get("stop_reason")
                    }
                            
        except ClientError as e:
            error_message = e.response.get('Error', {}).get('Message', str(e))
            logger.error(f"Bedrock streaming error: {error_message}")
            raise BedrockError(f"Streaming error: {error_message}")
    
    def generate_title(self, first_message: str) -> str:
        """Generate a conversation title"""
        messages = [{
            "role": "user",
            "content": f"Generate a very short title (max 6 words) for this conversation. Return ONLY the title:\n\n{first_message[:500]}"
        }]
        
        title_config = GenerationConfig(
            max_tokens=50,
            temperature=0.7,
            enable_thinking=False,
            throughput_mode="cross-region"
        )
        
        try:
            result = self.generate_response(
                messages,
                model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
                config=title_config
            )
            title = result.content.strip().strip('"\'')
            words = title.split()
            return ' '.join(words[:6]) if len(words) > 6 else title
        except Exception as e:
            logger.warning(f"Failed to generate title: {e}")
            return first_message[:40] + "..." if len(first_message) > 40 else first_message
    
    def embed_text(self, text: str, input_type: str = "search_document") -> List[float]:
        """Embed a single text using the configured embedding model (Cohere Embed v3).
        Used for codebase indexing and semantic retrieval."""
        return self.embed_texts([text], input_type=input_type)[0]

    def embed_texts(
        self,
        texts: List[str],
        input_type: str = "search_document",
        model_id: Optional[str] = None,
    ) -> List[List[float]]:
        """Embed texts using Bedrock Cohere Embed. Batches to stay under 2048 chars per call.
        input_type: 'search_document' for corpus, 'search_query' for queries."""
        from config import app_config
        embed_model = model_id or getattr(app_config, "embedding_model_id", "cohere.embed-english-v3")
        # Cohere limit: 2048 chars total per request, 96 texts; ~512 tokens per text recommended
        batch_size = 8
        char_limit = 1800
        all_embeddings: List[List[float]] = []
        i = 0
        while i < len(texts):
            batch = []
            batch_chars = 0
            while i < len(texts) and len(batch) < batch_size and batch_chars + len(texts[i]) <= char_limit:
                t = texts[i][:1500]
                batch.append(t)
                batch_chars += len(t)
                i += 1
            if not batch:
                t = texts[i][:1500]
                batch.append(t)
                i += 1
            body = json.dumps({"texts": batch, "input_type": input_type})
            try:
                response = self.client.invoke_model(
                    modelId=embed_model,
                    body=body,
                    contentType="application/json",
                    accept="application/json",
                )
                response_body = json.loads(response["body"].read())
                embeddings = response_body.get("embeddings")
                if isinstance(embeddings, list) and embeddings and isinstance(embeddings[0], list):
                    all_embeddings.extend(embeddings)
                elif isinstance(embeddings, dict) and "float" in embeddings:
                    all_embeddings.extend(embeddings["float"])
                else:
                    all_embeddings.extend([[]] * len(batch))
            except ClientError as e:
                logger.warning(f"Embed API error: {e}")
                all_embeddings.extend([[0.0] * 1024 for _ in batch])
        return all_embeddings

    def test_connection(self) -> tuple:
        """Test the Bedrock connection"""
        try:
            test_config = GenerationConfig(
                max_tokens=10,
                temperature=1.0,
                enable_thinking=False,
                throughput_mode="cross-region"
            )
            self.generate_response(
                [{"role": "user", "content": "Hi"}],
                model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
                config=test_config
            )
            return True, "Connection successful"
        except Exception as e:
            return False, str(e)