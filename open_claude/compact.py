"""
Conversation compaction (summarization).

When the conversation approaches the context window limit, automatically
summarize older messages to free space, following the original Claude Code pattern:

1. Detect threshold breach
2. Call the API to generate a summary of older messages
3. Replace old messages with a compact summary boundary
4. Preserve recent messages for continuity
"""

import json
from typing import Any, Optional

import anthropic

from .tokens import (
    estimate_messages_tokens,
    get_effective_context_window,
    AUTOCOMPACT_BUFFER,
)

# How many recent message pairs to preserve (not summarized)
PRESERVE_RECENT_PAIRS = 4  # ~4 user/assistant exchanges

# Max output tokens for the summary generation call
SUMMARY_MAX_TOKENS = 8_000

# Summary generation system prompt
SUMMARY_SYSTEM_PROMPT = """You are a conversation summarizer. Your task is to create a concise but thorough summary of the conversation so far.

Rules:
- Capture all important decisions, code changes, file paths, and technical details
- Note any tools that were used and their results
- Preserve specific file paths, function names, variable names, and code snippets that were discussed
- Include the current state of any ongoing task
- Keep the summary factual and structured
- Use bullet points for clarity
- Do NOT include greetings or meta-commentary
- Be thorough but concise - aim for ~20% of the original conversation length"""


def needs_compaction(messages: list[dict[str, Any]], model: str,
                     threshold_fraction: Optional[float] = None) -> bool:
    """Check if the conversation needs compaction.

    If `threshold_fraction` (0-1) is given, compact once usage exceeds that
    fraction of the context window; otherwise use the default buffer.
    """
    effective = get_effective_context_window(model)
    if threshold_fraction is not None:
        threshold = int(effective * threshold_fraction)
    else:
        threshold = effective - AUTOCOMPACT_BUFFER
    current = estimate_messages_tokens(messages)
    return current >= threshold


def compact_conversation(
    client: anthropic.Anthropic,
    messages: list[dict[str, Any]],
    model: str,
    system_prompt: str = "",
) -> list[dict[str, Any]]:
    """
    Compact the conversation by summarizing older messages.

    Returns a new messages list with old messages replaced by a summary.
    Preserves the most recent exchanges for continuity.
    """
    if len(messages) < 4:
        # Too few messages to compact
        return messages

    # Split into old (to summarize) and recent (to keep)
    # Keep at least PRESERVE_RECENT_PAIRS * 2 messages
    keep_count = min(PRESERVE_RECENT_PAIRS * 2, len(messages) // 2)
    keep_count = max(keep_count, 2)  # Always keep at least 2

    # Ensure we split at a clean boundary (after an assistant message)
    split_idx = len(messages) - keep_count
    while split_idx > 0 and messages[split_idx - 1].get("role") != "assistant":
        split_idx -= 1
    if split_idx <= 0:
        split_idx = max(1, len(messages) - keep_count)

    old_messages = messages[:split_idx]
    recent_messages = messages[split_idx:]

    # Generate summary of old messages
    summary = _generate_summary(client, old_messages, model, system_prompt)

    if not summary:
        # Summary generation failed; return messages unchanged
        return messages

    # Build new message list with summary
    compacted: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                "[CONVERSATION SUMMARY - Previous messages have been summarized "
                "to save context space]\n\n" + summary
            ),
        },
        {
            "role": "assistant",
            "content": (
                "Understood. I have the context from the conversation summary above. "
                "I'll continue from where we left off."
            ),
        },
    ]
    compacted.extend(recent_messages)

    return compacted


def _generate_summary(
    client: anthropic.Anthropic,
    messages: list[dict[str, Any]],
    model: str,
    system_prompt: str,
) -> Optional[str]:
    """Call the API to generate a conversation summary."""

    # Build a representation of the conversation for summarization
    conversation_text = _messages_to_text(messages)

    # Truncate if the conversation itself is too large for a summary call
    max_chars = 300_000  # ~75k tokens
    if len(conversation_text) > max_chars:
        conversation_text = conversation_text[:max_chars] + "\n\n... [older messages truncated]"

    summary_messages = [
        {
            "role": "user",
            "content": (
                "Please summarize the following conversation between a user and an AI assistant. "
                "The summary will replace these messages in the conversation context.\n\n"
                "--- CONVERSATION START ---\n"
                + conversation_text
                + "\n--- CONVERSATION END ---\n\n"
                "Provide a structured summary capturing all key details."
            ),
        },
    ]

    try:
        # Provider-agnostic completion: works for Anthropic and OpenAI-compatible
        # providers (Qwen/GLM/Kimi/DeepSeek/OpenAI) alike.
        from .api import complete

        result = complete(
            client,
            summary_messages,
            SUMMARY_SYSTEM_PROMPT,
            model=model,
            max_tokens=SUMMARY_MAX_TOKENS,
        )

        # Extract text from the normalized content blocks
        for block in result.get("content", []):
            if block.get("type") == "text" and block.get("text"):
                return block["text"]

        return None
    except Exception:
        return None


def _messages_to_text(messages: list[dict[str, Any]]) -> str:
    """Convert message list to readable text for summarization."""
    parts = []

    for msg in messages:
        role = msg.get("role", "unknown").upper()
        content = msg.get("content", "")

        if isinstance(content, str):
            parts.append(f"[{role}]: {content}")
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    btype = block.get("type", "")
                    if btype == "text":
                        parts.append(f"[{role}]: {block.get('text', '')}")
                    elif btype == "tool_use":
                        name = block.get("name", "?")
                        inp = json.dumps(block.get("input", {}), ensure_ascii=False)
                        if len(inp) > 500:
                            inp = inp[:500] + "..."
                        parts.append(f"[{role} → Tool:{name}]: {inp}")
                    elif btype == "tool_result":
                        result = block.get("content", "")
                        if isinstance(result, str) and len(result) > 500:
                            result = result[:500] + "..."
                        parts.append(f"[TOOL_RESULT]: {result}")
                elif isinstance(block, str):
                    parts.append(f"[{role}]: {block}")

    return "\n\n".join(parts)
