"""Open Claude - REPL loop with rich TUI, streaming display, and tool execution."""

import os
from typing import Any, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .agent import build_agent_schema, execute_agent, load_agent_types
from .api import create_client, stream_message
from .compact import compact_conversation, needs_compaction
from .config import (
    AVAILABLE_MODELS,
    PROVIDERS,
    get_api_key_for,
    get_model,
    get_model_provider,
    resolve_model,
)
from .hooks import HOOK_EVENTS, HookRunner
from .mcp import MCPManager
from .profile import (
    AgentProfile,
    MEMORY_MODES,
    PERMISSION_MODES,
    delete_profile,
    list_profiles,
    load_profile,
    save_profile,
)
from .prompt import build_system_prompt
from .sessions import SessionStore, latest_session_id, list_sessions, load_session
from .settings import (
    Settings,
    apply_env,
    load_settings,
    match_any,
    suggest_rule,
    write_local_allow_rule,
)
from .skills.bundled import init_bundled_skills
from .skills.registry import get_registry, load_skills
from .tasks import get_task_store
from .tokens import CostTracker, estimate_messages_tokens, get_effective_context_window, tokens_remaining
from .tools import TOOL_SCHEMAS, execute_tool
from . import tui

console = Console()


# ---------------------------------------------------------------------------
# Permission system
# ---------------------------------------------------------------------------

# Tools that are safe to auto-execute (read-only, no side effects)
SAFE_TOOLS = {"Read", "Glob", "Grep", "Skill", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "Agent"}

# Tools that modify files or run commands - require confirmation
DANGEROUS_TOOLS = {"Bash", "Write", "Edit"}


class PermissionManager:
    """Manages user permission for tool execution.

    Decision order (matching Claude Code):
      1. deny rules from settings.json    -> block
      2. ask rules from settings.json     -> always prompt
      3. safe (read-only) tools           -> allow
      4. global mode (always_allow/deny)  -> allow/block
      5. allow rules from settings.json   -> allow
      6. session per-tool always-allow    -> allow
      7. prompt the user
    """

    def __init__(self, settings: Optional[Settings] = None, cwd: str = "."):
        # "default" = ask each time for dangerous tools
        # "always_allow" = auto-approve everything
        # "deny_all" = block all dangerous tools
        self.mode = "default"
        self.cwd = cwd
        self.settings = settings or Settings()
        # Per-tool always-allow set (user said "allow always" for this tool)
        self._always_allowed: set[str] = set()
        # Profile-carried rules (combined with settings rules)
        self.profile_allow: list[str] = []
        self.profile_deny: list[str] = []
        self.profile_ask: list[str] = []

    def _allow_rules(self) -> list[str]:
        return self.settings.allow_rules + self.profile_allow

    def _deny_rules(self) -> list[str]:
        return self.settings.deny_rules + self.profile_deny

    def _ask_rules(self) -> list[str]:
        return self.settings.ask_rules + self.profile_ask

    def set_profile_rules(self, allow: list[str], deny: list[str], ask: list[str]):
        self.profile_allow = list(allow)
        self.profile_deny = list(deny)
        self.profile_ask = list(ask)

    def check_permission(self, tool_name: str, tool_input: dict[str, Any]) -> tuple[bool, str]:
        """
        Check if a tool call is permitted.
        Returns (allowed: bool, reason: str).
        If not allowed, reason explains why (e.g., "denied by user").
        """
        # 1. Deny rules always win
        rule = match_any(self._deny_rules(), tool_name, tool_input)
        if rule:
            return False, f"Denied by rule: {rule}"

        # 2. Ask rules force a prompt regardless of everything else
        ask_rule = match_any(self._ask_rules(), tool_name, tool_input)
        if ask_rule:
            return self._prompt_user(tool_name, tool_input, forced_by=ask_rule)

        # 3. Safe tools always allowed
        if tool_name in SAFE_TOOLS:
            return True, ""

        # 4. Global modes
        if self.mode == "always_allow":
            return True, ""
        if self.mode == "deny_all":
            return False, "All dangerous tools are blocked (deny_all mode)"

        # 5. Allow rules from settings + profile
        rule = match_any(self._allow_rules(), tool_name, tool_input)
        if rule:
            return True, ""

        # 6. Per-tool session always-allow
        if tool_name in self._always_allowed:
            return True, ""

        # 7. Ask the user
        return self._prompt_user(tool_name, tool_input)

    def _prompt_user(self, tool_name: str, tool_input: dict[str, Any],
                     forced_by: str = "") -> tuple[bool, str]:
        """Interactively ask the user for permission with a rich preview."""
        console.print()

        # Show what will be executed
        if tool_name == "Bash":
            tui.render_bash_preview(console, tool_input.get("command", ""))
        elif tool_name == "Write":
            tui.render_write_preview(
                console, tool_input.get("file_path", ""), tool_input.get("content", ""),
            )
        elif tool_name == "Edit":
            tui.render_edit_diff(
                console,
                tool_input.get("file_path", ""),
                tool_input.get("old_string", ""),
                tool_input.get("new_string", ""),
            )
        else:
            preview = _shorten(str(tool_input), 300)
            console.print(Panel(
                preview,
                title=f"[bold yellow]{tool_name}[/bold yellow]",
                border_style="yellow",
            ))

        if forced_by:
            console.print(f"  [dim]Confirmation required by settings rule: {forced_by}[/dim]")

        rule = suggest_rule(tool_name, tool_input)
        options = [
            "Yes, allow once",
            f"Yes, allow [bold]{tool_name}[/bold] for this session",
            f"Yes, and save [bold]{rule}[/bold] to .claude/settings.local.json",
            "No, deny",
        ]
        choice = tui.permission_menu(console, options, prompt="Allow?")

        if choice == 0:
            return True, ""
        if choice == 1:
            self._always_allowed.add(tool_name)
            console.print(f"  [dim]{tool_name} will be auto-approved for this session[/dim]")
            return True, ""
        if choice == 2:
            path = write_local_allow_rule(self.cwd, rule)
            self.settings.allow_rules.append(rule)
            console.print(f"  [dim]Saved '{rule}' to {path}[/dim]")
            return True, ""
        return False, "Denied by user"


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def print_tool_call(name: str, params: dict[str, Any]):
    """Display a tool call being made."""
    if name == "Bash":
        cmd = params.get("command", "")
        console.print(f"  [bold yellow]> Bash[/bold yellow] [dim]{_shorten(cmd, 120)}[/dim]")
    elif name == "Read":
        fp = params.get("file_path", "")
        console.print(f"  [bold yellow]> Read[/bold yellow] [dim]{fp}[/dim]")
    elif name == "Write":
        fp = params.get("file_path", "")
        console.print(f"  [bold yellow]> Write[/bold yellow] [dim]{fp}[/dim]")
    elif name == "Edit":
        fp = params.get("file_path", "")
        console.print(f"  [bold yellow]> Edit[/bold yellow] [dim]{fp}[/dim]")
    elif name == "Glob":
        p = params.get("pattern", "")
        console.print(f"  [bold yellow]> Glob[/bold yellow] [dim]{p}[/dim]")
    elif name == "Grep":
        p = params.get("pattern", "")
        console.print(f"  [bold yellow]> Grep[/bold yellow] [dim]{p}[/dim]")
    elif name == "Skill":
        s = params.get("skill", "")
        a = params.get("args", "")
        console.print(f"  [bold yellow]> Skill[/bold yellow] [dim]/{s} {a}[/dim]")
    elif name == "TaskCreate":
        console.print(f"  [bold yellow]> TaskCreate[/bold yellow] [dim]{params.get('subject', '')}[/dim]")
    elif name == "TaskUpdate":
        console.print(f"  [bold yellow]> TaskUpdate[/bold yellow] [dim]#{params.get('taskId', '')} -> {params.get('status', '')}[/dim]")
    elif name == "TaskList":
        console.print(f"  [bold yellow]> TaskList[/bold yellow]")
    elif name == "TaskGet":
        console.print(f"  [bold yellow]> TaskGet[/bold yellow] [dim]#{params.get('taskId', '')}[/dim]")
    elif name == "Agent":
        desc = params.get("description", "")
        atype = params.get("subagent_type", "general-purpose")
        console.print(f"  [bold magenta]> Agent[/bold magenta] [dim]({atype}) {desc}[/dim]")
    elif name.startswith("mcp__"):
        console.print(f"  [bold blue]> MCP[/bold blue] [dim]{name} {_shorten(str(params), 100)}[/dim]")
    else:
        console.print(f"  [bold yellow]> {name}[/bold yellow]")


def print_tool_result(name: str, result: str):
    """Display tool execution result."""
    lines = result.split("\n")
    if len(lines) > 30:
        preview = "\n".join(lines[:15] + [f"  ... ({len(lines) - 30} more lines) ..."] + lines[-15:])
    else:
        preview = result
    console.print(f"  [dim]{preview}[/dim]")
    console.print()


def _shorten(s: str, max_len: int) -> str:
    s = s.replace("\n", " ")
    if len(s) > max_len:
        return s[:max_len] + "..."
    return s


# ---------------------------------------------------------------------------
# Conversation management
# ---------------------------------------------------------------------------

class Conversation:
    """Manages message history and the query loop."""

    def __init__(self, cwd: str, permission_mode: str = "default",
                 resume_session_id: Optional[str] = None,
                 profile: Optional[AgentProfile] = None):
        self.cwd = cwd
        self.client = create_client()
        self.cost_tracker = CostTracker()

        # --- Agent profile (the editable, reusable configuration) ---
        self.profile = profile or AgentProfile()

        # --- Settings (multi-layer settings.json) ---
        self.settings = load_settings(cwd)
        apply_env(self.settings)
        # Model precedence: profile.model > settings.model > env/default
        chosen_model = self.profile.model or self.settings.model
        if chosen_model:
            os.environ["CLAUDE_MODEL"] = resolve_model(chosen_model)
        self.model = get_model()

        self.permissions = PermissionManager(self.settings, cwd)
        # Permission precedence: explicit (non-default) CLI arg > profile
        self.permissions.mode = (
            permission_mode if permission_mode != "default"
            else self.profile.permission_mode
        )

        # --- Session persistence ---
        self.messages: list[dict[str, Any]] = []
        if resume_session_id:
            loaded = load_session(cwd, resume_session_id)
            if loaded is not None:
                self.messages = loaded
                self.session = SessionStore(cwd, resume_session_id)
            else:
                console.print(f"[yellow]Session {resume_session_id} not found; starting fresh.[/yellow]")
                self.session = SessionStore(cwd)
        else:
            self.session = SessionStore(cwd)

        # --- Hooks ---
        self.hooks = HookRunner(self.settings.hooks, cwd, self.session.session_id)

        # --- Profile guardrails (env, permission rules, profile hooks) ---
        self._apply_guardrails()

        # --- MCP servers ---
        self.mcp = MCPManager(cwd)
        mcp_config = self.mcp.load_config()
        if mcp_config:
            with console.status("[bold cyan]Connecting to MCP servers...[/bold cyan]"):
                results = self.mcp.start_all()
            for name, ok, detail in results:
                style = "green" if ok else "red"
                console.print(f"  [dim]mcp:[/dim] [{style}]{name}[/{style}] [dim]{detail}[/dim]")

        # --- Skills ---
        init_bundled_skills()
        load_skills(cwd)
        get_registry().set_disabled(self.profile.disabled_skills)

        # --- Subagent types ---
        self.agent_types = load_agent_types(cwd)

        # SessionStart hook output becomes extra context (fired once at startup)
        start_result = self.hooks.run("SessionStart")
        self._session_hook_context = start_result.context
        for err in start_result.errors:
            console.print(f"  [dim red]SessionStart hook: {err}[/dim red]")

        # --- System prompt + tool schemas (profile-driven) ---
        self._build_system_prompt()
        self._build_tool_schemas()

    # -- profile application -------------------------------------------------

    def _apply_guardrails(self):
        """Apply profile env vars, permission rules, and profile-level hooks."""
        for k, v in self.profile.env.items():
            os.environ[str(k)] = str(v)
        self.permissions.set_profile_rules(
            self.profile.allow_rules, self.profile.deny_rules, self.profile.ask_rules,
        )
        # Hooks = settings hooks + profile hooks
        merged: dict[str, list] = {}
        for src in (self.settings.hooks, self.profile.hooks):
            for event, matchers in (src or {}).items():
                if isinstance(matchers, list):
                    merged.setdefault(event, []).extend(matchers)
        self.hooks.config = merged

    def _build_system_prompt(self):
        """(Re)build the system prompt from the active profile."""
        self.system_prompt = build_system_prompt(
            self.cwd,
            memory_mode=self.profile.memory_mode,
            prompt_mode=self.profile.system_prompt_mode,
            extra=self.profile.system_prompt_extra,
            override=self.profile.system_prompt_override,
            style=self.profile.style,
            pinned_files=self.profile.pinned_files,
        )
        if self._session_hook_context:
            self.system_prompt += f"\n# Session Hook Context\n{self._session_hook_context}\n"

    def _build_tool_schemas(self):
        disabled = set(self.profile.disabled_tools)
        schemas: list[dict[str, Any]] = [
            s for s in TOOL_SCHEMAS if s.get("name") not in disabled
        ]
        # Agent tool (unless disabled), with subagent types filtered
        if "Agent" not in disabled:
            visible_agents = {
                k: v for k, v in self.agent_types.items()
                if k not in self.profile.disabled_agents
            }
            if visible_agents:
                schemas.append(build_agent_schema(visible_agents))
        # MCP tools, minus disabled servers
        schemas.extend(
            self.mcp.get_tool_schemas(exclude_servers=set(self.profile.disabled_mcp_servers))
        )
        self.tool_schemas = schemas

    def apply_profile(self, profile: AgentProfile, *, reload_skills: bool = False):
        """Switch to a new profile and re-derive everything it controls."""
        self.profile = profile
        if profile.model:
            self.model = resolve_model(profile.model)
            os.environ["CLAUDE_MODEL"] = self.model
        self.permissions.mode = profile.permission_mode
        self.permissions._always_allowed.clear()
        self._apply_guardrails()
        self.rebuild(reload_skills=reload_skills)

    def rebuild(self, *, reload_skills: bool = False):
        """Re-apply skill toggles and rebuild prompt + tool schemas in place."""
        if reload_skills:
            registry = get_registry()
            registry.clear()
            init_bundled_skills()
            load_skills(self.cwd)
        get_registry().set_disabled(self.profile.disabled_skills)
        self._build_system_prompt()
        self._build_tool_schemas()

    def snapshot_profile(self, name: str) -> AgentProfile:
        """Capture the live runtime state into a profile for saving."""
        self.profile.name = name
        self.profile.model = self.model
        self.profile.permission_mode = self.permissions.mode
        self.profile.disabled_skills = get_registry().disabled_names()
        return self.profile

    def add_user_message(self, text: str):
        msg = {"role": "user", "content": text}
        self.messages.append(msg)
        self.session.append_message(msg)

    def run_turn(self) -> None:
        """Run one assistant turn: stream response, execute tools, loop until done."""
        max_iterations = self.profile.max_iterations  # Safety limit for tool loops
        stop_hook_fired = False

        for iteration in range(max_iterations):
            # Auto-compact if approaching context limit
            self._maybe_compact()

            stop_reason = self._stream_assistant_response()

            if stop_reason == "tool_use":
                # Process all tool_use blocks in the last assistant message
                self._execute_pending_tools()
                continue

            # Turn is ending — give Stop hooks a chance to keep it going
            if stop_reason == "end_turn" and not stop_hook_fired and self.hooks.has_hooks("Stop"):
                stop_hook_fired = True
                result = self.hooks.run("Stop", extra={"stop_hook_active": True})
                for err in result.errors:
                    console.print(f"  [dim red]Stop hook: {err}[/dim red]")
                if result.blocked:
                    console.print(f"  [dim]Stop hook requested continuation: {_shorten(result.block_reason, 120)}[/dim]")
                    self.add_user_message(
                        f"[Stop hook feedback - address this before finishing]\n{result.block_reason}"
                    )
                    continue
            break

    def _maybe_compact(self):
        """Auto-compact conversation if near context window limit."""
        if not self.messages or not self.profile.auto_compact:
            return
        if needs_compaction(self.messages, self.model, self.profile.compact_threshold):
            console.print("\n  [dim bold]Compacting conversation...[/dim bold]")
            try:
                self.messages = compact_conversation(
                    self.client, self.messages, self.model, self.system_prompt,
                )
                self.session.rewrite(self.messages)
                remaining = tokens_remaining(self.messages, self.model)
                console.print(f"  [dim]Compacted. ~{remaining:,} tokens available.[/dim]\n")
            except Exception as e:
                console.print(f"  [dim red]Compaction failed: {e}[/dim red]\n")

    def _stream_assistant_response(self) -> str:
        """Stream one assistant response (live Markdown). Returns stop_reason."""
        tool_uses: list[dict[str, Any]] = []
        stop_reason = "end_turn"

        md_stream = tui.MarkdownStream(console)

        # Use a single generator for the entire stream
        event_gen = stream_message(
            self.client, self.messages, self.system_prompt,
            model=self.model, tools=self.tool_schemas,
            max_tokens=self.profile.max_tokens,
            temperature=self.profile.temperature,
            thinking_budget=self.profile.thinking_budget if self.profile.thinking else None,
        )

        # Show spinner until first text/tool event
        spinner_active = True
        status = console.status("[bold cyan]Thinking...[/bold cyan]", spinner="dots")
        status.start()

        try:
            for event in event_gen:
                etype = event["type"]

                if etype == "thinking_delta":
                    pass  # extended thinking not surfaced

                elif etype == "text_delta":
                    if spinner_active:
                        status.stop()
                        spinner_active = False
                    md_stream.feed(event["text"])

                elif etype == "tool_use_start":
                    if spinner_active:
                        status.stop()
                        spinner_active = False
                    md_stream.finish()

                elif etype == "tool_input_delta":
                    pass  # Accumulating JSON in api.py

                elif etype == "tool_use_end":
                    tool_uses.append({
                        "type": "tool_use",
                        "id": event["id"],
                        "name": event["name"],
                        "input": event["input"],
                    })

                elif etype == "message_end":
                    stop_reason = event.get("stop_reason", "end_turn")
                    usage = event.get("usage", {})
                    self.cost_tracker.add_usage(
                        self.model,
                        input_tokens=usage.get("input_tokens", 0),
                        output_tokens=usage.get("output_tokens", 0),
                        cache_read=usage.get("cache_read_input_tokens", 0),
                        cache_creation=usage.get("cache_creation_input_tokens", 0),
                    )

                elif etype == "error":
                    if spinner_active:
                        status.stop()
                        spinner_active = False
                    md_stream.finish()
                    console.print(f"\n[bold red]Error: {event['error']}[/bold red]")
                    return "error"
        finally:
            if spinner_active:
                status.stop()
            md_stream.finish()

        # Build the assistant message content blocks
        content: list[dict[str, Any]] = []
        if md_stream.buffer:
            content.append({"type": "text", "text": md_stream.buffer})
        for tu in tool_uses:
            content.append(tu)

        if content:
            msg = {"role": "assistant", "content": content}
            self.messages.append(msg)
            self.session.append_message(msg)

        return stop_reason

    def _execute_pending_tools(self):
        """Execute tool_use blocks from the last assistant message and add results."""
        if not self.messages or self.messages[-1]["role"] != "assistant":
            return

        assistant_content = self.messages[-1]["content"]
        tool_results: list[dict[str, Any]] = []

        for block in assistant_content:
            if block.get("type") != "tool_use":
                continue

            tool_name = block["name"]
            tool_input = block["input"]
            tool_id = block["id"]

            # --- PreToolUse hooks (can block) ---
            pre = self.hooks.run("PreToolUse", tool_name=tool_name, tool_input=tool_input)
            for err in pre.errors:
                console.print(f"  [dim red]PreToolUse hook: {err}[/dim red]")
            if pre.blocked:
                console.print(f"  [bold red]> {tool_name} blocked by hook[/bold red] [dim]{_shorten(pre.block_reason, 120)}[/dim]")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": f"Tool execution blocked by PreToolUse hook: {pre.block_reason}",
                    "is_error": True,
                })
                continue

            # --- Permission check ---
            allowed, reason = self.permissions.check_permission(tool_name, tool_input)

            if not allowed:
                console.print(f"  [bold red]> {tool_name} blocked[/bold red] [dim]{reason}[/dim]")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": f"Tool execution denied by user: {reason}",
                    "is_error": True,
                })
                continue

            # Display the tool call (for tools that skip the permission prompt)
            if tool_name in SAFE_TOOLS or self.mcp.is_mcp_tool(tool_name):
                print_tool_call(tool_name, tool_input)

            # Execute — Agent needs client, MCP routes to servers, rest use execute_tool
            try:
                if tool_name == "Agent":
                    console.print(f"  [dim]Sub-agent running...[/dim]")
                    result = execute_agent(tool_input, self.cwd, self.client, self.agent_types)
                elif self.mcp.is_mcp_tool(tool_name):
                    result = self.mcp.call(tool_name, tool_input)
                else:
                    result = execute_tool(tool_name, tool_input, self.cwd)
            except Exception as e:
                result = f"Error executing {tool_name}: {e}"

            # --- PostToolUse hooks (feedback only) ---
            post = self.hooks.run(
                "PostToolUse", tool_name=tool_name, tool_input=tool_input,
                extra={"tool_response": result[:10_000]},
            )
            for reason_text in post.reasons + post.errors:
                console.print(f"  [dim yellow]PostToolUse hook: {_shorten(reason_text, 150)}[/dim yellow]")
            if post.blocked:
                # Feedback is appended to the tool result so the model sees it
                result += f"\n\n[PostToolUse hook feedback]: {post.block_reason}"

            # Display result (abbreviated)
            print_tool_result(tool_name, result)

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": result,
            })

        if tool_results:
            msg = {"role": "user", "content": tool_results}
            self.messages.append(msg)
            self.session.append_message(msg)

    # -- status line for the bottom toolbar ----------------------------------

    def status_line(self) -> str:
        used = estimate_messages_tokens(self.messages)
        window = get_effective_context_window(self.model)
        pct = min(100, int(used * 100 / window)) if window else 0
        mcp_running = sum(1 for s in self.mcp.servers.values() if s.status == "running")
        parts = [
            f" {self.model}",
            f"ctx {pct}%",
            f"${self.cost_tracker.total_cost_usd:.4f}",
        ]
        if self.profile.name and self.profile.name != "default":
            parts.append(f"agent {self.profile.name}")
        if mcp_running:
            parts.append(f"mcp {mcp_running}")
        parts.append(f"session {self.session.session_id}")
        return " | ".join(parts) + " "


# ---------------------------------------------------------------------------
# Welcome banner
# ---------------------------------------------------------------------------

def print_welcome(conv: Conversation):
    info = Text()
    info.append("Open Claude\n", style="bold cyan")
    if conv.profile.name and conv.profile.name != "default":
        info.append(f"agent    {conv.profile.name}\n", style="dim")
    info.append(f"model    {conv.model}\n", style="dim")
    info.append(f"cwd      {conv.cwd}\n", style="dim")
    info.append(f"session  {conv.session.session_id}", style="dim")
    if len(conv.messages) > 0:
        info.append(f"  (resumed, {len(conv.messages)} messages)", style="dim")
    info.append("\n", style="dim")
    if conv.settings.sources:
        info.append(f"settings {len(conv.settings.sources)} file(s) loaded\n", style="dim")
    if conv.hooks.count():
        info.append(f"hooks    {conv.hooks.count()} configured\n", style="dim")
    running = [n for n, s in conv.mcp.servers.items() if s.status == "running"]
    if running:
        info.append(f"mcp      {', '.join(running)}\n", style="dim")
    info.append("\nType a message, /help for commands, Tab to complete, 'quit' to exit.", style="dim")

    console.print()
    console.print(Panel.fit(info, border_style="cyan"))
    console.print()


# ---------------------------------------------------------------------------
# REPL commands
# ---------------------------------------------------------------------------

BUILTIN_COMMANDS: list[tuple[str, str]] = [
    ("help", "Show help"),
    ("clear", "Clear conversation history"),
    ("compact", "Manually compact conversation"),
    ("cost", "Show token usage and cost"),
    ("model", "Show or switch model: /model [name]"),
    ("tasks", "Show current tasks"),
    ("skills", "List all available skills"),
    ("skill", "Enable/disable/reload a skill: /skill <enable|disable|reload> [name]"),
    ("tool", "Enable/disable base tools: /tool [list|disable X|enable X|only A B C]"),
    ("agents", "List available subagent types"),
    ("mcp", "MCP servers: /mcp [status|enable <name>|disable <name>]"),
    ("hooks", "Show configured hooks"),
    ("hook", "Add/clear profile hooks: /hook [list|add <event> <matcher> <cmd>|clear]"),
    ("config", "Show loaded settings"),
    ("prompt", "View/edit the system prompt: /prompt [show|append|set|reset]"),
    ("style", "Set an output-style instruction: /style [show|set <text>|clear]"),
    ("memory", "Memory mode: /memory [show|mode <full|project|summary|off>|reload]"),
    ("context", "Pinned context files: /context [list|add <file>|remove <file>|clear]"),
    ("onstart", "Startup action: /onstart [show|set <prompt>|clear]"),
    ("env", "Profile env vars: /env [list|set KEY=VAL|unset KEY]"),
    ("runtime", "Run mechanism: /runtime [max-iterations|auto-compact|max-tokens|temperature|thinking|compact-threshold]"),
    ("profile", "Reusable agent profiles: /profile [show|list|save|load|new|delete]"),
    ("resume", "Resume a previous session"),
    ("permission", "Permission mode/rules: /permission [mode|allow <rule>|deny <rule>|ask <rule>|clear]"),
    ("quit", "Exit"),
]


def _handle_command(text: str, conv: Conversation) -> bool:
    """Handle a built-in REPL command. Returns True if handled."""
    lower = text.lower()

    if lower == "/clear":
        conv.messages.clear()
        conv.session = SessionStore(conv.cwd)  # start a fresh transcript
        console.print("[dim]Conversation cleared (new session started).[/dim]")
        return True

    if lower == "/help":
        registry = get_registry()
        user_skills = registry.get_user_invocable()

        table = Table(show_header=False, box=None, padding=(0, 2))
        for name, desc in BUILTIN_COMMANDS:
            table.add_row(f"[bold cyan]/{name}[/bold cyan]", desc)
        console.print(Panel(table, title="Commands", border_style="blue"))

        if user_skills:
            stable = Table(show_header=False, box=None, padding=(0, 2))
            for s in user_skills:
                hint = f" {s.argument_hint}" if s.argument_hint else ""
                stable.add_row(f"[bold cyan]/{s.name}[/bold cyan][dim]{hint}[/dim]", s.description or "")
            console.print(Panel(stable, title="Skills", border_style="blue"))

        console.print(Panel(
            "[bold]Tips[/bold]\n"
            "  - Tab completes /commands and @file paths\n"
            "  - @path/to/file attaches the file contents to your message\n"
            "  - Permission modes: default | always_allow | deny_all (/permission <mode>)\n"
            "  - Allow rules can be persisted to .claude/settings.local.json from the prompt\n"
            "  - Ctrl+C interrupts a response",
            border_style="blue",
        ))
        return True

    if lower.startswith("/model"):
        parts = text.split(maxsplit=1)
        if len(parts) > 1:
            resolved = resolve_model(parts[1])
            conv.model = resolved
            os.environ["CLAUDE_MODEL"] = resolved
            console.print(f"[dim]Model switched to: {resolved}[/dim]")
            provider = get_model_provider(resolved)
            if not get_api_key_for(provider):
                spec = PROVIDERS.get(provider, {})
                envs = " or ".join(spec.get("env", [])) or "the provider API key"
                console.print(
                    f"[yellow]Warning: no API key for {spec.get('label', provider)}. "
                    f"Set {envs} or add it to ~/.claude/config.json before sending.[/yellow]"
                )
        else:
            table = Table(show_header=True, box=None, padding=(0, 2))
            table.add_column("", style="bold cyan")
            table.add_column("model id", style="dim")
            table.add_column("provider", style="dim")
            table.add_column("key", style="dim")
            table.add_column("aliases", style="dim")
            for m in AVAILABLE_MODELS:
                marker = "*" if m["id"] == conv.model else " "
                provider = m.get("provider", "anthropic")
                has_key = bool(get_api_key_for(provider))
                key_cell = "[green]ok[/green]" if has_key else "[red]missing[/red]"
                table.add_row(
                    f"{marker} {m['label']}", m["id"],
                    PROVIDERS.get(provider, {}).get("label", provider),
                    key_cell, ", ".join(m["aliases"][:3]),
                )
            console.print(Panel(table, title=f"Models (current: {conv.model})", border_style="blue"))
            console.print("[dim]Switch with /model <name>, e.g. /model opus or /model qwen[/dim]")
        return True

    if lower == "/cost":
        console.print(f"[dim]{conv.cost_tracker.format_summary()}[/dim]")
        remaining = tokens_remaining(conv.messages, conv.model)
        console.print(f"[dim]Context: ~{estimate_messages_tokens(conv.messages):,} tokens used, ~{remaining:,} remaining[/dim]")
        return True

    if lower == "/compact":
        if len(conv.messages) < 4:
            console.print("[dim]Too few messages to compact.[/dim]")
        else:
            console.print("[dim]Compacting conversation...[/dim]")
            try:
                conv.messages = compact_conversation(
                    conv.client, conv.messages, conv.model, conv.system_prompt,
                )
                conv.session.rewrite(conv.messages)
                remaining = tokens_remaining(conv.messages, conv.model)
                console.print(f"[dim]Done. ~{remaining:,} tokens available.[/dim]")
            except Exception as e:
                console.print(f"[dim red]Compaction failed: {e}[/dim red]")
        return True

    if lower.startswith("/permission"):
        parts = text.split(maxsplit=2)
        sub = parts[1] if len(parts) > 1 else ""
        arg = parts[2].strip() if len(parts) > 2 else ""

        if sub in PERMISSION_MODES:
            conv.permissions.mode = sub
            conv.profile.permission_mode = sub
            conv.permissions._always_allowed.clear()
            console.print(f"[dim]Permission mode set to: {sub}[/dim]")
        elif sub in ("allow", "deny", "ask") and arg:
            target = {"allow": conv.profile.allow_rules,
                      "deny": conv.profile.deny_rules,
                      "ask": conv.profile.ask_rules}[sub]
            if arg not in target:
                target.append(arg)
            conv.permissions.set_profile_rules(
                conv.profile.allow_rules, conv.profile.deny_rules, conv.profile.ask_rules)
            console.print(f"[dim]Added {sub} rule: {arg}[/dim]")
        elif sub == "clear":
            conv.profile.allow_rules.clear()
            conv.profile.deny_rules.clear()
            conv.profile.ask_rules.clear()
            conv.permissions.set_profile_rules([], [], [])
            console.print("[dim]Cleared profile permission rules.[/dim]")
        else:
            mode = conv.permissions.mode
            allowed = ", ".join(conv.permissions._always_allowed) or "none"
            console.print(
                f"[dim]Current mode: {mode}\n"
                f"Session auto-approved tools: {allowed}\n"
                f"Profile rules: allow={conv.profile.allow_rules or '[]'}, "
                f"deny={conv.profile.deny_rules or '[]'}, ask={conv.profile.ask_rules or '[]'}\n"
                f"Settings rules: allow={len(conv.settings.allow_rules)}, "
                f"deny={len(conv.settings.deny_rules)}, ask={len(conv.settings.ask_rules)}\n"
                f"Usage: /permission <default|always_allow|deny_all|allow <rule>|deny <rule>|ask <rule>|clear>[/dim]"
            )
        return True

    if lower == "/tasks":
        store = get_task_store()
        tasks = store.list_all()
        if not tasks:
            console.print("[dim]No tasks.[/dim]")
        else:
            for t in tasks:
                color = {"pending": "dim", "in_progress": "cyan", "completed": "green"}.get(t.status, "white")
                console.print(f"  [{color}]{t.summary()}[/{color}]")
        return True

    if lower == "/skills":
        registry = get_registry()
        all_skills = registry.get_all()
        if not all_skills:
            console.print("[dim]No skills loaded.[/dim]")
        else:
            console.print(f"[dim]Loaded {len(all_skills)} skill(s):[/dim]")
            for s in all_skills:
                src = f"[{s.source}]"
                desc = f" - {s.description}" if s.description else ""
                invocable = " (user-invocable)" if s.user_invocable else ""
                console.print(f"  [bold cyan]/{s.name}[/bold cyan]{desc} [dim]{src}{invocable}[/dim]")
        return True

    if lower == "/agents":
        console.print(f"[dim]Available subagent types:[/dim]")
        for t in conv.agent_types.values():
            tools = ", ".join(t.tools)
            console.print(f"  [bold magenta]{t.name}[/bold magenta] [dim][{t.source}] tools: {tools}[/dim]")
            if t.description:
                console.print(f"    [dim]{t.description}[/dim]")
        return True

    if lower == "/mcp" or lower.startswith("/mcp "):
        parts = text.split(maxsplit=2)
        sub = parts[1].lower() if len(parts) > 1 else "status"
        arg = parts[2].strip() if len(parts) > 2 else ""

        if sub in ("enable", "disable") and arg:
            if arg not in conv.mcp.servers:
                console.print(f"[yellow]Unknown MCP server: {arg}[/yellow]")
                return True
            if sub == "disable":
                if arg not in conv.profile.disabled_mcp_servers:
                    conv.profile.disabled_mcp_servers.append(arg)
            else:
                if arg in conv.profile.disabled_mcp_servers:
                    conv.profile.disabled_mcp_servers.remove(arg)
            conv._build_tool_schemas()
            console.print(f"[dim]MCP server '{arg}' {sub}d for this agent.[/dim]")
            return True

        # status (default)
        if not conv.mcp.servers:
            console.print("[dim]No MCP servers configured. Add them to .mcp.json or ~/.claude/mcp.json[/dim]")
        else:
            disabled = set(conv.profile.disabled_mcp_servers)
            for name in conv.mcp.servers:
                tag = " [disabled by profile]" if name in disabled else ""
                line = next((l for l in conv.mcp.describe() if l.startswith(name + ":")), name)
                console.print(f"  [dim]{line}{tag}[/dim]")
        return True

    if lower == "/hooks":
        lines = conv.hooks.describe()
        if not lines:
            console.print("[dim]No hooks configured. Add them under \"hooks\" in settings.json[/dim]")
        else:
            for line in lines:
                console.print(f"  [dim]{line}[/dim]")
        return True

    if lower == "/config":
        console.print(f"[dim]Model: {conv.model}[/dim]")
        if conv.settings.sources:
            console.print("[dim]Settings files (low -> high precedence):[/dim]")
            for src in conv.settings.sources:
                console.print(f"  [dim]{src}[/dim]")
        else:
            console.print("[dim]No settings.json files found.[/dim]")
        if conv.settings.env:
            console.print(f"[dim]Env from settings: {', '.join(conv.settings.env)}[/dim]")
        return True

    if lower == "/resume":
        sessions = list_sessions(conv.cwd)
        # Exclude the current session
        sessions = [s for s in sessions if s["id"] != conv.session.session_id]
        if not sessions:
            console.print("[dim]No previous sessions for this directory.[/dim]")
            return True

        table = Table(box=None, padding=(0, 2))
        table.add_column("#", style="bold cyan")
        table.add_column("modified", style="dim")
        table.add_column("msgs", style="dim", justify="right")
        table.add_column("first message")
        for i, s in enumerate(sessions, start=1):
            table.add_row(str(i), s["modified"], str(s["messages"]), s["preview"])
        console.print(table)

        try:
            choice = console.input(f"  [bold]Resume which session? [1-{len(sessions)}, Enter to cancel] > [/bold]").strip()
        except (EOFError, KeyboardInterrupt):
            return True
        if choice.isdigit() and 1 <= int(choice) <= len(sessions):
            picked = sessions[int(choice) - 1]
            loaded = load_session(conv.cwd, picked["id"])
            if loaded is not None:
                conv.messages = loaded
                conv.session = SessionStore(conv.cwd, picked["id"])
                conv.hooks.session_id = picked["id"]
                console.print(f"[dim]Resumed session {picked['id']} ({len(loaded)} messages).[/dim]")
            else:
                console.print("[red]Failed to load session.[/red]")
        return True

    if lower == "/skill" or lower.startswith("/skill "):
        return _cmd_skill(text, conv)

    if lower == "/tool" or lower.startswith("/tool "):
        return _cmd_tool(text, conv)

    if lower.startswith("/prompt"):
        return _cmd_prompt(text, conv)

    if lower.startswith("/style"):
        return _cmd_style(text, conv)

    if lower.startswith("/memory"):
        return _cmd_memory(text, conv)

    if lower.startswith("/context"):
        return _cmd_context(text, conv)

    if lower.startswith("/onstart"):
        return _cmd_onstart(text, conv)

    if lower.startswith("/env"):
        return _cmd_env(text, conv)

    if lower == "/hook" or lower.startswith("/hook "):
        return _cmd_hook(text, conv)

    if lower.startswith("/runtime"):
        return _cmd_runtime(text, conv)

    if lower.startswith("/profile"):
        return _cmd_profile(text, conv)

    return False


# ---------------------------------------------------------------------------
# Editable-agent command handlers
# ---------------------------------------------------------------------------

def _read_multiline(prompt_label: str) -> str:
    """Read multi-line text from the user until a line with just '.'."""
    console.print(f"[dim]{prompt_label} (end with a single '.' on its own line; empty cancels):[/dim]")
    lines: list[str] = []
    while True:
        try:
            line = console.input("[dim]| [/dim]")
        except (EOFError, KeyboardInterrupt):
            return ""
        if line.strip() == ".":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _cmd_skill(text: str, conv: Conversation) -> bool:
    """/skill <enable|disable|reload> [name]"""
    registry = get_registry()
    parts = text.split()
    if len(parts) < 2:
        console.print("[dim]Usage: /skill <enable|disable|reload> [name][/dim]")
        disabled = registry.disabled_names()
        console.print(f"[dim]Disabled skills: {', '.join(disabled) or '(none)'}[/dim]")
        return True

    action = parts[1].lower()

    if action == "reload":
        conv.rebuild(reload_skills=True)
        count = len(registry.get_all())
        console.print(f"[dim]Reloaded skills from disk ({count} active).[/dim]")
        return True

    if action in ("enable", "disable"):
        if len(parts) < 3:
            console.print(f"[dim]Usage: /skill {action} <name>[/dim]")
            return True
        name = parts[2].lstrip("/")
        if name not in registry.known_names():
            console.print(f"[yellow]Unknown skill: {name}[/yellow]")
            return True
        if action == "disable":
            registry.disable(name)
        else:
            registry.enable(name)
        conv.profile.disabled_skills = registry.disabled_names()
        conv.rebuild()  # refresh the system prompt's skill listing
        console.print(f"[dim]Skill '{name}' {action}d.[/dim]")
        return True

    console.print("[dim]Usage: /skill <enable|disable|reload> [name][/dim]")
    return True


def _base_tool_names() -> list[str]:
    """Names of the core (non-Agent, non-MCP) tools, plus 'Agent'."""
    names = [s["name"] for s in TOOL_SCHEMAS if "name" in s]
    names.append("Agent")
    return names


def _cmd_tool(text: str, conv: Conversation) -> bool:
    """/tool [list|disable A B|enable A B|only A B C]"""
    parts = text.split()
    sub = parts[1].lower() if len(parts) > 1 else "list"
    args = [p for p in parts[2:]]
    all_names = _base_tool_names()
    disabled = set(conv.profile.disabled_tools)

    if sub == "list":
        table = Table(box=None, padding=(0, 2))
        table.add_column("tool", style="bold")
        table.add_column("state")
        for n in all_names:
            on = n not in disabled
            table.add_row(n, "[green]enabled[/green]" if on else "[red]disabled[/red]")
        console.print(table)
        console.print("[dim]/tool disable Bash Write Edit  |  /tool only Read Glob Grep[/dim]")
        return True

    if sub == "only":
        if not args:
            console.print("[dim]Usage: /tool only <ToolA> <ToolB> ...[/dim]")
            return True
        keep = {a for a in args}
        unknown = keep - set(all_names)
        if unknown:
            console.print(f"[yellow]Unknown tools: {', '.join(unknown)}[/yellow]")
        conv.profile.disabled_tools = [n for n in all_names if n not in keep]
        conv._build_tool_schemas()
        console.print(f"[dim]Tool surface limited to: {', '.join(sorted(keep & set(all_names))) or '(none)'}[/dim]")
        return True

    if sub in ("disable", "enable") and args:
        changed = []
        for name in args:
            if name not in all_names:
                console.print(f"[yellow]Unknown tool: {name}[/yellow]")
                continue
            if sub == "disable":
                disabled.add(name)
            else:
                disabled.discard(name)
            changed.append(name)
        conv.profile.disabled_tools = sorted(disabled)
        conv._build_tool_schemas()
        if changed:
            console.print(f"[dim]{sub}d: {', '.join(changed)}[/dim]")
        return True

    console.print("[dim]Usage: /tool [list|disable A B|enable A B|only A B C][/dim]")
    return True


def _cmd_style(text: str, conv: Conversation) -> bool:
    """/style [show|set <text>|clear]"""
    parts = text.split(maxsplit=2)
    sub = parts[1].lower() if len(parts) > 1 else "show"
    prof = conv.profile

    if sub == "show":
        console.print(f"[dim]Response style: {prof.style or '(none)'}[/dim]")
        return True
    if sub == "clear":
        prof.style = ""
        conv._build_system_prompt()
        console.print("[dim]Style cleared.[/dim]")
        return True
    if sub == "set":
        body = parts[2].strip() if len(parts) > 2 else _read_multiline("Response style instruction")
        if not body:
            console.print("[dim]Cancelled.[/dim]")
            return True
        prof.style = body
        conv._build_system_prompt()
        console.print("[dim]Style updated.[/dim]")
        return True
    console.print("[dim]Usage: /style [show|set <text>|clear][/dim]")
    return True


def _cmd_context(text: str, conv: Conversation) -> bool:
    """/context [list|add <file>|remove <file>|clear]"""
    parts = text.split(maxsplit=2)
    sub = parts[1].lower() if len(parts) > 1 else "list"
    arg = parts[2].strip() if len(parts) > 2 else ""
    prof = conv.profile

    if sub == "list":
        if not prof.pinned_files:
            console.print("[dim]No pinned context files.[/dim]")
        else:
            for f in prof.pinned_files:
                exists = os.path.exists(f if os.path.isabs(f) else os.path.join(conv.cwd, f))
                mark = "" if exists else " [yellow](missing)[/yellow]"
                console.print(f"  [dim]{f}[/dim]{mark}")
        return True
    if sub == "add" and arg:
        if arg not in prof.pinned_files:
            prof.pinned_files.append(arg)
        conv._build_system_prompt()
        console.print(f"[dim]Pinned: {arg}[/dim]")
        return True
    if sub == "remove" and arg:
        if arg in prof.pinned_files:
            prof.pinned_files.remove(arg)
            conv._build_system_prompt()
            console.print(f"[dim]Unpinned: {arg}[/dim]")
        else:
            console.print(f"[yellow]Not pinned: {arg}[/yellow]")
        return True
    if sub == "clear":
        prof.pinned_files.clear()
        conv._build_system_prompt()
        console.print("[dim]Cleared pinned context.[/dim]")
        return True
    console.print("[dim]Usage: /context [list|add <file>|remove <file>|clear][/dim]")
    return True


def _cmd_onstart(text: str, conv: Conversation) -> bool:
    """/onstart [show|set <prompt>|clear]"""
    parts = text.split(maxsplit=2)
    sub = parts[1].lower() if len(parts) > 1 else "show"
    prof = conv.profile

    if sub == "show":
        console.print(f"[dim]Startup action: {prof.startup_prompt or '(none)'}[/dim]")
        return True
    if sub == "clear":
        prof.startup_prompt = ""
        console.print("[dim]Startup action cleared.[/dim]")
        return True
    if sub == "set":
        body = parts[2].strip() if len(parts) > 2 else _read_multiline("Startup prompt (runs once at session start)")
        if not body:
            console.print("[dim]Cancelled.[/dim]")
            return True
        prof.startup_prompt = body
        console.print("[dim]Startup action set. It runs automatically on a fresh session.[/dim]")
        return True
    console.print("[dim]Usage: /onstart [show|set <prompt>|clear][/dim]")
    return True


def _cmd_env(text: str, conv: Conversation) -> bool:
    """/env [list|set KEY=VAL|unset KEY]"""
    parts = text.split(maxsplit=2)
    sub = parts[1].lower() if len(parts) > 1 else "list"
    arg = parts[2].strip() if len(parts) > 2 else ""
    prof = conv.profile

    if sub == "list":
        if not prof.env:
            console.print("[dim]No profile env vars.[/dim]")
        else:
            for k, v in prof.env.items():
                console.print(f"  [dim]{k}={v}[/dim]")
        return True
    if sub == "set" and "=" in arg:
        key, _, value = arg.partition("=")
        key = key.strip()
        if not key:
            console.print("[dim]Usage: /env set KEY=VAL[/dim]")
            return True
        prof.env[key] = value
        os.environ[key] = value
        console.print(f"[dim]Set {key}={value}[/dim]")
        return True
    if sub == "unset" and arg:
        prof.env.pop(arg, None)
        os.environ.pop(arg, None)
        console.print(f"[dim]Unset {arg}[/dim]")
        return True
    console.print("[dim]Usage: /env [list|set KEY=VAL|unset KEY][/dim]")
    return True


def _cmd_hook(text: str, conv: Conversation) -> bool:
    """/hook [list|add <event> <matcher> <command>|clear]"""
    parts = text.split(maxsplit=4)
    sub = parts[1].lower() if len(parts) > 1 else "list"
    prof = conv.profile

    if sub == "list":
        if not prof.hooks:
            console.print("[dim]No profile hooks. Add with /hook add PreToolUse Bash \"<cmd>\".[/dim]")
            return True
        for event, matchers in prof.hooks.items():
            for m in matchers:
                matcher = m.get("matcher", "*") or "*"
                for h in m.get("hooks", []):
                    console.print(f"  [dim]{event} [{matcher}]: {h.get('command', '')}[/dim]")
        return True

    if sub == "add":
        # /hook add <event> <matcher> <command...>
        if len(parts) < 5:
            console.print("[dim]Usage: /hook add <event> <matcher> <command>[/dim]")
            console.print(f"[dim]Events: {', '.join(HOOK_EVENTS)}[/dim]")
            return True
        event, matcher, command = parts[2], parts[3], parts[4]
        if event not in HOOK_EVENTS:
            console.print(f"[yellow]Unknown event: {event}. One of: {', '.join(HOOK_EVENTS)}[/yellow]")
            return True
        prof.hooks.setdefault(event, []).append(
            {"matcher": matcher, "hooks": [{"type": "command", "command": command}]}
        )
        conv._apply_guardrails()
        console.print(f"[dim]Added {event} hook [{matcher}]: {command}[/dim]")
        return True

    if sub == "clear":
        prof.hooks.clear()
        conv._apply_guardrails()
        console.print("[dim]Cleared profile hooks.[/dim]")
        return True

    console.print("[dim]Usage: /hook [list|add <event> <matcher> <command>|clear][/dim]")
    return True


def _cmd_prompt(text: str, conv: Conversation) -> bool:
    """/prompt [show|append|set|reset]"""
    parts = text.split(maxsplit=2)
    sub = parts[1].lower() if len(parts) > 1 else "show"
    prof = conv.profile

    if sub == "show":
        console.print(Panel(
            conv.system_prompt,
            title=f"System prompt (mode: {prof.system_prompt_mode})",
            border_style="blue",
        ))
        return True

    if sub == "reset":
        prof.system_prompt_mode = "default"
        prof.system_prompt_extra = ""
        prof.system_prompt_override = ""
        conv._build_system_prompt()
        console.print("[dim]System prompt reset to default.[/dim]")
        return True

    if sub in ("append", "set"):
        body = parts[2] if len(parts) > 2 else _read_multiline(
            "Appended instructions" if sub == "append" else "Full system prompt override"
        )
        if not body:
            console.print("[dim]Cancelled (no text).[/dim]")
            return True
        if sub == "append":
            prof.system_prompt_mode = "append"
            prof.system_prompt_extra = body
        else:
            prof.system_prompt_mode = "override"
            prof.system_prompt_override = body
        conv._build_system_prompt()
        console.print(f"[dim]System prompt mode set to '{prof.system_prompt_mode}'.[/dim]")
        return True

    console.print("[dim]Usage: /prompt [show|append [text]|set [text]|reset][/dim]")
    return True


def _cmd_memory(text: str, conv: Conversation) -> bool:
    """/memory [show|mode <full|project|summary|off>|reload]"""
    parts = text.split()
    sub = parts[1].lower() if len(parts) > 1 else "show"
    prof = conv.profile

    if sub == "show":
        from .claudemd import load_memory_files
        files = load_memory_files(conv.cwd)
        console.print(f"[dim]Memory mode: {prof.memory_mode}[/dim]")
        if files:
            console.print(f"[dim]Loaded {len(files)} memory file(s):[/dim]")
            for mf in files:
                console.print(f"  [dim]{mf.source}: {mf.path}[/dim]")
        else:
            console.print("[dim]No CLAUDE.md files found.[/dim]")
        return True

    if sub == "mode":
        if len(parts) < 3 or parts[2].lower() not in MEMORY_MODES:
            console.print(f"[dim]Modes: {', '.join(MEMORY_MODES)}[/dim]")
            return True
        prof.memory_mode = parts[2].lower()
        conv._build_system_prompt()
        console.print(f"[dim]Memory mode set to '{prof.memory_mode}'.[/dim]")
        return True

    if sub == "reload":
        conv._build_system_prompt()
        console.print("[dim]Reloaded memory files into the system prompt.[/dim]")
        return True

    console.print("[dim]Usage: /memory [show|mode <full|project|summary|off>|reload][/dim]")
    return True


def _cmd_runtime(text: str, conv: Conversation) -> bool:
    """/runtime [max-iterations N|auto-compact on/off|max-tokens N|temperature F|thinking on/off [budget]|compact-threshold F]"""
    parts = text.split()
    prof = conv.profile

    if len(parts) < 2:
        thr = f"{int(prof.compact_threshold*100)}%" if prof.compact_threshold else "(default buffer)"
        think = f"on (budget {prof.thinking_budget})" if prof.thinking else "off"
        console.print(
            f"[dim]Runtime settings:\n"
            f"  max-iterations    {prof.max_iterations}\n"
            f"  auto-compact      {'on' if prof.auto_compact else 'off'}\n"
            f"  compact-threshold {thr}\n"
            f"  max-tokens        {prof.max_tokens or '(global default)'}\n"
            f"  temperature       {prof.temperature if prof.temperature is not None else '(default)'}\n"
            f"  thinking          {think}[/dim]"
        )
        console.print("[dim]e.g. /runtime temperature 0.2 | thinking on 8000 | compact-threshold 0.8[/dim]")
        return True

    key = parts[1].lower()
    val = parts[2] if len(parts) > 2 else ""

    def _is_float(s: str) -> bool:
        try:
            float(s)
            return True
        except ValueError:
            return False

    if key in ("max-iterations", "max-iter") and val.isdigit():
        prof.max_iterations = max(1, int(val))
        console.print(f"[dim]max-iterations set to {prof.max_iterations}.[/dim]")
    elif key in ("auto-compact", "autocompact") and val.lower() in ("on", "off"):
        prof.auto_compact = val.lower() == "on"
        console.print(f"[dim]auto-compact {'on' if prof.auto_compact else 'off'}.[/dim]")
    elif key in ("max-tokens", "max-token"):
        if val.lower() in ("default", "none", "0", ""):
            prof.max_tokens = None
            console.print("[dim]max-tokens reset to global default.[/dim]")
        elif val.isdigit():
            prof.max_tokens = int(val)
            console.print(f"[dim]max-tokens set to {prof.max_tokens}.[/dim]")
        else:
            console.print("[dim]Usage: /runtime max-tokens <N|default>[/dim]")
    elif key in ("temperature", "temp"):
        if val.lower() in ("default", "none", ""):
            prof.temperature = None
            console.print("[dim]temperature reset to default.[/dim]")
        elif _is_float(val) and 0.0 <= float(val) <= 1.0:
            prof.temperature = float(val)
            # temperature and extended thinking are mutually exclusive
            note = ""
            if prof.thinking:
                prof.thinking = False
                note = " (extended thinking turned off)"
            console.print(f"[dim]temperature set to {prof.temperature}.{note}[/dim]")
        else:
            console.print("[dim]Usage: /runtime temperature <0.0-1.0|default>[/dim]")
    elif key in ("thinking", "think"):
        if val.lower() == "on":
            prof.thinking = True
            if len(parts) > 3 and parts[3].isdigit():
                prof.thinking_budget = max(1024, int(parts[3]))
            # extended thinking forces the API temperature; clear any custom value
            note = ""
            if prof.temperature is not None:
                prof.temperature = None
                note = " (temperature reset to default — ignored while thinking)"
            console.print(f"[dim]extended thinking on (budget {prof.thinking_budget}).{note}[/dim]")
        elif val.lower() == "off":
            prof.thinking = False
            console.print("[dim]extended thinking off.[/dim]")
        else:
            console.print("[dim]Usage: /runtime thinking <on [budget]|off>[/dim]")
    elif key in ("compact-threshold", "compact-thresh"):
        if val.lower() in ("default", "none", ""):
            prof.compact_threshold = None
            console.print("[dim]compact-threshold reset to default buffer.[/dim]")
        elif _is_float(val) and 0.1 <= float(val) <= 0.99:
            prof.compact_threshold = float(val)
            console.print(f"[dim]compact-threshold set to {int(prof.compact_threshold*100)}% of context window.[/dim]")
        else:
            console.print("[dim]Usage: /runtime compact-threshold <0.1-0.99|default>[/dim]")
    else:
        console.print("[dim]Usage: /runtime [max-iterations N|auto-compact on/off|max-tokens N|temperature F|thinking on/off [budget]|compact-threshold F][/dim]")
    return True


def _cmd_profile(text: str, conv: Conversation) -> bool:
    """/profile [show|list|save <name>|load <name>|new <name>|delete <name>]"""
    parts = text.split(maxsplit=2)
    sub = parts[1].lower() if len(parts) > 1 else "show"
    arg = parts[2].strip() if len(parts) > 2 else ""

    if sub == "show":
        console.print(Panel(
            "\n".join(conv.profile.summary_lines()),
            title="Active agent profile", border_style="magenta",
        ))
        return True

    if sub == "list":
        profiles = list_profiles(conv.cwd)
        if not profiles:
            console.print("[dim]No saved profiles. Save one with /profile save <name>.[/dim]")
            return True
        table = Table(box=None, padding=(0, 2))
        table.add_column("name", style="bold magenta")
        table.add_column("scope", style="dim")
        table.add_column("description", style="dim")
        for p in profiles:
            table.add_row(p["name"], p["scope"], p["description"] or "")
        console.print(table)
        return True

    if sub == "save":
        if not arg:
            console.print("[dim]Usage: /profile save <name> [--user][/dim]")
            return True
        scope = "project"
        if arg.endswith(" --user"):
            scope = "user"
            arg = arg[: -len(" --user")].strip()
        prof = conv.snapshot_profile(arg)
        path = save_profile(prof, conv.cwd, scope=scope)
        console.print(f"[dim]Saved profile '{prof.name}' to {path}[/dim]")
        return True

    if sub == "load":
        if not arg:
            console.print("[dim]Usage: /profile load <name>[/dim]")
            return True
        prof = load_profile(arg, conv.cwd)
        if prof is None:
            console.print(f"[yellow]Profile not found: {arg}[/yellow]")
            return True
        conv.apply_profile(prof, reload_skills=True)
        console.print(f"[dim]Loaded profile '{prof.name}'. Model: {conv.model}[/dim]")
        return True

    if sub == "new":
        name = arg or "default"
        conv.apply_profile(AgentProfile(name=name), reload_skills=True)
        console.print(f"[dim]Reset to a fresh profile '{name}'.[/dim]")
        return True

    if sub == "delete":
        if not arg:
            console.print("[dim]Usage: /profile delete <name>[/dim]")
            return True
        path = delete_profile(arg, conv.cwd)
        if path:
            console.print(f"[dim]Deleted profile file {path}[/dim]")
        else:
            console.print(f"[yellow]Profile not found: {arg}[/yellow]")
        return True

    console.print("[dim]Usage: /profile [show|list|save <name>|load <name>|new <name>|delete <name>][/dim]")
    return True


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------

def run_repl(cwd: Optional[str] = None, permission_mode: str = "default",
             resume_session_id: Optional[str] = None, continue_last: bool = False,
             profile_name: Optional[str] = None):
    """Main REPL loop."""
    cwd = cwd or os.getcwd()

    if continue_last and not resume_session_id:
        resume_session_id = latest_session_id(cwd)
        if not resume_session_id:
            console.print("[dim]No previous session to continue; starting fresh.[/dim]")

    profile = None
    if profile_name:
        profile = load_profile(profile_name, cwd)
        if profile is None:
            console.print(f"[yellow]Profile '{profile_name}' not found; using defaults.[/yellow]")

    conv = Conversation(cwd, permission_mode=permission_mode,
                        resume_session_id=resume_session_id, profile=profile)
    print_welcome(conv)

    # Profile startup action: run once for a fresh (non-resumed) session
    if conv.profile.startup_prompt and not conv.messages:
        console.print("[dim]Running profile startup action...[/dim]")
        conv.add_user_message(conv.profile.startup_prompt)
        try:
            conv.run_turn()
        except KeyboardInterrupt:
            console.print("\n[dim](startup interrupted)[/dim]")
        except Exception as e:
            console.print(f"[dim red]Startup action failed: {e}[/dim red]")

    def _get_commands() -> list[tuple[str, str]]:
        commands = list(BUILTIN_COMMANDS)
        for s in get_registry().get_user_invocable():
            commands.append((s.name, s.description or "skill"))
        return commands

    session = tui.make_prompt_session(_get_commands, cwd, conv.status_line)

    try:
        while True:
            try:
                # Get input
                if session is not None:
                    text = session.prompt("\n❯ ")
                else:
                    text = input("\nYou> ")

                text = text.strip()
                if not text:
                    continue

                # Exit
                if text.lower() in ("quit", "exit", "/quit", "/exit"):
                    console.print("[dim]Goodbye![/dim]")
                    break

                # Built-in commands
                if text.startswith("/") and _handle_command(text, conv):
                    continue

                # Slash command → skill invocation
                if text.startswith("/"):
                    parts = text.split(maxsplit=1)
                    skill_name = parts[0].lstrip("/")
                    skill_args = parts[1] if len(parts) > 1 else ""
                    registry = get_registry()
                    skill = registry.get(skill_name)
                    if skill:
                        # Inject the skill prompt as a user message
                        prompt_content = skill.get_prompt_for_command(skill_args)
                        conv.add_user_message(prompt_content)
                        conv.run_turn()
                        continue
                    # Not a known skill - pass through to model as normal text

                # UserPromptSubmit hooks (can block, can add context)
                hook_result = conv.hooks.run("UserPromptSubmit", extra={"prompt": text})
                for err in hook_result.errors:
                    console.print(f"  [dim red]UserPromptSubmit hook: {err}[/dim red]")
                if hook_result.blocked:
                    console.print(f"[red]Prompt blocked by hook:[/red] {hook_result.block_reason}")
                    continue
                if hook_result.context:
                    text = text + f"\n\n[Hook context]\n{hook_result.context}"

                # @file references → attach contents
                text = tui.expand_file_references(text, cwd)

                # Add message and run
                conv.add_user_message(text)
                conv.run_turn()

            except KeyboardInterrupt:
                console.print("\n[dim](interrupted)[/dim]")
                continue
            except EOFError:
                console.print("\n[dim]Goodbye![/dim]")
                break
    finally:
        conv.mcp.shutdown()
