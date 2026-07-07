"""Configuration management for Open Claude."""

import json
import os
import platform
import re
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
    """Get the Anthropic API key (kept for backward compatibility)."""
    return get_api_key_for("anthropic")


# ---------------------------------------------------------------------------
# Provider registry
#
# Every model belongs to a provider. Anthropic uses the native SDK; the others
# all expose an OpenAI-compatible Chat Completions API, so they share one code
# path (see openai_compat.py) and differ only by base_url + API key env var.
#
# Each provider's API key is read from (in order): its env var(s), then the
# config.json keys map  {"api_keys": {"qwen": "sk-..."}}.  The base_url can be
# overridden per provider with  <PROVIDER>_BASE_URL  (e.g. QWEN_BASE_URL).
# ---------------------------------------------------------------------------

PROVIDERS: dict[str, dict[str, Any]] = {
    "anthropic": {
        "label": "Anthropic",
        "env": ["ANTHROPIC_API_KEY"],
        "base_url": None,  # native SDK
    },
    "openai": {
        "label": "OpenAI",
        "env": ["OPENAI_API_KEY"],
        "base_url": "https://api.openai.com/v1",
    },
    "qwen": {
        "label": "Qwen (Alibaba DashScope)",
        "env": ["DASHSCOPE_API_KEY", "QWEN_API_KEY"],
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    },
    "glm": {
        "label": "Zhipu GLM",
        "env": ["ZHIPUAI_API_KEY", "GLM_API_KEY"],
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
    },
    "moonshot": {
        "label": "Moonshot (Kimi)",
        "env": ["MOONSHOT_API_KEY", "KIMI_API_KEY"],
        "base_url": "https://api.moonshot.cn/v1",
    },
    "deepseek": {
        "label": "DeepSeek",
        "env": ["DEEPSEEK_API_KEY"],
        "base_url": "https://api.deepseek.com/v1",
    },
}


# ---------------------------------------------------------------------------
# Model registry: friendly names -> canonical API model IDs
# ---------------------------------------------------------------------------

# Ordered, newest/most-capable first. The first entry is the default.
#
# NOTE: the non-Anthropic ids below are the names requested by the user. Some
# providers may expect a slightly different exact model id — override the "id"
# field here (or pass --model <exact-id>) if the provider rejects it.
AVAILABLE_MODELS: list[dict[str, Any]] = [
    # --- Anthropic ---
    {"id": "claude-opus-4-8", "label": "Claude Opus 4.8", "provider": "anthropic",
     "aliases": ["opus", "opus-4.8", "opus4.8", "opus-4-8"]},
    {"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6", "provider": "anthropic",
     "aliases": ["sonnet", "sonnet-4.6", "sonnet4.6", "sonnet-4-6"]},
    {"id": "claude-opus-4-7", "label": "Claude Opus 4.7", "provider": "anthropic",
     "aliases": ["opus-4.7", "opus4.7", "opus-4-7"]},
    {"id": "claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5", "provider": "anthropic",
     "aliases": ["haiku", "haiku-4.5", "haiku4.5", "haiku-4-5"]},
    # --- OpenAI ---
    {"id": "gpt-5.5", "label": "GPT-5.5", "provider": "openai",
     "aliases": ["gpt", "gpt5.5", "gpt-5-5"]},
    # --- Qwen (DashScope) ---
    {"id": "qwen3.7-max", "label": "Qwen3.7-Max", "provider": "qwen",
     "aliases": ["qwen", "qwen-max", "qwen3.7max", "qwen-3.7-max"]},
    {"id": "qwen3.7-plus", "label": "Qwen3.7-Plus", "provider": "qwen",
     "aliases": ["qwen-plus", "qwen3.7plus", "qwen-3.7-plus"]},
    {"id": "qwen3.5-plus", "label": "Qwen3.5-Plus", "provider": "qwen",
     "aliases": ["qwen3.5plus", "qwen-3.5-plus"]},
    # --- Zhipu GLM ---
    {"id": "glm-5.2", "label": "GLM-5.2", "provider": "glm",
     "aliases": ["glm", "glm5.2", "glm-5-2"]},
    {"id": "glm-5.1", "label": "GLM-5.1", "provider": "glm",
     "aliases": ["glm5.1", "glm-5-1"]},
    # --- Moonshot (Kimi) ---
    {"id": "kimi-k2.6", "label": "Kimi K2.6", "provider": "moonshot",
     "aliases": ["kimi", "k2.6", "kimi-k2.6", "kimi-k2-6"]},
    # --- DeepSeek ---
    {"id": "deepseek-v4-pro", "label": "DeepSeek-V4-Pro", "provider": "deepseek",
     "aliases": ["deepseek", "deepseek-v4-pro", "v4-pro"]},
    {"id": "deepseek-v4-flash", "label": "DeepSeek-V4-Flash", "provider": "deepseek",
     "aliases": ["deepseek-flash", "v4-flash"]},
]

DEFAULT_MODEL = AVAILABLE_MODELS[0]["id"]

# Build the alias lookup: alias/id (lowercased) -> canonical id
_MODEL_ALIASES: dict[str, str] = {}
# id -> provider
_MODEL_PROVIDERS: dict[str, str] = {}
for _m in AVAILABLE_MODELS:
    _MODEL_ALIASES[_m["id"].lower()] = _m["id"]
    _MODEL_PROVIDERS[_m["id"]] = _m.get("provider", "anthropic")
    for _a in _m["aliases"]:
        _MODEL_ALIASES.setdefault(_a.lower(), _m["id"])


def get_model_provider(model_id: Optional[str]) -> str:
    """Return the provider for a model id (best-effort prefix inference)."""
    if not model_id:
        return "anthropic"
    mid = model_id.strip()
    if mid in _MODEL_PROVIDERS:
        return _MODEL_PROVIDERS[mid]
    low = mid.lower()
    if low.startswith("claude"):
        return "anthropic"
    if low.startswith(("gpt", "o1", "o3", "o4")):
        return "openai"
    if low.startswith("qwen"):
        return "qwen"
    if low.startswith("glm"):
        return "glm"
    if low.startswith(("kimi", "moonshot")):
        return "moonshot"
    if low.startswith("deepseek"):
        return "deepseek"
    return "anthropic"


def get_provider_base_url(provider: str) -> Optional[str]:
    """Base URL for an OpenAI-compatible provider (env override supported)."""
    override = os.environ.get(provider.upper() + "_BASE_URL")
    if override:
        return override
    return PROVIDERS.get(provider, {}).get("base_url")


def get_api_key_for(provider: str) -> Optional[str]:
    """Resolve the API key for a provider from env vars, then config.json."""
    spec = PROVIDERS.get(provider)
    if not spec:
        return None
    for env in spec.get("env", []):
        val = os.environ.get(env)
        if val:
            return val
    cfg = load_config()
    keys = cfg.get("api_keys", {})
    if isinstance(keys, dict) and keys.get(provider):
        return keys[provider]
    # 兼容顶层扁平写法:直接用环境变量名的小写形式作 key(如 dashscope_api_key)
    for env in spec.get("env", []):
        val = cfg.get(env.lower())
        if val:
            return val
    if provider == "anthropic" and cfg.get("api_key"):
        return cfg["api_key"]
    return None


def resolve_model(name: Optional[str]) -> Optional[str]:
    """Map a friendly model name/alias to its canonical API ID.

    Unknown names are returned unchanged so users can still pass any raw
    model ID (e.g. a dated snapshot we don't list).
    """
    if not name:
        return name
    name = name.strip()
    # Claude Code settings may append a context-window marker (e.g.
    # "claude-fable-5[1m]") that the raw API rejects — strip it.
    name = re.sub(r"\[[^\]]*\]$", "", name).strip()
    resolved = _MODEL_ALIASES.get(name.lower(), name)
    # Fable-tier ids are Claude Code-session models, not served to this API
    # key — never call them here; fall back to the default model instead.
    if resolved.lower().startswith("claude-fable"):
        return DEFAULT_MODEL
    return resolved


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
    return 32768


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
