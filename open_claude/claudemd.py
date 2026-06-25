"""
CLAUDE.md discovery and loading.

Search order (matching original Claude Code):
1. Managed:  /etc/claude-code/CLAUDE.md  (system-wide policy)
2. User:     ~/.claude/CLAUDE.md          (personal global)
3. Project:  Walk up from cwd → root:
             - CLAUDE.md
             - .claude/CLAUDE.md
             - .claude/rules/*.md
4. Local:    CLAUDE.local.md              (gitignored personal project)
"""

import os
import re
from pathlib import Path
from typing import Optional


class MemoryFile:
    """One loaded CLAUDE.md (or rules/*.md) file."""
    __slots__ = ("path", "content", "source")

    def __init__(self, path: str, content: str, source: str):
        self.path = path          # absolute path
        self.content = content    # file text (after processing)
        self.source = source      # "managed" | "user" | "project" | "local"


# Maximum total bytes for memory content injected into prompt
MAX_MEMORY_BYTES = 100_000
# Maximum lines for MEMORY.md index files
MAX_MEMORY_INDEX_LINES = 200


def load_memory_files(cwd: str) -> list[MemoryFile]:
    """
    Discover and load all CLAUDE.md / rules files.
    Returns list ordered from broadest scope (managed) to narrowest (local).
    """
    files: list[MemoryFile] = []
    seen: set[str] = set()

    def _add(path: str, source: str):
        norm = os.path.normcase(os.path.realpath(path))
        if norm in seen:
            return
        content = _read_file(path)
        if content is None:
            return
        seen.add(norm)
        # Process @include directives
        base_dir = os.path.dirname(path)
        content = _resolve_includes(content, base_dir, seen_includes=set())
        # Truncate MEMORY.md specifically
        if os.path.basename(path).upper() == "MEMORY.MD":
            content = _truncate_lines(content, MAX_MEMORY_INDEX_LINES)
        files.append(MemoryFile(path=path, content=content, source=source))

    # 1. Managed (system-wide)
    managed = _managed_path()
    if managed:
        _add(managed, "managed")

    # 2. User global: ~/.claude/CLAUDE.md
    user_claude = os.path.join(Path.home(), ".claude", "CLAUDE.md")
    _add(user_claude, "user")

    # 3. Project: walk up from cwd
    current = os.path.abspath(cwd)
    home = str(Path.home())
    visited_dirs: set[str] = set()

    while True:
        norm_dir = os.path.normcase(current)
        if norm_dir in visited_dirs:
            break
        visited_dirs.add(norm_dir)

        # CLAUDE.md in directory root
        _add(os.path.join(current, "CLAUDE.md"), "project")

        # .claude/CLAUDE.md
        _add(os.path.join(current, ".claude", "CLAUDE.md"), "project")

        # .claude/rules/*.md
        rules_dir = os.path.join(current, ".claude", "rules")
        if os.path.isdir(rules_dir):
            try:
                for fname in sorted(os.listdir(rules_dir)):
                    if fname.endswith(".md"):
                        _add(os.path.join(rules_dir, fname), "project")
            except OSError:
                pass

        parent = os.path.dirname(current)
        if parent == current:
            break
        # Stop at home dir
        if not current.startswith(home) or current == home:
            break
        current = parent

    # 4. Local: CLAUDE.local.md (cwd only)
    _add(os.path.join(cwd, "CLAUDE.local.md"), "local")

    return files


def build_memory_prompt(cwd: str, mode: str = "full") -> str:
    """
    Build the memory/instructions block for the system prompt.
    Returns empty string if no memory files found.

    Modes (controlled by the active agent profile):
      full     - all CLAUDE.md / rules / local files (default behaviour)
      project  - only project + local files (ignore user/managed globals)
      summary  - only MEMORY.md index files, plus the first lines of each file
      off      - inject nothing
    """
    if mode == "off":
        return ""

    mem_files = load_memory_files(cwd)
    if not mem_files:
        return ""

    if mode == "project":
        mem_files = [mf for mf in mem_files if mf.source in ("project", "local")]
    elif mode == "summary":
        mem_files = [_summarize_memory_file(mf) for mf in mem_files]
    if not mem_files:
        return ""

    parts = []
    total_bytes = 0

    for mf in mem_files:
        if total_bytes + len(mf.content.encode()) > MAX_MEMORY_BYTES:
            # Truncate to stay within budget
            remaining = MAX_MEMORY_BYTES - total_bytes
            if remaining > 200:
                truncated = mf.content.encode()[:remaining].decode("utf-8", errors="ignore")
                parts.append(f"# From {mf.path} ({mf.source}) [truncated]\n{truncated}")
            break

        rel = _try_relative(mf.path, cwd)
        parts.append(f"# From {rel} ({mf.source})\n{mf.content}")
        total_bytes += len(mf.content.encode())

    if not parts:
        return ""

    header = (
        "Codebase and user instructions from CLAUDE.md files are shown below. "
        "Adhere to these instructions.\n\n"
    )
    return header + "\n\n".join(parts)


# Lines kept per file in "summary" memory mode
SUMMARY_HEAD_LINES = 20


def _summarize_memory_file(mf: "MemoryFile") -> "MemoryFile":
    """Condense a memory file to its leading lines for 'summary' mode."""
    lines = mf.content.split("\n")
    if len(lines) <= SUMMARY_HEAD_LINES:
        return mf
    condensed = "\n".join(lines[:SUMMARY_HEAD_LINES])
    condensed += f"\n\n... ({len(lines) - SUMMARY_HEAD_LINES} more lines omitted in summary mode)"
    return MemoryFile(path=mf.path, content=condensed, source=mf.source)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _managed_path() -> Optional[str]:
    """System-managed CLAUDE.md path (Linux/macOS only)."""
    path = "/etc/claude-code/CLAUDE.md"
    if os.path.isfile(path):
        return path
    return None


def _read_file(path: str) -> Optional[str]:
    """Read a text file, return None on failure."""
    if not os.path.isfile(path):
        return None
    try:
        content = Path(path).read_text(encoding="utf-8", errors="replace")
        # Strip HTML comments (but not inside code blocks)
        content = _strip_html_comments(content)
        return content.strip()
    except OSError:
        return None


def _strip_html_comments(text: str) -> str:
    """Remove <!-- ... --> comments, but not inside ``` code blocks."""
    parts = re.split(r"(```[\s\S]*?```)", text)
    result = []
    for i, part in enumerate(parts):
        if i % 2 == 0:  # Outside code blocks
            part = re.sub(r"<!--[\s\S]*?-->", "", part)
        result.append(part)
    return "".join(result)


def _resolve_includes(
    content: str, base_dir: str, seen_includes: set[str], depth: int = 0,
) -> str:
    """Resolve @path include directives (non-recursive past depth 5)."""
    if depth > 5:
        return content

    def _replace_include(match: re.Match) -> str:
        raw_path = match.group(1).strip()

        # Resolve path
        if raw_path.startswith("~/"):
            full_path = os.path.expanduser(raw_path)
        elif raw_path.startswith("/") or (len(raw_path) > 1 and raw_path[1] == ":"):
            full_path = raw_path
        else:
            full_path = os.path.join(base_dir, raw_path)

        full_path = os.path.normpath(full_path)
        norm = os.path.normcase(os.path.realpath(full_path))

        if norm in seen_includes:
            return ""  # Circular reference
        seen_includes.add(norm)

        included = _read_file(full_path)
        if included is None:
            return ""  # Silently ignore missing files

        # Recursive include resolution
        inc_base = os.path.dirname(full_path)
        return _resolve_includes(included, inc_base, seen_includes, depth + 1)

    # Match @path at start of line (not in code blocks)
    # Simple approach: only match lines starting with @
    lines = content.split("\n")
    result_lines = []
    in_code_block = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            result_lines.append(line)
            continue

        if in_code_block:
            result_lines.append(line)
            continue

        # Check for @include pattern
        inc_match = re.match(r"^@(.+)$", stripped)
        if inc_match:
            included = _replace_include(inc_match)
            if included:
                result_lines.append(included)
        else:
            result_lines.append(line)

    return "\n".join(result_lines)


def _truncate_lines(text: str, max_lines: int) -> str:
    """Truncate text to max_lines."""
    lines = text.split("\n")
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[:max_lines]) + f"\n\n... (truncated, {len(lines) - max_lines} more lines)"


def _try_relative(path: str, cwd: str) -> str:
    """Try to make path relative to cwd for display."""
    try:
        return os.path.relpath(path, cwd)
    except ValueError:
        return path
