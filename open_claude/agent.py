"""
Agent tool — spawns isolated sub-conversations (sub-agents).

Supports named agent types (like Claude Code's subagent_type):
- Built-in: "general-purpose" (all base tools), "explore" (read-only search)
- Custom:   ~/.claude/agents/*.md and .claude/agents/*.md with frontmatter:

    ---
    name: code-reviewer
    description: Reviews code for bugs and style issues
    tools: Read, Glob, Grep
    model: claude-haiku-4-5
    ---
    You are a code reviewer. <system prompt body...>

Each sub-agent gets its own message history, a filtered tool set, and a
focused system prompt. Runs a full tool-use loop until done.
"""

import os
from pathlib import Path
from typing import Any, Optional

import anthropic

from .config import get_model

# Max iterations for sub-agent tool loops
MAX_AGENT_ITERATIONS = 20

# Max output tokens for sub-agent responses
AGENT_MAX_TOKENS = 16_000

BASE_TOOL_NAMES = ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]
READONLY_TOOL_NAMES = ["Read", "Glob", "Grep"]


# ---------------------------------------------------------------------------
# Agent types
# ---------------------------------------------------------------------------

class AgentType:
    """One subagent type definition."""

    def __init__(self, name: str, description: str, tools: list[str],
                 system_prompt: str = "", model: Optional[str] = None,
                 source: str = "bundled"):
        self.name = name
        self.description = description
        self.tools = tools
        self.system_prompt = system_prompt
        self.model = model
        self.source = source


def _builtin_agent_types() -> dict[str, AgentType]:
    return {
        "general-purpose": AgentType(
            name="general-purpose",
            description="General-purpose agent for researching questions, searching code, and multi-step tasks.",
            tools=list(BASE_TOOL_NAMES),
        ),
        "explore": AgentType(
            name="explore",
            description="Read-only search agent for fan-out exploration of the codebase. Cannot modify files or run commands.",
            tools=list(READONLY_TOOL_NAMES),
        ),
    }


def load_agent_types(cwd: str) -> dict[str, AgentType]:
    """Load built-in + custom agent types (~/.claude/agents, .claude/agents)."""
    from .skills.frontmatter import parse_frontmatter, normalize_metadata

    types = _builtin_agent_types()

    dirs = [
        (os.path.join(Path.home(), ".claude", "agents"), "user"),
        (os.path.join(cwd, ".claude", "agents"), "project"),
    ]
    for directory, source in dirs:
        if not os.path.isdir(directory):
            continue
        try:
            entries = sorted(os.listdir(directory))
        except OSError:
            continue
        for entry in entries:
            if not entry.endswith(".md"):
                continue
            path = os.path.join(directory, entry)
            try:
                content = Path(path).read_text(encoding="utf-8")
            except OSError:
                continue
            raw_meta, body = parse_frontmatter(content)
            meta = normalize_metadata(raw_meta)

            name = meta.get("name") or Path(entry).stem
            tools_str = meta.get("tools") or meta.get("allowed_tools") or ""
            if isinstance(tools_str, str) and tools_str.strip():
                tools = [t.strip() for t in tools_str.split(",") if t.strip()]
                # Only base tools are available to sub-agents
                tools = [t for t in tools if t in BASE_TOOL_NAMES] or list(BASE_TOOL_NAMES)
            else:
                tools = list(BASE_TOOL_NAMES)

            types[name] = AgentType(
                name=name,
                description=meta.get("description", ""),
                tools=tools,
                system_prompt=body,
                model=meta.get("model"),
                source=source,
            )

    return types


def build_agent_schema(agent_types: dict[str, AgentType]) -> dict[str, Any]:
    """Build the Agent tool schema with the available types listed."""
    type_lines = "\n".join(
        f"- {t.name}: {t.description}" for t in agent_types.values()
    )
    return {
        "name": "Agent",
        "description": (
            "Launch a sub-agent to handle a complex, multi-step task autonomously. "
            "The agent runs in an isolated conversation with its own context. "
            "Provide a clear, complete task description — the agent has no prior context.\n\n"
            "Available agent types (subagent_type):\n" + type_lines
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Short (3-5 word) summary of what the agent will do.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Full task description for the agent. Be specific and complete.",
                },
                "subagent_type": {
                    "type": "string",
                    "description": "Agent type to use. Defaults to 'general-purpose'.",
                },
                "model": {
                    "type": "string",
                    "description": "Optional model override (e.g. 'claude-haiku-4-5').",
                },
            },
            "required": ["description", "prompt"],
        },
    }


# Static schema kept for backwards compatibility (built-in types only)
AGENT_SCHEMA = build_agent_schema(_builtin_agent_types())


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _build_agent_system_prompt(cwd: str, agent_type: AgentType) -> str:
    """Build the system prompt for a sub-agent of the given type."""
    base = agent_type.system_prompt.strip() if agent_type.system_prompt else (
        "You are a sub-agent assistant. Complete the given task and report your findings."
    )
    tool_list = ", ".join(agent_type.tools)
    return f"""{base}

You have access to tools: {tool_list}.
- Use Read instead of cat, Edit instead of sed, Glob instead of find, Grep instead of grep.
- Working directory: {cwd}
- Be concise. Focus on completing the task.
- When done, provide a clear summary of what you found or accomplished."""


def execute_agent(params: dict[str, Any], cwd: str, client: anthropic.Anthropic,
                  agent_types: Optional[dict[str, AgentType]] = None) -> str:
    """
    Run a sub-agent synchronously.

    Creates an isolated conversation, runs tool loops, and returns the final text.
    """
    prompt = params["prompt"]
    types = agent_types or _builtin_agent_types()

    type_name = params.get("subagent_type", "general-purpose")
    agent_type = types.get(type_name)
    if agent_type is None:
        agent_type = types.get("general-purpose") or next(iter(types.values()))

    model = params.get("model") or agent_type.model or get_model()
    system_prompt = _build_agent_system_prompt(cwd, agent_type)

    # Lazy import to avoid circular dependency
    from .tools import get_base_tool_schemas, execute_tool

    # Sub-agent gets the type's tool subset (no Agent/Task to prevent nesting)
    allowed = set(agent_type.tools)
    tools = [t for t in get_base_tool_schemas() if t["name"] in allowed]

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": prompt},
    ]

    final_text = ""

    # Provider-agnostic completion (Anthropic + OpenAI-compatible providers).
    from .api import complete

    for iteration in range(MAX_AGENT_ITERATIONS):
        try:
            response = complete(
                client,
                messages,
                system_prompt,
                model=model,
                tools=tools,
                max_tokens=AGENT_MAX_TOKENS,
            )
        except Exception as e:
            return f"Agent error: {e}"

        # Process the normalized response content blocks (plain dicts)
        assistant_content: list[dict[str, Any]] = []
        tool_uses: list[dict[str, Any]] = []
        text_parts: list[str] = []

        for block in response.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
                assistant_content.append({"type": "text", "text": block.get("text", "")})
            elif block.get("type") == "tool_use":
                tool_use = {
                    "type": "tool_use",
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "input": block.get("input", {}),
                }
                assistant_content.append(tool_use)
                tool_uses.append(tool_use)

        messages.append({"role": "assistant", "content": assistant_content})

        if response.get("stop_reason") != "tool_use" or not tool_uses:
            # Agent is done
            final_text = "\n".join(text_parts)
            break

        # Execute tools and add results
        tool_results: list[dict[str, Any]] = []
        for tu in tool_uses:
            if tu["name"] not in allowed:
                result = f"Error: tool {tu['name']} is not available to this agent type"
            else:
                try:
                    result = execute_tool(tu["name"], tu["input"], cwd)
                except Exception as e:
                    result = f"Error: {e}"

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu["id"],
                "content": result,
            })

        messages.append({"role": "user", "content": tool_results})
    else:
        final_text += "\n[Agent reached iteration limit]"

    return final_text if final_text else "[Agent produced no output]"
