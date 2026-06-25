"""
Session persistence, following the Claude Code transcript layout:

~/.claude/projects/<cwd-slug>/<session-id>.jsonl

Each line is a JSON object:
  {"type": "meta", "session_id": "...", "cwd": "...", "created": "..."}
  {"type": "message", "message": {"role": ..., "content": ...}, "timestamp": "..."}

Supports:
  --continue        resume the most recent session for this directory
  --resume [id]     resume a specific session (or pick interactively)
  /resume           in-REPL session picker
"""

import datetime
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Optional


def _slug(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(cwd))


def get_sessions_dir(cwd: str) -> Path:
    return Path.home() / ".claude" / "projects" / _slug(cwd)


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


class SessionStore:
    """Append-only JSONL transcript for one session."""

    def __init__(self, cwd: str, session_id: Optional[str] = None):
        self.cwd = cwd
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.dir = get_sessions_dir(cwd)
        self.path = self.dir / f"{self.session_id}.jsonl"
        self._is_new = not self.path.exists()

    def _ensure_meta(self):
        if self._is_new:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._write_line({
                "type": "meta",
                "session_id": self.session_id,
                "cwd": self.cwd,
                "created": _now(),
            })
            self._is_new = False

    def _write_line(self, obj: dict[str, Any]):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")

    def append_message(self, message: dict[str, Any]):
        """Persist one conversation message (user or assistant)."""
        try:
            self._ensure_meta()
            self._write_line({
                "type": "message",
                "message": message,
                "timestamp": _now(),
            })
        except OSError:
            pass  # persistence is best-effort; never break the conversation

    def rewrite(self, messages: list[dict[str, Any]]):
        """Replace the transcript with the given messages (after compaction)."""
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(json.dumps({
                    "type": "meta",
                    "session_id": self.session_id,
                    "cwd": self.cwd,
                    "created": _now(),
                    "compacted": True,
                }, ensure_ascii=False) + "\n")
                for msg in messages:
                    f.write(json.dumps({
                        "type": "message",
                        "message": msg,
                        "timestamp": _now(),
                    }, ensure_ascii=False, default=str) + "\n")
            self._is_new = False
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Listing and loading
# ---------------------------------------------------------------------------

def list_sessions(cwd: str, limit: int = 10) -> list[dict[str, Any]]:
    """List recent sessions for this directory, newest first."""
    sessions_dir = get_sessions_dir(cwd)
    if not sessions_dir.is_dir():
        return []

    entries = []
    for path in sessions_dir.glob("*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        entries.append((mtime, path))
    entries.sort(reverse=True)

    results = []
    for mtime, path in entries[:limit]:
        preview, count = _session_preview(path)
        results.append({
            "id": path.stem,
            "path": str(path),
            "modified": datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M"),
            "preview": preview,
            "messages": count,
        })
    return results


def _session_preview(path: Path) -> tuple[str, int]:
    """First user message text + total message count."""
    preview = ""
    count = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "message":
                    continue
                count += 1
                msg = obj.get("message", {})
                if not preview and msg.get("role") == "user":
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        preview = content.replace("\n", " ")[:60]
    except OSError:
        pass
    return preview or "(no user message)", count


def load_session(cwd: str, session_id: str) -> Optional[list[dict[str, Any]]]:
    """Load a session transcript back into a messages list."""
    path = get_sessions_dir(cwd) / f"{session_id}.jsonl"
    if not path.is_file():
        return None

    messages: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "message" and isinstance(obj.get("message"), dict):
                    messages.append(obj["message"])
    except OSError:
        return None
    return messages


def latest_session_id(cwd: str) -> Optional[str]:
    sessions = list_sessions(cwd, limit=1)
    return sessions[0]["id"] if sessions else None
