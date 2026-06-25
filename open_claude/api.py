"""Open Claude - Anthropic API client with streaming support and prompt caching."""

import json
from typing import Any, Generator, Optional

import anthropic

from . import openai_compat
from .config import (
    get_api_key,
    get_max_tokens,
    get_model,
    get_model_provider,
)
from .tools import TOOL_SCHEMAS


def _anthropic_client() -> anthropic.Anthropic:
    """Build the Anthropic client, raising a clear error if no key is set."""
    api_key = get_api_key()
    if not api_key:
        raise ValueError(
            "No Anthropic API key found. Set ANTHROPIC_API_KEY environment variable "
            "or add 'api_key' to ~/.claude/config.json"
        )
    return anthropic.Anthropic(api_key=api_key)


def create_client():
    """Create the Anthropic client if a key exists, else None.

    Returning None (instead of raising) lets a session start when the user only
    has a non-Anthropic provider key; the Anthropic client is built on demand.
    """
    try:
        return _anthropic_client()
    except ValueError:
        return None


def get_tool_schemas() -> list[dict[str, Any]]:
    """Get the default tool schemas in Anthropic API format."""
    return TOOL_SCHEMAS


# ---------------------------------------------------------------------------
# Prompt caching
#
# We place up to 3 cache breakpoints (the API allows 4):
#   1. System prompt (stable across the session)
#   2. Tool definitions (stable across the session)
#   3. Last content block of the last message (moving breakpoint — caches the
#      whole conversation prefix between agentic iterations)
# ---------------------------------------------------------------------------

_CACHE = {"type": "ephemeral"}


def _cached_system(system_prompt: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": system_prompt, "cache_control": _CACHE}]


def _cached_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not tools:
        return tools
    tools = [dict(t) for t in tools]
    tools[-1] = {**tools[-1], "cache_control": _CACHE}
    return tools


def _cached_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shallow-copy messages, adding cache_control to the last cacheable block."""
    if not messages:
        return messages

    result = list(messages)
    last = dict(result[-1])
    content = last.get("content")

    if isinstance(content, str):
        last["content"] = [{"type": "text", "text": content, "cache_control": _CACHE}]
        result[-1] = last
    elif isinstance(content, list) and content:
        blocks = list(content)
        final = blocks[-1]
        if isinstance(final, dict) and final.get("type") in ("text", "tool_result"):
            blocks[-1] = {**final, "cache_control": _CACHE}
            last["content"] = blocks
            result[-1] = last

    return result


def stream_message(
    client: anthropic.Anthropic,
    messages: list[dict[str, Any]],
    system_prompt: str,
    model: Optional[str] = None,
    max_tokens: Optional[int] = None,
    tools: Optional[list[dict[str, Any]]] = None,
    temperature: Optional[float] = None,
    thinking_budget: Optional[int] = None,
) -> Generator[dict[str, Any], None, None]:
    """
    Stream a message from the API, yielding events as they arrive.

    Yields dicts with keys:
        - {"type": "text_delta", "text": "..."}
        - {"type": "thinking_delta", "text": "..."}
        - {"type": "tool_use_start", "id": "...", "name": "..."}
        - {"type": "tool_input_delta", "partial_json": "..."}
        - {"type": "tool_use_end", "id": "...", "name": "...", "input": {...}}
        - {"type": "message_end", "stop_reason": "...", "usage": {...}}
        - {"type": "error", "error": "..."}
    """
    model = model or get_model()
    max_tokens = max_tokens or get_max_tokens()
    tools = tools if tools is not None else get_tool_schemas()

    # Route non-Anthropic models through the OpenAI-compatible adapter.
    provider = get_model_provider(model)
    if provider != "anthropic":
        yield from openai_compat.stream(
            provider, model, messages, system_prompt, tools, max_tokens, temperature,
        )
        return

    client = client or _anthropic_client()

    # Optional sampling controls (extended thinking forces temperature=1 and
    # requires budget_tokens < max_tokens).
    extra_kwargs: dict[str, Any] = {}
    if thinking_budget and thinking_budget > 0:
        budget = min(thinking_budget, max(1024, max_tokens - 1))
        extra_kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
    elif temperature is not None:
        extra_kwargs["temperature"] = temperature

    # Only send the tools parameter when there actually are tools — a pure-chat
    # session (e.g. the web surface) passes an empty list and must not get tools.
    if tools:
        extra_kwargs["tools"] = _cached_tools(tools)

    # Usage captured from message_start (input + cache) and message_delta (output)
    usage_acc = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }

    try:
        with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=_cached_system(system_prompt),
            messages=_cached_messages(messages),
            **extra_kwargs,
        ) as stream:
            # Track current tool use block
            current_tool: Optional[dict[str, Any]] = None
            current_tool_json = ""

            for event in stream:
                if event.type == "message_start":
                    u = event.message.usage
                    usage_acc["input_tokens"] = getattr(u, "input_tokens", 0) or 0
                    usage_acc["cache_read_input_tokens"] = getattr(u, "cache_read_input_tokens", 0) or 0
                    usage_acc["cache_creation_input_tokens"] = getattr(u, "cache_creation_input_tokens", 0) or 0

                elif event.type == "content_block_start":
                    block = event.content_block
                    if block.type == "text":
                        pass  # text deltas come via content_block_delta
                    elif block.type == "thinking":
                        pass
                    elif block.type == "tool_use":
                        current_tool = {"id": block.id, "name": block.name}
                        current_tool_json = ""
                        yield {"type": "tool_use_start", "id": block.id, "name": block.name}

                elif event.type == "content_block_delta":
                    delta = event.delta
                    if delta.type == "text_delta":
                        yield {"type": "text_delta", "text": delta.text}
                    elif delta.type == "thinking_delta":
                        yield {"type": "thinking_delta", "text": delta.thinking}
                    elif delta.type == "input_json_delta":
                        current_tool_json += delta.partial_json
                        yield {"type": "tool_input_delta", "partial_json": delta.partial_json}

                elif event.type == "content_block_stop":
                    if current_tool is not None:
                        try:
                            tool_input = json.loads(current_tool_json) if current_tool_json else {}
                        except json.JSONDecodeError:
                            tool_input = {}
                        yield {
                            "type": "tool_use_end",
                            "id": current_tool["id"],
                            "name": current_tool["name"],
                            "input": tool_input,
                        }
                        current_tool = None
                        current_tool_json = ""

                elif event.type == "message_delta":
                    if event.usage:
                        usage_acc["output_tokens"] = event.usage.output_tokens or 0
                    yield {
                        "type": "message_end",
                        "stop_reason": event.delta.stop_reason,
                        "usage": dict(usage_acc),
                    }

    except anthropic.APIError as e:
        yield {"type": "error", "error": f"API Error: {e.message}"}
    except Exception as e:
        yield {"type": "error", "error": str(e)}


def send_message(
    client: anthropic.Anthropic,
    messages: list[dict[str, Any]],
    system_prompt: str,
    model: Optional[str] = None,
    max_tokens: Optional[int] = None,
    tools: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Non-streaming message send. Returns full response."""
    model = model or get_model()
    max_tokens = max_tokens or get_max_tokens()
    tools = tools if tools is not None else get_tool_schemas()

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=_cached_system(system_prompt),
        messages=_cached_messages(messages),
        tools=_cached_tools(tools),
    )
    usage = response.usage
    return {
        "content": response.content,
        "stop_reason": response.stop_reason,
        "usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
            "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        },
    }


def complete(
    client,
    messages: list[dict[str, Any]],
    system_prompt: str,
    model: Optional[str] = None,
    tools: Optional[list[dict[str, Any]]] = None,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
) -> dict[str, Any]:
    """Provider-agnostic, non-streaming completion.

    Returns {"content": [normalized blocks], "stop_reason", "usage"} where each
    block is a plain dict: {"type": "text", "text": ...} or
    {"type": "tool_use", "id", "name", "input"}. Works for every provider.
    """
    model = model or get_model()
    max_tokens = max_tokens or get_max_tokens()
    provider = get_model_provider(model)

    if provider != "anthropic":
        return openai_compat.send(
            provider, model, messages, system_prompt, tools, max_tokens, temperature,
        )

    client = client or _anthropic_client()
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = tools
    if temperature is not None:
        kwargs["temperature"] = temperature

    response = client.messages.create(**kwargs)

    content: list[dict[str, Any]] = []
    for block in response.content:
        if block.type == "text":
            content.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            content.append({"type": "tool_use", "id": block.id,
                            "name": block.name, "input": block.input})
    u = response.usage
    return {
        "content": content,
        "stop_reason": response.stop_reason,
        "usage": {
            "input_tokens": getattr(u, "input_tokens", 0) or 0,
            "output_tokens": getattr(u, "output_tokens", 0) or 0,
            "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
        },
    }
