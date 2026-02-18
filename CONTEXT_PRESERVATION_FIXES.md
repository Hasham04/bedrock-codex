# Context Preservation Fixes for Bedrock Codex

## Problem: Agent Loses Recent Conversational Context

The agent lost track that "it" referred to the `cr` command when the user said "can you try running it" after a detailed discussion about the command.

## Root Causes

1. **Aggressive Context Trimming** - Starts at 40% capacity, losing recent conversation
2. **Stream Failure Recovery** - Persistent tool_use/tool_result mismatch causes rollbacks
3. **Poor Semantic Continuity** - Summarization misses conversational context

## Proposed Solutions

### 1. Fix Context Trimming Thresholds

**Current (too aggressive):**
```python
tier1_limit = int(usable * 0.40)  # Start trimming at 40%
tier2_limit = int(usable * 0.55)  # Aggressive at 55%  
tier3_limit = int(usable * 0.70)  # Emergency at 70%
```

**Proposed (preserve recent context):**
```python
tier1_limit = int(usable * 0.65)  # Start trimming at 65%
tier2_limit = int(usable * 0.80)  # Aggressive at 80%
tier3_limit = int(usable * 0.90)  # Emergency at 90%
```

### 2. Improve Recent Context Preservation

**Add conversational context buffer:**
```python
def _trim_history(self) -> None:
    # Preserve more recent messages for conversational continuity
    safe_tail = min(12, len(self.history))  # Increased from 6
    
    # Don't compress the last few conversational turns
    recent_cutoff = max(0, len(self.history) - safe_tail)
```

**Preserve pronoun references:**
```python
def _preserve_conversational_context(self, messages: List[Dict]) -> str:
    """Extract recent conversational context that should be preserved."""
    context_items = []
    
    # Look for recent references, pronouns, "it", "that", etc.
    for msg in messages[-6:]:  # Last 6 messages
        content = self._extract_text_from_message(msg)
        if any(word in content.lower() for word in ['it', 'that', 'this', 'them', 'those']):
            context_items.append(f"Recent reference: {content[:200]}")
    
    return "\n".join(context_items)
```

### 3. Fix Persistent Stream Failures

**Improve history repair:**
```python
def _repair_history_aggressive(self) -> None:
    """More aggressive history repair for persistent failures."""
    # Find and remove ALL orphaned tool_use blocks
    # Add proper error handling for malformed messages
    # Clean up message structure completely
```

**Add failure pattern detection:**
```python
def _detect_persistent_failures(self, error_msg: str) -> bool:
    """Detect when the same error repeats and needs different handling."""
    if hasattr(self, '_last_stream_errors'):
        recent_errors = self._last_stream_errors[-3:]
        if len(recent_errors) >= 2 and all(error_msg in err for err in recent_errors):
            return True
    return False
```

### 4. Enhance Summarization for Conversational Context

**Update summary prompt:**
```python
system_prompt = (
    "COMPACTION CONTRACT: This summary must preserve conversational context.\n"
    "Include exactly:\n"
    "1. **Current Discussion**: What was just being discussed (commands, topics, decisions)\n"
    "2. **Recent References**: What 'it', 'that', 'this' refer to in recent messages\n"  
    "3. **Task**: What the user asked for (exact goal)\n"
    "4. **Files touched**: Paths read, edited, or created\n"
    "5. **Current state**: What is done, what remains\n"
    "6. **Next steps**: What should happen next\n"
    "CRITICAL: Preserve enough conversational context that pronouns and references make sense."
)
```

### 5. Add Context Loss Detection

**Detect when context might be lost:**
```python
def _detect_context_loss_risk(self, user_msg: str) -> bool:
    """Detect when a user message might reference lost context."""
    # Look for pronouns without clear antecedents
    pronouns = ['it', 'that', 'this', 'them', 'those', 'he', 'she', 'they']
    msg_lower = user_msg.lower()
    
    # If message is short and contains pronouns, might be referencing lost context
    if len(user_msg.split()) < 10 and any(p in msg_lower for p in pronouns):
        return True
    
    return False

async def _handle_potential_context_loss(self, user_msg: str):
    """Ask for clarification when context might be lost."""
    if self._detect_context_loss_risk(user_msg):
        await on_event(AgentEvent(
            type="context_clarification",
            content="I may have lost some conversational context. Could you clarify what you're referring to?"
        ))
```

## Implementation Priority

1. **CRITICAL**: Fix persistent stream failures - this is causing the immediate rollback loop
2. **HIGH**: Adjust context trimming thresholds to preserve more recent context  
3. **MEDIUM**: Enhance summarization to preserve conversational context
4. **LOW**: Add context loss detection and recovery

## Expected Impact

- **Immediate**: Stop the stream failure/rollback loop
- **Short-term**: Preserve recent conversational context through trimming
- **Long-term**: Maintain semantic continuity even under memory pressure