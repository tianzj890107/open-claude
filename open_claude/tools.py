"""Tool definitions and execution for Open Claude."""

import fnmatch
import glob as glob_module
import os
import re
import subprocess
import sys
import platform
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Base helpers
# ---------------------------------------------------------------------------

def _truncate(text: str, max_chars: int = 80_000) -> str:
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + f"\n\n... [truncated {len(text) - max_chars} chars] ...\n\n" + text[-half:]


def _relative(path: str, cwd: str) -> str:
    try:
        return os.path.relpath(path, cwd)
    except ValueError:
        return path


# ---------------------------------------------------------------------------
# Tool: Bash
# ---------------------------------------------------------------------------

BASH_SCHEMA = {
    "name": "Bash",
    "description": (
        "Executes a bash command and returns its output. "
        "Use this for running shell commands, installing packages, "
        "running scripts, git operations, etc."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The bash command to execute.",
            },
            "timeout": {
                "type": "integer",
                "description": "Optional timeout in seconds (default 120).",
            },
        },
        "required": ["command"],
    },
}


def _find_git_bash() -> Optional[str]:
    """Find Git Bash on Windows."""
    candidates = [
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "bin", "bash.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Git", "bin", "bash.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Git", "bin", "bash.exe"),
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return None


def _run_command(args, *, shell=False, executable=None, timeout=120, cwd="."):
    """Run a command and return (stdout, stderr, returncode)."""
    result = subprocess.run(
        args,
        shell=shell,
        executable=executable,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=cwd,
    )
    return result.stdout, result.stderr, result.returncode


def execute_bash(params: dict[str, Any], cwd: str) -> str:
    command = params["command"]
    timeout = params.get("timeout", 120)

    is_windows = platform.system() == "Windows"

    if not is_windows:
        # Linux/macOS: straightforward bash
        try:
            stdout, stderr, rc = _run_command(
                command, shell=True, executable="/bin/bash", timeout=timeout, cwd=cwd,
            )
        except subprocess.TimeoutExpired:
            return f"Command timed out after {timeout}s"
        except Exception as e:
            return f"Error executing command: {e}"
    else:
        # Windows: try multiple shells, pick the first that succeeds.
        # 1) Git Bash  - full Unix compatibility (rm -rf, grep, sed, awk …)
        # 2) PowerShell - Windows-native + some Unix aliases (ls, rm, cp …)
        # 3) cmd.exe   - basic fallback (dir /b, del, etc.)
        attempts: list[tuple[str, list | str, dict]] = []

        git_bash = _find_git_bash()
        if git_bash:
            attempts.append(("git-bash", [git_bash, "-c", command], {"timeout": timeout, "cwd": cwd}))
        attempts.append(("powershell", ["powershell", "-NoProfile", "-NonInteractive", "-Command", command], {"timeout": timeout, "cwd": cwd}))
        attempts.append(("cmd", command, {"shell": True, "timeout": timeout, "cwd": cwd}))

        stdout, stderr, rc = "", "", -1
        for name, args, kwargs in attempts:
            try:
                stdout, stderr, rc = _run_command(args, **kwargs)
                if rc == 0:
                    break  # Success, stop trying
            except subprocess.TimeoutExpired:
                return f"Command timed out after {timeout}s"
            except Exception:
                continue  # Try next shell

    parts = []
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(f"STDERR:\n{stderr}")
    if rc != 0:
        parts.append(f"Exit code: {rc}")
    output = "\n".join(parts) if parts else "(no output)"
    return _truncate(output)


# ---------------------------------------------------------------------------
# Tool: Read
# ---------------------------------------------------------------------------

READ_SCHEMA = {
    "name": "Read",
    "description": (
        "Reads a file from the filesystem. Returns file content with line numbers. "
        "Supports text files, with optional offset and limit parameters."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute path to the file to read.",
            },
            "offset": {
                "type": "integer",
                "description": "Line number to start reading from (0-based). Optional.",
            },
            "limit": {
                "type": "integer",
                "description": "Max number of lines to read. Optional, default 2000.",
            },
        },
        "required": ["file_path"],
    },
}


def execute_read(params: dict[str, Any], cwd: str) -> str:
    file_path = params["file_path"]
    # Allow relative paths resolved from cwd
    if not os.path.isabs(file_path):
        file_path = os.path.join(cwd, file_path)

    offset = params.get("offset", 0)
    limit = params.get("limit", 2000)

    if not os.path.exists(file_path):
        return f"Error: File not found: {file_path}"
    if os.path.isdir(file_path):
        return f"Error: {file_path} is a directory, not a file. Use Bash with 'ls' to list directories."

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()

        total = len(all_lines)
        selected = all_lines[offset : offset + limit]
        result_lines = []
        for i, line in enumerate(selected, start=offset + 1):
            result_lines.append(f"{i}\t{line.rstrip()}")

        header = f"File: {file_path} ({total} lines total)"
        if offset > 0 or len(selected) < total:
            header += f" [showing lines {offset + 1}-{offset + len(selected)}]"

        return header + "\n" + "\n".join(result_lines)
    except Exception as e:
        return f"Error reading file: {e}"


# ---------------------------------------------------------------------------
# Tool: Write
# ---------------------------------------------------------------------------

WRITE_SCHEMA = {
    "name": "Write",
    "description": (
        "Writes content to a file, creating it if it doesn't exist "
        "or overwriting if it does. Creates parent directories as needed."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute path to the file to write.",
            },
            "content": {
                "type": "string",
                "description": "The content to write to the file.",
            },
        },
        "required": ["file_path", "content"],
    },
}


def execute_write(params: dict[str, Any], cwd: str) -> str:
    file_path = params["file_path"]
    content = params["content"]

    if not os.path.isabs(file_path):
        file_path = os.path.join(cwd, file_path)

    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        existed = os.path.exists(file_path)
        with open(file_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        action = "Updated" if existed else "Created"
        lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return f"{action} file: {file_path} ({lines} lines)"
    except Exception as e:
        return f"Error writing file: {e}"


# ---------------------------------------------------------------------------
# Tool: Edit
# ---------------------------------------------------------------------------

EDIT_SCHEMA = {
    "name": "Edit",
    "description": (
        "Performs exact string replacement in a file. "
        "The old_string must match exactly (including whitespace/indentation). "
        "Use replace_all=true to replace all occurrences."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute path to the file to edit.",
            },
            "old_string": {
                "type": "string",
                "description": "The exact text to find and replace.",
            },
            "new_string": {
                "type": "string",
                "description": "The replacement text.",
            },
            "replace_all": {
                "type": "boolean",
                "description": "If true, replace all occurrences. Default false.",
            },
        },
        "required": ["file_path", "old_string", "new_string"],
    },
}


def execute_edit(params: dict[str, Any], cwd: str) -> str:
    file_path = params["file_path"]
    old_string = params["old_string"]
    new_string = params["new_string"]
    replace_all = params.get("replace_all", False)

    if not os.path.isabs(file_path):
        file_path = os.path.join(cwd, file_path)

    if not os.path.exists(file_path):
        return f"Error: File not found: {file_path}"

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        return f"Error reading file: {e}"

    # Check for match
    count = content.count(old_string)
    if count == 0:
        # Try with normalized line endings
        normalized_old = old_string.replace("\r\n", "\n")
        normalized_content = content.replace("\r\n", "\n")
        count = normalized_content.count(normalized_old)
        if count == 0:
            return (
                f"Error: old_string not found in {file_path}. "
                "Make sure the string matches exactly including whitespace and indentation."
            )
        # Work with normalized content
        content = normalized_content
        old_string = normalized_old

    if count > 1 and not replace_all:
        return (
            f"Error: old_string found {count} times in {file_path}. "
            "Use replace_all=true to replace all, or provide more context to make it unique."
        )

    if replace_all:
        new_content = content.replace(old_string, new_string)
    else:
        new_content = content.replace(old_string, new_string, 1)

    try:
        with open(file_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(new_content)

        replacements = count if replace_all else 1
        return f"Edited {file_path}: replaced {replacements} occurrence(s)"
    except Exception as e:
        return f"Error writing file: {e}"


# ---------------------------------------------------------------------------
# Tool: Glob
# ---------------------------------------------------------------------------

GLOB_SCHEMA = {
    "name": "Glob",
    "description": (
        "Fast file pattern matching. Supports glob patterns like '**/*.py' or 'src/**/*.ts'. "
        "Returns matching file paths sorted by modification time."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern to match files (e.g. '**/*.py').",
            },
            "path": {
                "type": "string",
                "description": "Directory to search in. Defaults to cwd.",
            },
        },
        "required": ["pattern"],
    },
}


def execute_glob(params: dict[str, Any], cwd: str) -> str:
    pattern = params["pattern"]
    search_path = params.get("path", cwd)
    if not os.path.isabs(search_path):
        search_path = os.path.join(cwd, search_path)

    try:
        full_pattern = os.path.join(search_path, pattern)
        matches = glob_module.glob(full_pattern, recursive=True)

        # Sort by mtime (newest first)
        file_matches = [m for m in matches if os.path.isfile(m)]
        try:
            file_matches.sort(key=lambda x: os.path.getmtime(x), reverse=True)
        except OSError:
            file_matches.sort()

        # Limit results
        max_results = 200
        truncated = len(file_matches) > max_results
        file_matches = file_matches[:max_results]

        if not file_matches:
            return f"No files found matching pattern: {pattern}"

        rel_paths = [_relative(f, cwd) for f in file_matches]
        result = "\n".join(rel_paths)
        if truncated:
            result += f"\n\n(truncated, showing {max_results} of {len(matches)} matches)"
        return result
    except Exception as e:
        return f"Error in glob search: {e}"


# ---------------------------------------------------------------------------
# Tool: Grep
# ---------------------------------------------------------------------------

GREP_SCHEMA = {
    "name": "Grep",
    "description": (
        "Search file contents using regex patterns. "
        "Supports regex syntax, file type filtering, and context lines."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regex pattern to search for.",
            },
            "path": {
                "type": "string",
                "description": "File or directory to search in. Defaults to cwd.",
            },
            "glob": {
                "type": "string",
                "description": "Glob pattern to filter files (e.g. '*.py').",
            },
            "case_insensitive": {
                "type": "boolean",
                "description": "Case insensitive search. Default false.",
            },
        },
        "required": ["pattern"],
    },
}


def execute_grep(params: dict[str, Any], cwd: str) -> str:
    pattern = params["pattern"]
    search_path = params.get("path", cwd)
    file_glob = params.get("glob")
    case_insensitive = params.get("case_insensitive", False)

    if not os.path.isabs(search_path):
        search_path = os.path.join(cwd, search_path)

    flags = re.IGNORECASE if case_insensitive else 0

    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        return f"Invalid regex pattern: {e}"

    results = []
    max_results = 250
    searched = 0

    # VCS dirs to skip
    skip_dirs = {".git", ".svn", ".hg", "__pycache__", "node_modules", ".venv", "venv"}

    if os.path.isfile(search_path):
        files_to_search = [search_path]
    else:
        files_to_search = []
        for root, dirs, files in os.walk(search_path):
            # Skip hidden and VCS dirs
            dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
            for fname in files:
                if file_glob and not fnmatch.fnmatch(fname, file_glob):
                    continue
                files_to_search.append(os.path.join(root, fname))

    for fpath in files_to_search:
        if len(results) >= max_results:
            break
        try:
            with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                for line_num, line in enumerate(f, 1):
                    if regex.search(line):
                        rel = _relative(fpath, cwd)
                        results.append(f"{rel}:{line_num}:{line.rstrip()}")
                        if len(results) >= max_results:
                            break
            searched += 1
        except (OSError, UnicodeDecodeError):
            continue

    if not results:
        return f"No matches found for pattern: {pattern}"

    output = "\n".join(results)
    if len(results) >= max_results:
        output += f"\n\n(results limited to {max_results} matches)"
    return output


# ---------------------------------------------------------------------------
# Tool: Skill
# ---------------------------------------------------------------------------

SKILL_SCHEMA = {
    "name": "Skill",
    "description": (
        "Execute a skill (slash command). Skills provide specialized capabilities "
        "like /commit, /review, /test, /fix, /explain, /simplify, and user-defined skills."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "description": "The skill name (e.g. 'commit', 'review', or '/commit').",
            },
            "args": {
                "type": "string",
                "description": "Optional arguments for the skill.",
            },
        },
        "required": ["skill"],
    },
}


def execute_skill(params: dict[str, Any], cwd: str) -> str:
    from .skills.registry import get_registry

    skill_name = params["skill"].lstrip("/")
    args = params.get("args", "")

    registry = get_registry()
    skill = registry.get(skill_name)

    if not skill:
        available = [s.name for s in registry.get_user_invocable()]
        return (
            f"Unknown skill: {skill_name}. "
            f"Available skills: {', '.join(available) if available else 'none'}"
        )

    try:
        prompt = skill.get_prompt_for_command(args)
        return prompt
    except Exception as e:
        return f"Error executing skill '{skill_name}': {e}"


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

from .tasks import TASK_TOOL_SCHEMAS, TASK_TOOL_EXECUTORS

# Core tool schemas. The Agent tool schema is built dynamically per session
# (it lists the available subagent types), and MCP tool schemas are appended
# by the conversation — see repl.py.
TOOL_SCHEMAS = [
    BASH_SCHEMA,
    READ_SCHEMA,
    WRITE_SCHEMA,
    EDIT_SCHEMA,
    GLOB_SCHEMA,
    GREP_SCHEMA,
    SKILL_SCHEMA,
    *TASK_TOOL_SCHEMAS,
]

TOOL_EXECUTORS = {
    "Bash": execute_bash,
    "Read": execute_read,
    "Write": execute_write,
    "Edit": execute_edit,
    "Glob": execute_glob,
    "Grep": execute_grep,
    "Skill": execute_skill,
    **TASK_TOOL_EXECUTORS,
    # Agent executor is special — needs client, handled in repl.py
}


def get_base_tool_schemas() -> list[dict[str, Any]]:
    """Get only the core tool schemas (for sub-agents, no Agent/Task)."""
    return [BASH_SCHEMA, READ_SCHEMA, WRITE_SCHEMA, EDIT_SCHEMA, GLOB_SCHEMA, GREP_SCHEMA]


# Tools that can modify the local filesystem or run arbitrary shell commands.
# When OC_READONLY_FS is set (e.g. by the web bridge), they are refused at the
# execution layer — this also covers sub-agents, which dispatch through here.
_READONLY_BLOCKED_TOOLS = {"Write", "Edit", "Bash"}


# ---------------------------------------------------------------------------
# Sandbox confinement (OC_SANDBOX_ROOT)
#
# When OC_SANDBOX_ROOT is set (e.g. by the codex-style web server), every tool
# dispatched through execute_tool is confined to that directory tree. File
# tools get their path arguments resolved and checked; Bash commands are
# screened for the common escape vectors (absolute paths outside the sandbox,
# `..` traversal, `~` home expansion). The Bash screen is best-effort — the
# hard guarantee applies to the file tools; shell commands always execute with
# their cwd inside the sandbox. The CLI never sets this variable.
# ---------------------------------------------------------------------------

_SANDBOX_PATH_KEYS = {
    "Read": ("file_path",),
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "Glob": ("path",),
    "Grep": ("path",),
}

# Windows drive-letter absolute paths appearing anywhere in a shell command.
_WIN_ABS_RE = re.compile(r"(?<![\w])([A-Za-z]:[\\/][^\s\"'`;|&<>]*)")
# Git-bash style /c/... drive paths.
_POSIX_DRIVE_RE = re.compile(r"(?:^|[\s\"'`=(;|&])(/([A-Za-z])/[^\s\"'`;|&<>]*)")
# A standalone `..` path component (bounded so `main..HEAD` etc. still pass).
_DOTDOT_RE = re.compile(r"(?:^|[\s\"'`=(;|&:])\.\.(?:$|[\s\"'`\\/);|&])")
# A `~` used as a path (home-dir escape).
_HOME_RE = re.compile(r"(?:^|[\s\"'`=(;|&:])~(?:$|[/\\\s\"'`;)|&])")


def _sandbox_root() -> Optional[str]:
    root = os.environ.get("OC_SANDBOX_ROOT")
    if not root:
        return None
    return os.path.normcase(os.path.realpath(root))


def _in_sandbox(path: str, root: str) -> bool:
    real = os.path.normcase(os.path.realpath(path))
    return real == root or real.startswith(root + os.sep)


def _sandbox_violation(name: str, params: dict[str, Any], cwd: str, root: str) -> Optional[str]:
    """Return an error message if this tool call would leave the sandbox."""
    if name in _SANDBOX_PATH_KEYS:
        candidates = [params.get(k) for k in _SANDBOX_PATH_KEYS[name]]
        # An absolute Glob pattern bypasses the `path` arg — check it as a path.
        if name == "Glob" and os.path.isabs(params.get("pattern", "")):
            candidates.append(params.get("pattern"))
        for raw in candidates:
            if not raw:
                continue
            p = raw if os.path.isabs(raw) else os.path.join(cwd, raw)
            if not _in_sandbox(p, root):
                return (
                    f"Error: path '{raw}' is outside the sandbox. "
                    f"This session can only access files under: {root}"
                )
    elif name == "Bash":
        cmd = params.get("command", "")
        if _DOTDOT_RE.search(cmd):
            return ("Error: '..' path traversal is not allowed in this sandboxed session. "
                    f"Work with paths under: {root}")
        if _HOME_RE.search(cmd):
            return ("Error: '~' (home directory) paths are not allowed in this sandboxed session. "
                    f"Work with paths under: {root}")
        for m in _WIN_ABS_RE.finditer(cmd):
            if not _in_sandbox(m.group(1), root):
                return (f"Error: absolute path '{m.group(1)}' is outside the sandbox. "
                        f"This session can only access files under: {root}")
        for m in _POSIX_DRIVE_RE.finditer(cmd):
            translated = m.group(2) + ":" + m.group(1)[2:]
            if not _in_sandbox(translated, root):
                return (f"Error: absolute path '{m.group(1)}' is outside the sandbox. "
                        f"This session can only access files under: {root}")
    return None


def execute_tool(name: str, params: dict[str, Any], cwd: str) -> str:
    """Execute a tool by name with given params."""
    if os.environ.get("OC_READONLY_FS") and name in _READONLY_BLOCKED_TOOLS:
        return (
            f"Error: '{name}' is disabled in this read-only session. "
            "The filesystem cannot be modified and shell commands cannot be run here."
        )
    root = _sandbox_root()
    if root:
        violation = _sandbox_violation(name, params, cwd, root)
        if violation:
            return violation
    executor = TOOL_EXECUTORS.get(name)
    if not executor:
        return f"Unknown tool: {name}"
    return executor(params, cwd)
