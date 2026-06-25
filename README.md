# Open Claude

An open-source AI coding assistant CLI, powered by Anthropic's Claude API. Inspired by [Claude Code](https://docs.anthropic.com/en/docs/claude-code), built from scratch in Python.

## Features

- **Editable, reusable agent** - reshape the agent at runtime across every axis — model, system prompt & output style, memory mode, pinned context, startup action, tool surface, skills, MCP servers, permission mode & rules, sampling (temperature / extended thinking), run mechanism, env vars and hooks — then `/profile save <name>` to bundle it all into a named, inheritable profile you can `/profile load` or launch with `--profile`
- **Rich TUI** - live streaming Markdown rendering, colored diff previews for edits, syntax-highlighted file previews, bottom status bar (model / context / cost / MCP), Tab completion for `/commands` and `@file` paths, persistent input history
- **12 built-in tools**: Bash, Read, Write, Edit, Glob, Grep, Skill, TaskCreate, TaskUpdate, TaskList, TaskGet, Agent
- **MCP support** - connect stdio MCP servers via `.mcp.json` / `~/.claude/mcp.json`; their tools appear as `mcp__server__tool`
- **Hooks** - `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop` hooks configured in settings.json (exit code 2 blocks the action)
- **Multi-layer settings** - `~/.claude/settings.json` → `.claude/settings.json` → `.claude/settings.local.json` with permission rules like `Bash(git:*)`
- **Permission system** - allow/deny/ask rules from settings, interactive approval menu, one-keystroke persist to `.claude/settings.local.json`
- **Session persistence** - transcripts saved to `~/.claude/projects/`; resume with `--continue`, `--resume`, or `/resume`
- **Prompt caching** - cache breakpoints on system prompt, tools, and conversation prefix (up to ~90% input cost savings on agentic loops)
- **Sub-agent system** - built-in `general-purpose` and `explore` types plus custom agents from `.claude/agents/*.md`
- **Skills (slash commands)** - bundled (`/commit`, `/review`, `/test`, `/fix`, `/explain`, `/simplify`) and user-defined
- **CLAUDE.md support** - project instructions via `CLAUDE.md`, `.claude/CLAUDE.md`, `.claude/rules/*.md`
- **Conversation compaction** - auto-summarizes when approaching context window limit
- **Token tracking & cost display** - per-model pricing, cache-aware cost, context usage monitoring
- **Task management** - track multi-step work with in-memory task system
- **Cross-platform** - Windows (Git Bash / PowerShell / cmd fallback), macOS, Linux

## Prerequisites

- **Python 3.10+** ([download](https://www.python.org/downloads/))
- **Anthropic API Key** - get one at https://console.anthropic.com/
- **Git** (optional, for `/commit`, `/review` and other git-related skills)

## Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/YOUR_USERNAME/open-claude.git
cd open-claude

# 2. (Recommended) Create a virtual environment
python -m venv .venv

# Activate it:
#   Windows (PowerShell):
.venv\Scripts\Activate.ps1
#   Windows (cmd):
.venv\Scripts\activate.bat
#   macOS / Linux:
source .venv/bin/activate

# 3. Install open-claude and all dependencies
#    This installs the `open-claude` command to your PATH
#    so you can run it from anywhere.
pip install -e .

# 4. Set your Anthropic API key
#   Windows (PowerShell):
$env:ANTHROPIC_API_KEY = "sk-ant-your-key-here"
#   Windows (cmd):
set ANTHROPIC_API_KEY=sk-ant-your-key-here
#   macOS / Linux:
export ANTHROPIC_API_KEY="sk-ant-your-key-here"

# 5. Run!
open-claude
```

> **Tip**: To avoid setting the API key every time, add the `export` / `$env:` line to your shell profile (`~/.bashrc`, `~/.zshrc`, or PowerShell `$PROFILE`), or save it in the config file (see below).

### What does `pip install -e .` do?

It reads `pyproject.toml` and:

1. Installs dependencies: `anthropic`, `rich`, `prompt_toolkit`
2. Registers the `open-claude` command on your PATH (via `[project.scripts]`)
3. `-e` (editable) means changes to the source code take effect immediately without reinstalling

After this step, you can run `open-claude` from any directory.

### Alternative: Run without installing

If you don't want to `pip install`, install dependencies manually and run as a Python module:

```bash
pip install anthropic rich prompt_toolkit
python -m open_claude

# or
python run.py
```

Note: this way you won't have the `open-claude` command, you'll need to use `python -m open_claude` every time.

## Configuration

### API Key

You have two options:

**Option A** - Environment variable (recommended):

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

**Option B** - Config file at `~/.claude/config.json`:

```json
{
  "api_key": "sk-ant-...",
  "model": "opus"
}
```

### Models & providers

Open Claude is multi-provider. Anthropic models use the native SDK; every other
provider (Qwen, GLM, Kimi, DeepSeek, OpenAI) speaks the OpenAI-compatible Chat
Completions API and is handled by a single adapter. Switch models with
`/model <name>` in the REPL (run `/model` with no argument to see the list, each
model's provider, and whether its key is configured), the `--model` flag, or the
`CLAUDE_MODEL` env var. Friendly aliases resolve to canonical API IDs, so `opus`
works as well as `claude-opus-4-8`:

| Alias | Model | Provider | API ID |
|---|---|---|---|
| `opus` | Claude Opus 4.8 (default) | Anthropic | `claude-opus-4-8` |
| `sonnet` | Claude Sonnet 4.6 | Anthropic | `claude-sonnet-4-6` |
| `opus-4.7` | Claude Opus 4.7 | Anthropic | `claude-opus-4-7` |
| `haiku` | Claude Haiku 4.5 | Anthropic | `claude-haiku-4-5-20251001` |
| `gpt` | GPT-5.5 | OpenAI | `gpt-5.5` |
| `qwen` | Qwen3.7-Max | Qwen (DashScope) | `qwen3.7-max` |
| `qwen-plus` | Qwen3.7-Plus | Qwen (DashScope) | `qwen3.7-plus` |
| `qwen3.5-plus` | Qwen3.5-Plus | Qwen (DashScope) | `qwen3.5-plus` |
| `glm` | GLM-5.2 | Zhipu GLM | `glm-5.2` |
| `glm5.1` | GLM-5.1 | Zhipu GLM | `glm-5.1` |
| `kimi` | Kimi K2.6 | Moonshot | `kimi-k2.6` |
| `deepseek` | DeepSeek-V4-Pro | DeepSeek | `deepseek-v4-pro` |
| `deepseek-flash` | DeepSeek-V4-Flash | DeepSeek | `deepseek-v4-flash` |

Any unrecognized value is passed through unchanged, so you can still use a dated
snapshot ID or a provider-specific model name directly.

**Non-Anthropic models need the `openai` package** (an optional dependency):

```bash
pip install "open-claude[openai]"   # or: pip install openai
```

Each provider reads its key from its own env var(s), or from an `api_keys` map in
`~/.claude/config.json`. Base URLs can be overridden with `<PROVIDER>_BASE_URL`
(e.g. `QWEN_BASE_URL`):

| Provider | API-key env var(s) | Default base URL |
|---|---|---|
| Anthropic | `ANTHROPIC_API_KEY` | (native SDK) |
| OpenAI | `OPENAI_API_KEY` | `https://api.openai.com/v1` |
| Qwen | `DASHSCOPE_API_KEY` / `QWEN_API_KEY` | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| Zhipu GLM | `ZHIPUAI_API_KEY` / `GLM_API_KEY` | `https://open.bigmodel.cn/api/paas/v4` |
| Moonshot (Kimi) | `MOONSHOT_API_KEY` / `KIMI_API_KEY` | `https://api.moonshot.cn/v1` |
| DeepSeek | `DEEPSEEK_API_KEY` | `https://api.deepseek.com/v1` |

```json
{
  "model": "qwen",
  "api_keys": {
    "qwen": "sk-...",
    "glm": "...",
    "deepseek": "sk-..."
  }
}
```

> The non-Anthropic model IDs above are best-effort defaults. If a provider
> rejects an ID, pass the exact one with `--model <id>` or set the matching
> `id` in `AVAILABLE_MODELS`.

### Environment Variables

| Variable | Description | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key | (required for Claude models) |
| `OPENAI_API_KEY` | OpenAI key (GPT models) | — |
| `DASHSCOPE_API_KEY` / `QWEN_API_KEY` | Qwen key | — |
| `ZHIPUAI_API_KEY` / `GLM_API_KEY` | Zhipu GLM key | — |
| `MOONSHOT_API_KEY` / `KIMI_API_KEY` | Moonshot (Kimi) key | — |
| `DEEPSEEK_API_KEY` | DeepSeek key | — |
| `<PROVIDER>_BASE_URL` | Override a provider's base URL | (per-provider default) |
| `CLAUDE_MODEL` | Model to use (alias or full ID) | `claude-opus-4-8` |
| `CLAUDE_MAX_TOKENS` | Max output tokens | `16384` |

## Usage

### Interactive Mode (REPL)

```bash
open-claude
```

This opens an interactive chat session. Type your message, press Enter, and the assistant will respond with streaming output. It can read/write files, run shell commands, search your codebase, and more.

### Single Prompt Mode

```bash
# Run one prompt and exit (useful for scripting)
open-claude -p "explain the main function in src/app.py"
```

### Web UI (browser front-end)

A GPT/Claude-style chat UI that drives the **same** agent — streaming replies,
live tool-call cards (Bash/Read/Write/Edit/Glob/Grep/Skill/Agent/MCP), model
switching, and the active profile. It's a thin adapter (`oc_web_server.py` +
`generic_claude_gpt_style_chat.html`) that imports the engine without modifying
the `open_claude` package.

```bash
python oc_web_server.py            # serves http://127.0.0.1:47291/
python oc_web_server.py --cwd path/to/project --profile researcher --port 47291
```

The web session runs non-interactively (auto-approves tools, like
`--dangerously-skip-permissions`), since there is no terminal to answer
permission prompts; deny rules in settings/profiles are still honored.

### CLI Options

```
open-claude [OPTIONS]

Options:
  -v, --version                 Show version and exit
  -p, --prompt TEXT             Run a single prompt and exit
  --model MODEL                 Model override (e.g. claude-opus-4-20250514)
  --cwd PATH                    Set working directory
  --dangerously-skip-permissions  Auto-approve all tool executions
  -c, --continue                Continue the most recent session for this directory
  --resume [SESSION_ID]         Resume a session (omit the ID to pick from a list)
  --profile NAME                Start with a saved agent profile
```

### REPL Commands

| Command | Description |
|---|---|
| `/help` | Show help |
| `/clear` | Clear conversation history (starts a new session) |
| `/compact` | Manually compact conversation |
| `/cost` | Show token usage, cache stats, and cost |
| `/model [name]` | Show or switch the current model |
| `/tasks` | Show current tasks |
| `/skills` | List all available skills |
| `/skill <enable\|disable\|reload> [name]` | Toggle a skill on/off, or rescan skills from disk |
| `/tool [list\|disable A B\|enable A B\|only A B C]` | Control which base tools the agent can use |
| `/agents` | List available subagent types |
| `/mcp [status\|enable <name>\|disable <name>]` | MCP server status, or hide a server for this agent |
| `/hooks` | Show configured hooks (settings + profile) |
| `/hook [list\|add <event> <matcher> <cmd>\|clear]` | Add/clear profile-carried hooks |
| `/config` | Show loaded settings files |
| `/prompt [show\|append\|set\|reset]` | View/edit the system prompt (append to or fully override the default) |
| `/style [show\|set <text>\|clear]` | Set a short output-style instruction (always appended) |
| `/memory [show\|mode <full\|project\|summary\|off>\|reload]` | Change how CLAUDE.md memory is injected |
| `/context [list\|add <file>\|remove <file>\|clear]` | Pin files that are always attached to the prompt |
| `/onstart [show\|set <prompt>\|clear]` | A prompt that auto-runs once when a fresh session starts |
| `/env [list\|set KEY=VAL\|unset KEY]` | Profile-carried environment variables |
| `/runtime [max-iterations\|auto-compact\|max-tokens\|temperature\|thinking\|compact-threshold]` | Tune the run mechanism and sampling |
| `/permission [mode\|allow <rule>\|deny <rule>\|ask <rule>\|clear]` | Permission mode, plus profile-carried allow/deny/ask rules |
| `/profile [show\|list\|save <name>\|load <name>\|new <name>\|delete <name>]` | Save/load reusable agent profiles |
| `/resume` | Resume a previous session |
| `quit` | Exit |

In the input box, **Tab** completes `/commands` and `@file` paths. Writing
`@path/to/file` in a message attaches that file's contents for the model.

### Editable, reusable agents

Open Claude can be reshaped at runtime and saved as a named **profile** — a
bundle of everything that defines an agent persona:

| Dimension | Command | Notes |
|---|---|---|
| Model | `/model opus` | Per-profile model |
| System prompt | `/prompt append` / `/prompt set` | `append` adds to the default prompt; `set` replaces it entirely (the live environment block is still injected) |
| Output style | `/style set Answer in Chinese.` | Short instruction always appended to the prompt |
| Memory mode | `/memory mode summary` | `full` (default), `project` (skip user/global CLAUDE.md), `summary` (headers only), `off` |
| Pinned context | `/context add docs/architecture.md` | Files always attached to the prompt |
| Startup action | `/onstart set "Read the README, then wait."` | Auto-runs once on a fresh session |
| Skills | `/skill disable commit` | Hide skills from both the model and the user; `/skill reload` rescans disk |
| Tool surface | `/tool only Read Glob Grep` | Choose exactly which base tools exist (e.g. a read-only agent) |
| MCP | `/mcp disable postgres` | Hide an MCP server for this agent |
| Permissions | `/permission deny_all`, `/permission ask Bash(rm:*)` | Mode **and** profile-carried allow/deny/ask rules |
| Sampling | `/runtime temperature 0.2`, `/runtime thinking on 8000` | Temperature, or extended-thinking budget (mutually exclusive) |
| Run mechanism | `/runtime max-iterations 50`, `/runtime auto-compact off`, `/runtime compact-threshold 0.8`, `/runtime max-tokens 8192` | Tool-loop limit, auto-compaction + threshold, max output tokens |
| Env vars | `/env set DEBUG=1` | Profile-carried environment variables |
| Hooks | `/hook add PreToolUse Bash "npm run lint"` | Profile-carried guardrails (merged with settings.json hooks) |
| Inheritance | `"extends": "base"` in the JSON | A profile inherits a parent's fields and overrides only what it sets |

A few ready-made personas this enables:

| Persona | How |
|---|---|
| Read-only researcher | `/tool only Read Glob Grep` + `/runtime temperature 0` |
| Cautious engineer | `/permission ask Bash(rm:*)` + `/runtime thinking on` |
| Codebase expert | `/context add docs/*.md` + `/onstart "Skim the architecture docs."` |
| Creative writer | `/tool disable Bash Write Edit` + `/runtime temperature 0.9` |

Then snapshot the live state into a profile:

```text
/profile save researcher          # writes .claude/oc-profiles/researcher.json
/profile save researcher --user   # or ~/.claude/oc-profiles/ (all projects)
/profile list
/profile load researcher
/profile show
```

Or launch straight into one:

```bash
open-claude --profile researcher
```

Profiles are plain JSON, so you can check them into a repo and share an agent
configuration with your team. Project profiles (`.claude/oc-profiles/`) take
precedence over user profiles (`~/.claude/oc-profiles/`).

### Built-in Skills

| Skill | Description |
|---|---|
| `/commit` | Generate a git commit from current changes |
| `/review` | Review code changes or a pull request |
| `/test` | Find and run the project's tests |
| `/fix` | Diagnose and fix a bug or error |
| `/explain` | Explain code or a file |
| `/simplify` | Review and simplify changed code |

### Custom Skills

Create your own skills by adding `.md` files to:

- `~/.claude/skills/` (global, personal)
- `.claude/skills/` (project-specific)

Example skill file (`.claude/skills/deploy/SKILL.md`):

```markdown
---
description: Deploy to production
user-invocable: true
argument-hint: <environment>
---

Deploy the application to the specified environment.
1. Run tests first
2. Build the project
3. Deploy using the project's deploy script
```

### Settings (settings.json)

Settings are merged from three layers (later wins):

1. `~/.claude/settings.json` (user)
2. `.claude/settings.json` (project, checked in)
3. `.claude/settings.local.json` (personal, gitignored)

```json
{
  "model": "claude-sonnet-4-5",
  "env": {"MY_VAR": "value"},
  "permissions": {
    "allow": ["Bash(git:*)", "Bash(npm:*)"],
    "deny":  ["Read(./.env)"],
    "ask":   ["Bash(rm:*)"]
  },
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "python check_command.py", "timeout": 30}]
      }
    ]
  }
}
```

Permission rules: `Bash(git:*)` matches commands starting with `git`; `Read(*.env)`
glob-matches file paths; a bare tool name matches every call to that tool.
`deny` beats `allow`; `ask` forces a confirmation prompt. When you approve a tool
interactively, you can persist the rule to `.claude/settings.local.json` with one keystroke.

### Hooks

Hook commands receive a JSON payload on stdin
(`{"hook_event_name", "tool_name", "tool_input", "cwd", "session_id", ...}`).

| Event | When | Special behavior |
|---|---|---|
| `SessionStart` | REPL starts | stdout is added to the system prompt |
| `UserPromptSubmit` | before each user prompt | exit 2 blocks the prompt; stdout is added as context |
| `PreToolUse` | before a tool runs | exit 2 blocks the tool (stderr = reason) |
| `PostToolUse` | after a tool runs | feedback appended to the tool result |
| `Stop` | assistant finishes a turn | exit 2 makes the conversation continue with the feedback |

A hook can also print `{"decision": "block", "reason": "..."}` to stdout.

### MCP Servers

Configure stdio MCP servers in `.mcp.json` (project) or `~/.claude/mcp.json` (user):

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]
    }
  }
}
```

Servers are started on launch; their tools are exposed to the model as
`mcp__<server>__<tool>` and go through the same permission system
(allow them with rules like `"mcp__filesystem"`). Check status with `/mcp`.

### Sessions

Every conversation is persisted as JSONL under `~/.claude/projects/<dir-slug>/`.

```bash
open-claude --continue        # resume the most recent session here
open-claude --resume          # pick a session from a list
open-claude --resume abc123   # resume by ID
```

Inside the REPL, `/resume` switches to a previous session and `/clear` starts a new one.

### Subagents

The built-in `Agent` tool accepts a `subagent_type`:

- `general-purpose` - all base tools (Bash, Read, Write, Edit, Glob, Grep)
- `explore` - read-only (Read, Glob, Grep)

Define custom types in `~/.claude/agents/*.md` or `.claude/agents/*.md`:

```markdown
---
name: code-reviewer
description: Reviews code for bugs and style issues
tools: Read, Glob, Grep
model: claude-haiku-4-5
---

You are a thorough code reviewer. Focus on correctness and security.
```

List available types with `/agents`.

### CLAUDE.md

Add a `CLAUDE.md` file to your project root to provide persistent instructions:

```markdown
# Project Rules
- Always use type hints in Python code
- Run `pytest` before committing
- Use conventional commit messages
```

Supports: `CLAUDE.md`, `.claude/CLAUDE.md`, `.claude/rules/*.md`, `CLAUDE.local.md`, `~/.claude/CLAUDE.md`

## Architecture

```
open_claude/
  __init__.py       # Version
  __main__.py       # CLI entry point (argparse, --continue/--resume/--profile)
  config.py         # API key, model, environment detection
  profile.py        # Editable, reusable agent profiles (save/load)
  settings.py       # Multi-layer settings.json, permission rules
  api.py            # Anthropic API client (streaming + prompt caching)
  tools.py          # Tool schemas + executors (Bash, Read, Write, Edit, Glob, Grep, Skill)
  tasks.py          # In-memory task management system
  agent.py          # Sub-agent spawning + custom agent types (.claude/agents)
  hooks.py          # Hooks engine (PreToolUse, PostToolUse, Stop, ...)
  mcp.py            # MCP client (stdio transport, .mcp.json)
  sessions.py       # Session persistence (~/.claude/projects, JSONL)
  repl.py           # Interactive REPL, permission system, command handling
  tui.py            # Rich TUI: streaming Markdown, diffs, completion, status bar
  prompt.py         # System prompt construction
  tokens.py         # Token estimation, context window, cache-aware cost tracking
  compact.py        # Conversation compaction (summarization)
  claudemd.py       # CLAUDE.md discovery and loading
  skills/
    __init__.py
    frontmatter.py  # YAML frontmatter parser
    registry.py     # Skill registry, discovery, loading
    bundled.py      # Built-in skills (/commit, /review, etc.)
```

## Troubleshooting

**`No API key found`** - Set `ANTHROPIC_API_KEY` environment variable or add it to `~/.claude/config.json`.

**`'anthropic' package not installed`** - Run `pip install anthropic` (or `pip install -e .` if you cloned the repo).

**`python: command not found`** - Try `python3` instead, or ensure Python 3.10+ is installed and on your PATH.

**Windows: `open-claude` command not found after install** - Make sure your Python Scripts directory is on PATH. Alternatively, use `python -m open_claude`.

**Permission denied on tool execution** - The default mode asks before running Bash/Write/Edit. Press `y` to allow, `a` to always allow that tool, or `A` to allow all. You can also start with `--dangerously-skip-permissions`.

## License

MIT
