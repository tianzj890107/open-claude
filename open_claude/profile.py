"""
Agent profiles — the "editable, reusable agent" layer.

A profile bundles every runtime knob that shapes how Open Claude behaves into
one named, saveable unit:

  - model                  which Claude model to use
  - system prompt mode     default / append-to-default / full-override
  - memory mode            how CLAUDE.md files are injected (full/project/summary/off)
  - disabled skills        skills to hide from the model and the user
  - permission mode        default / always_allow / deny_all
  - run mechanism          max tool-loop iterations, auto-compaction, max output tokens

Profiles are stored as JSON so they can be checked into a repo and reused:

  Project:  <cwd>/.claude/oc-profiles/<name>.json
  User:     ~/.claude/oc-profiles/<name>.json

`/profile save <name>` writes to the project dir by default; loading searches the
project dir first, then the user dir.
"""

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

PROFILE_DIR_NAME = "oc-profiles"

SYSTEM_PROMPT_MODES = ("default", "append", "override")
MEMORY_MODES = ("full", "project", "summary", "off")
PERMISSION_MODES = ("default", "always_allow", "deny_all")


@dataclass
class AgentProfile:
    """All mutable, saveable configuration for one agent persona."""

    name: str = "default"
    description: str = ""
    extends: Optional[str] = None         # inherit fields from another profile

    # Model (canonical id or friendly alias; resolved when applied). None = global default.
    model: Optional[str] = None

    # System prompt
    system_prompt_mode: str = "default"   # default | append | override
    system_prompt_extra: str = ""         # appended in "append" mode
    system_prompt_override: str = ""      # full replacement in "override" mode
    style: str = ""                       # short output-style instruction (always appended)

    # Memory (CLAUDE.md) handling
    memory_mode: str = "full"             # full | project | summary | off

    # Knowledge / context
    pinned_files: list[str] = field(default_factory=list)   # always attached to the prompt
    startup_prompt: str = ""              # auto-run once when a fresh session starts

    # Capability surface
    disabled_skills: list[str] = field(default_factory=list)
    disabled_tools: list[str] = field(default_factory=list)         # base/Agent tool names
    disabled_mcp_servers: list[str] = field(default_factory=list)   # MCP servers to hide
    disabled_agents: list[str] = field(default_factory=list)        # subagent types to hide

    # Permission behaviour
    permission_mode: str = "default"      # default | always_allow | deny_all
    allow_rules: list[str] = field(default_factory=list)
    deny_rules: list[str] = field(default_factory=list)
    ask_rules: list[str] = field(default_factory=list)

    # Guardrails / automation
    hooks: dict[str, Any] = field(default_factory=dict)   # same shape as settings.json hooks
    env: dict[str, str] = field(default_factory=dict)     # env vars applied on load

    # Sampling / temperament
    temperature: Optional[float] = None   # None = SDK default
    thinking: bool = False                # extended thinking on/off
    thinking_budget: int = 8000           # thinking token budget when enabled

    # Run mechanism
    max_iterations: int = 30              # tool-loop safety limit per turn
    auto_compact: bool = True             # auto-summarise near the context limit
    compact_threshold: Optional[float] = None  # fraction 0-1 of window (None = default)
    max_tokens: Optional[int] = None      # max output tokens (None = global default)

    # -- serialization ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentProfile":
        prof = cls()
        for f in cls.__dataclass_fields__:  # type: ignore[attr-defined]
            if f in data:
                setattr(prof, f, data[f])
        # Normalise list/dict fields against malformed JSON
        for lf in ("disabled_skills", "disabled_tools", "disabled_mcp_servers",
                   "disabled_agents", "pinned_files", "allow_rules", "deny_rules",
                   "ask_rules"):
            if not isinstance(getattr(prof, lf), list):
                setattr(prof, lf, [])
        for df in ("hooks", "env"):
            if not isinstance(getattr(prof, df), dict):
                setattr(prof, df, {})
        return prof

    def merge_over(self, base: "AgentProfile") -> "AgentProfile":
        """Return a copy of `base` overlaid with this profile's own (non-default) values."""
        from dataclasses import fields
        result = AgentProfile.from_dict(base.to_dict())
        default = AgentProfile()
        for f in fields(self):
            val = getattr(self, f.name)
            # Skip identity/link fields and values still at their dataclass default
            if f.name in ("name", "description", "extends"):
                setattr(result, f.name, getattr(self, f.name))
                continue
            if val != getattr(default, f.name):
                setattr(result, f.name, val)
        return result

    def summary_lines(self) -> list[str]:
        """Human-readable description of the active configuration."""
        sp = self.system_prompt_mode
        if sp == "append" and self.system_prompt_extra:
            sp = f"append (+{len(self.system_prompt_extra)} chars)"
        elif sp == "override" and self.system_prompt_override:
            sp = f"override ({len(self.system_prompt_override)} chars)"
        if self.thinking:
            think = f"on (budget {self.thinking_budget})"
        else:
            think = "off"
        lines = [
            f"name            {self.name}",
            f"description     {self.description or '(none)'}",
        ]
        if self.extends:
            lines.append(f"extends         {self.extends}")
        lines += [
            f"model           {self.model or '(global default)'}",
            f"system prompt   {sp}",
            f"style           {self.style or '(none)'}",
            f"memory mode     {self.memory_mode}",
            f"permission      {self.permission_mode}",
            f"temperature     {self.temperature if self.temperature is not None else '(default)'}",
            f"thinking        {think}",
            f"max iterations  {self.max_iterations}",
            f"auto-compact    {'on' if self.auto_compact else 'off'}"
            + (f" (@{int(self.compact_threshold*100)}%)" if self.compact_threshold else ""),
            f"max tokens      {self.max_tokens or '(global default)'}",
            f"disabled tools  {', '.join(self.disabled_tools) or '(none)'}",
            f"disabled skills {', '.join(self.disabled_skills) or '(none)'}",
            f"disabled mcp    {', '.join(self.disabled_mcp_servers) or '(none)'}",
            f"disabled agents {', '.join(self.disabled_agents) or '(none)'}",
            f"permission rules allow={len(self.allow_rules)} deny={len(self.deny_rules)} ask={len(self.ask_rules)}",
            f"pinned files    {', '.join(self.pinned_files) or '(none)'}",
            f"startup prompt  {self.startup_prompt[:60] + '...' if len(self.startup_prompt) > 60 else (self.startup_prompt or '(none)')}",
            f"profile hooks   {sum(len(v) for v in self.hooks.values()) if self.hooks else 0}",
            f"profile env     {', '.join(self.env) or '(none)'}",
        ]
        return lines


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _project_dir(cwd: str) -> Path:
    return Path(cwd) / ".claude" / PROFILE_DIR_NAME


def _user_dir() -> Path:
    return Path.home() / ".claude" / PROFILE_DIR_NAME


def _safe_name(name: str) -> str:
    name = name.strip().lstrip("/")
    # Keep it filesystem-safe and predictable
    return "".join(c for c in name if c.isalnum() or c in ("-", "_", ".")) or "profile"


def profile_path(name: str, cwd: str, *, scope: str = "project") -> Path:
    base = _user_dir() if scope == "user" else _project_dir(cwd)
    return base / f"{_safe_name(name)}.json"


def find_profile(name: str, cwd: str) -> Optional[Path]:
    """Locate a profile by name: project dir first, then user dir."""
    safe = _safe_name(name)
    for base in (_project_dir(cwd), _user_dir()):
        p = base / f"{safe}.json"
        if p.is_file():
            return p
    return None


def load_profile(name: str, cwd: str, _chain: Optional[set] = None) -> Optional[AgentProfile]:
    path = find_profile(name, cwd)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    prof = AgentProfile.from_dict(data)
    prof.name = _safe_name(name)

    # Resolve inheritance: load parent, overlay this profile's own values.
    if prof.extends:
        chain = _chain or set()
        if prof.extends not in chain:
            chain.add(_safe_name(name))
            parent = load_profile(prof.extends, cwd, chain)
            if parent is not None:
                prof = prof.merge_over(parent)
    return prof


def save_profile(profile: AgentProfile, cwd: str, *, scope: str = "project") -> str:
    """Persist a profile; returns the written path."""
    path = profile_path(profile.name, cwd, scope=scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(profile.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return str(path)


def delete_profile(name: str, cwd: str) -> Optional[str]:
    path = find_profile(name, cwd)
    if path is None:
        return None
    try:
        path.unlink()
        return str(path)
    except OSError:
        return None


def list_profiles(cwd: str) -> list[dict[str, str]]:
    """List available profiles across project and user scopes."""
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for scope, base in (("project", _project_dir(cwd)), ("user", _user_dir())):
        if not base.is_dir():
            continue
        for entry in sorted(os.listdir(base)):
            if not entry.endswith(".json"):
                continue
            name = entry[:-5]
            if name in seen:
                continue
            seen.add(name)
            desc = ""
            try:
                data = json.loads((base / entry).read_text(encoding="utf-8"))
                desc = data.get("description", "") or ""
            except (json.JSONDecodeError, OSError):
                pass
            out.append({"name": name, "scope": scope, "description": desc})
    return out
