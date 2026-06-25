"""
Multi-layer settings.json support, following the Claude Code layout:

1. User:    ~/.claude/settings.json
2. Project: .claude/settings.json        (checked into git)
3. Local:   .claude/settings.local.json  (gitignored, personal)

Later layers take precedence. Supported keys:

{
  "model": "claude-sonnet-4-5",
  "env": {"FOO": "bar"},
  "permissions": {
    "allow": ["Bash(git:*)", "Read"],
    "deny":  ["Read(./.env)"],
    "ask":   ["Bash(rm:*)"]
  },
  "hooks": {
    "PreToolUse": [
      {"matcher": "Bash", "hooks": [{"type": "command", "command": "...", "timeout": 60}]}
    ]
  }
}
"""

import fnmatch
import json
import os
from pathlib import Path
from typing import Any, Optional


class Settings:
    """Merged view of all settings layers."""

    def __init__(self):
        self.allow_rules: list[str] = []
        self.deny_rules: list[str] = []
        self.ask_rules: list[str] = []
        self.env: dict[str, str] = {}
        self.hooks: dict[str, list[dict[str, Any]]] = {}
        self.model: Optional[str] = None
        self.sources: list[str] = []  # paths of loaded settings files


def _settings_paths(cwd: str) -> list[tuple[str, Path]]:
    return [
        ("user", Path.home() / ".claude" / "settings.json"),
        ("project", Path(cwd) / ".claude" / "settings.json"),
        ("local", Path(cwd) / ".claude" / "settings.local.json"),
    ]


def load_settings(cwd: str) -> Settings:
    """Load and merge all settings layers (later layers win)."""
    settings = Settings()

    for _scope, path in _settings_paths(cwd):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue

        settings.sources.append(str(path))

        perms = data.get("permissions", {})
        if isinstance(perms, dict):
            settings.allow_rules.extend(perms.get("allow", []) or [])
            settings.deny_rules.extend(perms.get("deny", []) or [])
            settings.ask_rules.extend(perms.get("ask", []) or [])

        env = data.get("env", {})
        if isinstance(env, dict):
            settings.env.update({str(k): str(v) for k, v in env.items()})

        hooks = data.get("hooks", {})
        if isinstance(hooks, dict):
            for event, matchers in hooks.items():
                if isinstance(matchers, list):
                    settings.hooks.setdefault(event, []).extend(matchers)

        if data.get("model"):
            settings.model = data["model"]

    return settings


def apply_env(settings: Settings):
    """Inject settings env vars into the process environment."""
    for k, v in settings.env.items():
        os.environ.setdefault(k, v)


# ---------------------------------------------------------------------------
# Permission rule matching
# ---------------------------------------------------------------------------

def parse_rule(rule: str) -> tuple[str, Optional[str]]:
    """'Bash(git:*)' -> ('Bash', 'git:*');  'Read' -> ('Read', None)"""
    rule = rule.strip()
    if rule.endswith(")") and "(" in rule:
        idx = rule.index("(")
        return rule[:idx], rule[idx + 1 : -1]
    return rule, None


def rule_matches(rule: str, tool_name: str, tool_input: dict[str, Any]) -> bool:
    """Check whether a permission rule matches a concrete tool call."""
    name, spec = parse_rule(rule)

    # Tool name match. "mcp__server" also matches "mcp__server__tool".
    if name != "*" and name != tool_name:
        if not (name.startswith("mcp__") and tool_name.startswith(name + "__")):
            return False

    if spec is None or spec == "*":
        return True

    if tool_name == "Bash":
        command = (tool_input.get("command") or "").strip()
        if spec.endswith(":*"):
            prefix = spec[:-2]
            return command.startswith(prefix)
        return command == spec

    # File tools: glob match against the path argument
    path = tool_input.get("file_path") or tool_input.get("path") or ""
    if path:
        if fnmatch.fnmatch(path, spec):
            return True
        # Also try relative form (rules are often written as ./src/**)
        norm = path.replace("\\", "/")
        return fnmatch.fnmatch(norm, spec) or fnmatch.fnmatch(norm, spec.lstrip("./"))

    return False


def match_any(rules: list[str], tool_name: str, tool_input: dict[str, Any]) -> Optional[str]:
    """Return the first matching rule, or None."""
    for rule in rules:
        if rule_matches(rule, tool_name, tool_input):
            return rule
    return None


def suggest_rule(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Suggest a persistent allow-rule for a tool call (like Claude Code does)."""
    if tool_name == "Bash":
        command = (tool_input.get("command") or "").strip()
        first_word = command.split()[0] if command.split() else ""
        if first_word:
            return f"Bash({first_word}:*)"
        return "Bash"
    return tool_name


def write_local_allow_rule(cwd: str, rule: str) -> str:
    """Persist an allow rule to .claude/settings.local.json. Returns the file path."""
    path = Path(cwd) / ".claude" / "settings.local.json"
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}

    perms = data.setdefault("permissions", {})
    allow = perms.setdefault("allow", [])
    if rule not in allow:
        allow.append(rule)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return str(path)
