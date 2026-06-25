"""Configuration management for Open Claude."""

import json
import os
import platform
from pathlib import Path
from typing import Any, Optional


def get_home_dir() -> Path:
    return Path.home()


def get_claude_dir() -> Path:
    d = get_home_dir() / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_config_path() -> Path:
    return get_claude_dir() / "config.json"


def load_config() -> dict[str, Any]:
    path = get_config_path()
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def get_api_key() -> Optional[str]:
    """Get API key from env var or config file."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    cfg = load_config()
    return cfg.get("api_key")


# ---------------------------------------------------------------------------
# Model registry: friendly names -> canonical API model IDs
# ---------------------------------------------------------------------------

# Ordered, newest/most-capable first. The first entry is the default.
AVAILABLE_MODELS: list[dict[str, Any]] = [
    {"id": "claude-opus-4-8", "label": "Opus 4.8",
     "aliases": ["opus", "opus-4.8", "opus4.8", "opus-4-8"]},
    {"id": "claude-opus-4-7", "label": "Opus 4.7",
     "aliases": ["opus-4.7", "opus4.7", "opus-4-7"]},
    {"id": "claude-sonnet-4-6", "label": "Sonnet 4.6",
     "aliases": ["sonnet", "sonnet-4.6", "sonnet4.6", "sonnet-4-6"]},
    {"id": "claude-haiku-4-5-20251001", "label": "Haiku 4.5",
     "aliases": ["haiku", "haiku-4.5", "haiku4.5", "haiku-4-5"]},
]

DEFAULT_MODEL = AVAILABLE_MODELS[0]["id"]

# Build the alias lookup: alias/id (lowercased) -> canonical id
_MODEL_ALIASES: dict[str, str] = {}
for _m in AVAILABLE_MODELS:
    _MODEL_ALIASES[_m["id"].lower()] = _m["id"]
    for _a in _m["aliases"]:
        _MODEL_ALIASES[_a.lower()] = _m["id"]


def resolve_model(name: Optional[str]) -> Optional[str]:
    """Map a friendly model name/alias to its canonical API ID.

    Unknown names are returned unchanged so users can still pass any raw
    model ID (e.g. a dated snapshot we don't list).
    """
    if not name:
        return name
    return _MODEL_ALIASES.get(name.strip().lower(), name.strip())


def get_model() -> str:
    """Get the canonical model ID from env var or config, else the default."""
    model = os.environ.get("CLAUDE_MODEL") or os.environ.get("ANTHROPIC_MODEL")
    if not model:
        model = load_config().get("model")
    return resolve_model(model) or DEFAULT_MODEL


def get_max_tokens() -> int:
    val = os.environ.get("CLAUDE_MAX_TOKENS")
    if val:
        return int(val)
    return 16384


def get_environment_info() -> dict[str, str]:
    """Gather environment info for system prompt."""
    cwd = os.getcwd()
    system = platform.system()
    release = platform.release()
    is_git = os.path.isdir(os.path.join(cwd, ".git"))

    # Detect shell
    shell = os.environ.get("SHELL", "")
    if not shell:
        shell = "powershell" if system == "Windows" else "bash"

    return {
        "cwd": cwd,
        "platform": system.lower(),
        "os_version": f"{system} {release}",
        "shell": os.path.basename(shell) if "/" in shell or "\\" in shell else shell,
        "is_git_repo": str(is_git).lower(),
    }
