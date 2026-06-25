"""
Hooks system, following the Claude Code settings.json hooks format.

Supported events:
- SessionStart      - fired once when the REPL starts; stdout is added as context
- UserPromptSubmit  - fired before a user prompt is sent; can block (exit 2)
- PreToolUse        - fired before a tool executes; can block (exit 2)
- PostToolUse       - fired after a tool executes; feedback shown to the user
- Stop              - fired when the assistant finishes a turn; can block (exit 2)
                      to force the conversation to continue with the feedback

Hook commands receive a JSON payload on stdin:
  {"hook_event_name": "...", "cwd": "...", "session_id": "...",
   "tool_name": "...", "tool_input": {...}, "tool_response": "..."}

Exit code semantics (matching Claude Code):
  0  - success; stdout may be used as context (SessionStart / UserPromptSubmit)
  2  - block the action; stderr is the reason fed back
  *  - non-blocking error; shown to the user

A hook may also print JSON to stdout: {"decision": "block", "reason": "..."}.
"""

import json
import re
import subprocess
from typing import Any, Optional

HOOK_EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]

DEFAULT_HOOK_TIMEOUT = 60  # seconds


class HookResult:
    """Aggregated result of all hooks for one event."""

    def __init__(self):
        self.blocked = False
        self.reasons: list[str] = []     # block reasons (stderr / JSON reason)
        self.outputs: list[str] = []     # stdout from successful hooks
        self.errors: list[str] = []      # non-blocking hook failures

    @property
    def block_reason(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "Blocked by hook"

    @property
    def context(self) -> str:
        return "\n".join(o for o in self.outputs if o.strip())


class HookRunner:
    """Executes hooks configured in settings.json."""

    def __init__(self, hooks_config: dict[str, list[dict[str, Any]]], cwd: str,
                 session_id: str = ""):
        self.config = hooks_config or {}
        self.cwd = cwd
        self.session_id = session_id

    def has_hooks(self, event: str) -> bool:
        return bool(self.config.get(event))

    def count(self) -> int:
        return sum(
            len(m.get("hooks", []))
            for matchers in self.config.values()
            for m in matchers
        )

    def describe(self) -> list[str]:
        """Human-readable listing of configured hooks."""
        lines = []
        for event, matchers in self.config.items():
            for m in matchers:
                matcher = m.get("matcher", "") or "*"
                for h in m.get("hooks", []):
                    lines.append(f"{event} [{matcher}]: {h.get('command', '')}")
        return lines

    def run(self, event: str, *, tool_name: str = "", tool_input: Any = None,
            extra: Optional[dict[str, Any]] = None) -> HookResult:
        """Run all hooks for an event. Returns aggregated result."""
        result = HookResult()
        matchers = self.config.get(event, [])
        if not matchers:
            return result

        payload: dict[str, Any] = {
            "hook_event_name": event,
            "cwd": self.cwd,
            "session_id": self.session_id,
        }
        if tool_name:
            payload["tool_name"] = tool_name
        if tool_input is not None:
            payload["tool_input"] = tool_input
        if extra:
            payload.update(extra)

        payload_json = json.dumps(payload, ensure_ascii=False, default=str)

        for matcher_block in matchers:
            matcher = matcher_block.get("matcher", "")
            if not self._matcher_matches(matcher, tool_name):
                continue
            for hook in matcher_block.get("hooks", []):
                if hook.get("type", "command") != "command":
                    continue
                command = hook.get("command", "")
                if not command:
                    continue
                timeout = hook.get("timeout", DEFAULT_HOOK_TIMEOUT)
                self._run_one(command, payload_json, timeout, result)

        return result

    def _matcher_matches(self, matcher: str, tool_name: str) -> bool:
        """Empty matcher or '*' matches everything; otherwise regex on tool name."""
        if not matcher or matcher == "*":
            return True
        if not tool_name:
            return True  # non-tool events (Stop, UserPromptSubmit) ignore matchers
        try:
            return re.fullmatch(matcher, tool_name) is not None
        except re.error:
            return matcher == tool_name

    def _run_one(self, command: str, payload_json: str, timeout: int, result: HookResult):
        try:
            proc = subprocess.run(
                command,
                shell=True,
                input=payload_json,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=self.cwd,
            )
        except subprocess.TimeoutExpired:
            result.errors.append(f"Hook timed out after {timeout}s: {command}")
            return
        except Exception as e:
            result.errors.append(f"Hook failed to run ({command}): {e}")
            return

        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()

        # JSON decision output takes precedence
        if stdout.startswith("{"):
            try:
                data = json.loads(stdout)
                if isinstance(data, dict) and data.get("decision") == "block":
                    result.blocked = True
                    result.reasons.append(data.get("reason", "Blocked by hook"))
                    return
            except json.JSONDecodeError:
                pass

        if proc.returncode == 2:
            result.blocked = True
            result.reasons.append(stderr or "Blocked by hook")
        elif proc.returncode != 0:
            result.errors.append(stderr or f"Hook exited with code {proc.returncode}")
        else:
            if stdout:
                result.outputs.append(stdout)
