"""
Token counting, context window management, and cost tracking.

Follows the original Claude Code patterns:
- Token estimation via bytes (1 token ≈ 4 bytes for text, 2 bytes for JSON)
- Context window detection per model
- Cost calculation per model pricing
- Session-level accumulation
"""

import json
import os
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Context window sizes per model family
# ---------------------------------------------------------------------------

MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-opus-4-8":     200_000,
    "claude-opus-4-7":     200_000,
    "claude-opus-4-6":     200_000,
    "claude-sonnet-4-6":   200_000,
    "claude-opus-4-5":     200_000,
    "claude-sonnet-4-5":   200_000,
    "claude-haiku-4-5":    200_000,
    "claude-3-5-sonnet":   200_000,
    "claude-3-5-haiku":    200_000,
    "claude-3-opus":       200_000,
    "claude-3-sonnet":     200_000,
    "claude-3-haiku":      200_000,
    # --- Non-Anthropic providers (best-effort; override via env if needed) ---
    "gpt-5.5":             400_000,
    "qwen3.7-max":         262_144,
    "qwen3.7-plus":        262_144,
    "qwen3.5-plus":        131_072,
    "glm-5.2":             200_000,
    "glm-5.1":             200_000,
    "kimi-k2.6":           262_144,
    "deepseek-v4-pro":     131_072,
    "deepseek-v4-flash":   131_072,
}

MODEL_MAX_OUTPUT: dict[str, int] = {
    "claude-opus-4-8":     64_000,
    "claude-opus-4-7":     64_000,
    "claude-opus-4-6":     64_000,
    "claude-sonnet-4-6":   64_000,
    "claude-opus-4-5":     32_000,
    "claude-sonnet-4-5":   32_000,
    "claude-haiku-4-5":    32_000,
    "claude-3-5-sonnet":   8_192,
    "claude-3-5-haiku":    8_192,
    "claude-3-opus":       4_096,
    "claude-3-sonnet":     4_096,
    "claude-3-haiku":      4_096,
    # --- Non-Anthropic providers ---
    "gpt-5.5":             32_000,
    "qwen3.7-max":         16_384,
    "qwen3.7-plus":        16_384,
    "qwen3.5-plus":        8_192,
    "glm-5.2":             16_384,
    "glm-5.1":             16_384,
    "kimi-k2.6":           16_384,
    "deepseek-v4-pro":     8_192,
    "deepseek-v4-flash":   8_192,
}

# Pricing: (input_per_million, output_per_million) in USD
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-8":     (15.0, 75.0),
    "claude-opus-4-7":     (15.0, 75.0),
    "claude-opus-4-6":     (15.0, 75.0),
    "claude-sonnet-4-6":   (3.0, 15.0),
    "claude-opus-4-5":     (15.0, 75.0),
    "claude-sonnet-4-5":   (3.0, 15.0),
    "claude-haiku-4-5":    (0.80, 4.0),
    "claude-3-5-sonnet":   (3.0, 15.0),
    "claude-3-5-haiku":    (0.80, 4.0),
    "claude-3-opus":       (15.0, 75.0),
    "claude-3-sonnet":     (3.0, 15.0),
    "claude-3-haiku":      (0.25, 1.25),
    # --- Non-Anthropic providers (approximate; for cost display only) ---
    "gpt-5.5":             (5.0, 15.0),
    "qwen3.7-max":         (2.4, 9.6),
    "qwen3.7-plus":        (0.8, 2.0),
    "qwen3.5-plus":        (0.8, 2.0),
    "glm-5.2":             (0.6, 2.2),
    "glm-5.1":             (0.6, 2.2),
    "kimi-k2.6":           (0.6, 2.5),
    "deepseek-v4-pro":     (0.5, 1.5),
    "deepseek-v4-flash":   (0.15, 0.5),
}


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

def estimate_tokens(text: str, bytes_per_token: int = 4) -> int:
    """Estimate token count from text. 1 token ≈ 4 bytes for natural text."""
    return max(1, len(text.encode("utf-8")) // bytes_per_token)


def estimate_message_tokens(message: dict[str, Any]) -> int:
    """Estimate tokens for a single message (role + content)."""
    content = message.get("content", "")
    overhead = 4  # role/structure tokens

    if isinstance(content, str):
        return overhead + estimate_tokens(content)

    if isinstance(content, list):
        total = overhead
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    total += estimate_tokens(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    total += estimate_tokens(json.dumps(block.get("input", {})), bytes_per_token=2)
                    total += estimate_tokens(block.get("name", ""))
                elif block.get("type") == "tool_result":
                    result_content = block.get("content", "")
                    if isinstance(result_content, str):
                        total += estimate_tokens(result_content)
                    elif isinstance(result_content, list):
                        for sub in result_content:
                            if isinstance(sub, dict) and sub.get("type") == "text":
                                total += estimate_tokens(sub.get("text", ""))
                else:
                    total += estimate_tokens(json.dumps(block), bytes_per_token=2)
            elif isinstance(block, str):
                total += estimate_tokens(block)
        return total

    return overhead


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate total tokens for all messages."""
    return sum(estimate_message_tokens(m) for m in messages)


# ---------------------------------------------------------------------------
# Context window helpers
# ---------------------------------------------------------------------------

def _match_model(model: str) -> Optional[str]:
    """Find the best matching key in model dicts."""
    # Exact match
    if model in MODEL_CONTEXT_WINDOWS:
        return model
    # Prefix match (e.g., "claude-sonnet-4-6-20250514" → "claude-sonnet-4-6")
    for key in sorted(MODEL_CONTEXT_WINDOWS, key=len, reverse=True):
        if model.startswith(key):
            return key
    return None


def get_context_window(model: str) -> int:
    """Get context window size for a model."""
    # Environment override
    env_val = os.environ.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS")
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            pass

    key = _match_model(model)
    if key:
        return MODEL_CONTEXT_WINDOWS[key]
    return 200_000  # Default


def get_max_output_tokens(model: str) -> int:
    """Get max output tokens for a model."""
    key = _match_model(model)
    if key:
        return MODEL_MAX_OUTPUT[key]
    return 16_384


def get_effective_context_window(model: str) -> int:
    """
    Effective window after reserving space for output.
    = context_window - min(max_output, 20_000)
    """
    window = get_context_window(model)
    max_out = get_max_output_tokens(model)
    reserved = min(max_out, 20_000)
    return window - reserved


# ---------------------------------------------------------------------------
# Compaction thresholds
# ---------------------------------------------------------------------------

AUTOCOMPACT_BUFFER = 13_000  # Trigger compaction this many tokens before limit


def should_compact(messages: list[dict[str, Any]], model: str) -> bool:
    """Check if conversation should be compacted."""
    effective = get_effective_context_window(model)
    threshold = effective - AUTOCOMPACT_BUFFER
    current = estimate_messages_tokens(messages)
    return current >= threshold


def tokens_remaining(messages: list[dict[str, Any]], model: str) -> int:
    """How many tokens remain before compaction threshold."""
    effective = get_effective_context_window(model)
    threshold = effective - AUTOCOMPACT_BUFFER
    current = estimate_messages_tokens(messages)
    return threshold - current


# ---------------------------------------------------------------------------
# Cost calculation
# ---------------------------------------------------------------------------

class CostTracker:
    """Track token usage and cost across a session."""

    def __init__(self):
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cache_read_tokens = 0
        self.total_cache_creation_tokens = 0
        self.total_cost_usd = 0.0
        self.per_model: dict[str, dict[str, Any]] = {}

    def add_usage(self, model: str, input_tokens: int = 0, output_tokens: int = 0,
                  cache_read: int = 0, cache_creation: int = 0):
        """Record token usage from an API response."""
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_cache_read_tokens += cache_read
        self.total_cache_creation_tokens += cache_creation

        # Per-model tracking
        if model not in self.per_model:
            self.per_model[model] = {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read": 0,
                "cache_creation": 0,
                "cost_usd": 0.0,
                "api_calls": 0,
            }
        m = self.per_model[model]
        m["input_tokens"] += input_tokens
        m["output_tokens"] += output_tokens
        m["cache_read"] += cache_read
        m["cache_creation"] += cache_creation
        m["api_calls"] += 1

        # Cost
        cost = self._calculate_cost(model, input_tokens, output_tokens,
                                    cache_read, cache_creation)
        self.total_cost_usd += cost
        m["cost_usd"] += cost

    def _calculate_cost(self, model: str, input_tokens: int, output_tokens: int,
                        cache_read: int = 0, cache_creation: int = 0) -> float:
        """Calculate USD cost for a single API call.

        Cache reads cost 10% of the input price; cache writes cost 125%.
        """
        key = _match_model(model)
        if key and key in MODEL_PRICING:
            inp_price, out_price = MODEL_PRICING[key]
        else:
            # Default to sonnet pricing
            inp_price, out_price = 3.0, 15.0

        return (
            input_tokens * inp_price
            + output_tokens * out_price
            + cache_read * inp_price * 0.1
            + cache_creation * inp_price * 1.25
        ) / 1_000_000

    def format_summary(self) -> str:
        """Human-readable cost summary."""
        lines = [
            f"Total tokens: {self.total_input_tokens:,} in / {self.total_output_tokens:,} out",
            f"Total cost:   ${self.total_cost_usd:.4f}",
        ]
        if self.total_cache_read_tokens > 0:
            lines.append(f"Cache read:   {self.total_cache_read_tokens:,} tokens")
        if self.total_cache_creation_tokens > 0:
            lines.append(f"Cache write:  {self.total_cache_creation_tokens:,} tokens")
        if len(self.per_model) > 1:
            lines.append("Per model:")
            for model, data in self.per_model.items():
                lines.append(
                    f"  {model}: {data['input_tokens']:,} in / {data['output_tokens']:,} out "
                    f"(${data['cost_usd']:.4f}, {data['api_calls']} calls)"
                )
        return "\n".join(lines)
