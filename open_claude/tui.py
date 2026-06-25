"""
Rich terminal UI components:

- MarkdownStream: live-rendered streaming Markdown (rich.Live)
- Colored diff rendering for Edit permission prompts
- Syntax-highlighted Write previews
- Numbered permission menu (Claude Code style)
- prompt_toolkit session with slash-command / @file completion and a
  bottom status toolbar (model | context | cost | mcp)
"""

import difflib
import os
from typing import Callable, Optional

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text


# ---------------------------------------------------------------------------
# Streaming Markdown
# ---------------------------------------------------------------------------

class MarkdownStream:
    """Accumulates streamed text and live-renders it as Markdown."""

    def __init__(self, console: Console):
        self.console = console
        self.buffer = ""
        self._live: Optional[Live] = None

    def feed(self, text: str):
        self.buffer += text
        if self._live is None:
            self.console.print()
            self._live = Live(
                console=self.console,
                refresh_per_second=8,
                vertical_overflow="visible",
            )
            self._live.start()
        try:
            self._live.update(Markdown(self.buffer, code_theme="monokai"))
        except Exception:
            # Malformed partial markdown should never kill the stream
            self._live.update(Text(self.buffer))

    def finish(self):
        """Stop the live region, leaving the final render in the scrollback."""
        if self._live is not None:
            try:
                self._live.update(Markdown(self.buffer, code_theme="monokai"))
            except Exception:
                self._live.update(Text(self.buffer))
            self._live.stop()
            self._live = None

    @property
    def started(self) -> bool:
        return self._live is not None or bool(self.buffer)


# ---------------------------------------------------------------------------
# Diff / file previews
# ---------------------------------------------------------------------------

_EXT_LEXERS = {
    ".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "tsx",
    ".jsx": "jsx", ".json": "json", ".md": "markdown", ".sh": "bash",
    ".yml": "yaml", ".yaml": "yaml", ".toml": "toml", ".html": "html",
    ".css": "css", ".rs": "rust", ".go": "go", ".java": "java", ".c": "c",
    ".cpp": "cpp", ".h": "c", ".rb": "ruby", ".php": "php", ".sql": "sql",
    ".xml": "xml", ".ps1": "powershell",
}


def _lexer_for(file_path: str) -> str:
    return _EXT_LEXERS.get(os.path.splitext(file_path)[1].lower(), "text")


def render_edit_diff(console: Console, file_path: str, old: str, new: str):
    """Show a colored unified diff for an Edit tool call."""
    diff_lines = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile="before", tofile="after", lineterm="",
    ))

    text = Text()
    # Skip the ---/+++ header lines, keep hunks
    for line in diff_lines[2:]:
        if line.startswith("@@"):
            text.append(line + "\n", style="cyan")
        elif line.startswith("+"):
            text.append(line + "\n", style="green")
        elif line.startswith("-"):
            text.append(line + "\n", style="red")
        else:
            text.append(line + "\n", style="dim")

    if not diff_lines:
        text.append("(no changes)", style="dim")

    console.print(Panel(
        text,
        title=f"[bold yellow]Edit[/bold yellow] [dim]{file_path}[/dim]",
        border_style="yellow",
    ))


def render_write_preview(console: Console, file_path: str, content: str, max_lines: int = 25):
    """Show a syntax-highlighted preview for a Write tool call."""
    lines = content.split("\n")
    preview = "\n".join(lines[:max_lines])
    truncated = len(lines) > max_lines

    body = Syntax(preview, _lexer_for(file_path), theme="monokai", line_numbers=True)
    title = f"[bold yellow]Write[/bold yellow] [dim]{file_path} ({len(lines)} lines)[/dim]"
    console.print(Panel(body, title=title, border_style="yellow"))
    if truncated:
        console.print(f"  [dim]... {len(lines) - max_lines} more lines not shown[/dim]")


def render_bash_preview(console: Console, command: str):
    console.print(Panel(
        Syntax(command, "bash", theme="monokai"),
        title="[bold yellow]Bash[/bold yellow]",
        border_style="yellow",
    ))


# ---------------------------------------------------------------------------
# Permission menu
# ---------------------------------------------------------------------------

def permission_menu(console: Console, options: list[str], prompt: str = "Choose") -> int:
    """
    Numbered choice menu. Returns the selected 0-based index.
    Accepts the option number, or y (first option) / n (last option).
    Ctrl+C / EOF selects the last option (deny).
    """
    for i, opt in enumerate(options, start=1):
        style = "bold green" if i == 1 else ("bold red" if i == len(options) else "bold cyan")
        console.print(f"  [{style}]{i}[/{style}]. {opt}")

    deny_index = len(options) - 1
    while True:
        try:
            choice = console.input(f"  [bold]{prompt} [1-{len(options)}] > [/bold]").strip()
        except (EOFError, KeyboardInterrupt):
            return deny_index

        if choice.lower() in ("y", "yes"):
            return 0
        if choice.lower() in ("n", "no", "esc", "q"):
            return deny_index
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            return int(choice) - 1
        console.print(f"  [dim]Enter a number between 1 and {len(options)}[/dim]")


# ---------------------------------------------------------------------------
# prompt_toolkit input: completion + bottom toolbar
# ---------------------------------------------------------------------------

def make_prompt_session(get_commands: Callable[[], list[tuple[str, str]]],
                        cwd: str,
                        get_status: Callable[[], str]):
    """
    Build a PromptSession with:
    - slash-command completion (from get_commands: [(name, description)])
    - @file path completion
    - persistent history at ~/.claude/open-claude-history
    - bottom status toolbar (from get_status)

    Returns None if prompt_toolkit is unavailable.
    """
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.history import FileHistory
    except ImportError:
        return None

    class ReplCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor

            # Slash command completion at the start of the line
            if text.startswith("/") and " " not in text:
                frag = text[1:]
                for name, desc in get_commands():
                    if name.startswith(frag):
                        yield Completion(
                            "/" + name,
                            start_position=-len(text),
                            display=f"/{name}",
                            display_meta=desc[:60],
                        )
                return

            # @file completion on the last token
            last_token = text.split()[-1] if text.split() else ""
            if last_token.startswith("@") and not text.endswith(" "):
                frag = last_token[1:]
                for path in _complete_paths(cwd, frag):
                    yield Completion(
                        "@" + path,
                        start_position=-len(last_token),
                        display=path,
                    )

    history_path = os.path.join(os.path.expanduser("~"), ".claude", "open-claude-history")
    try:
        os.makedirs(os.path.dirname(history_path), exist_ok=True)
        history = FileHistory(history_path)
    except OSError:
        history = None

    def _toolbar():
        try:
            return get_status()
        except Exception:
            return ""

    return PromptSession(
        completer=ReplCompleter(),
        complete_while_typing=True,
        history=history,
        bottom_toolbar=_toolbar,
    )


def _complete_paths(cwd: str, frag: str, max_results: int = 20) -> list[str]:
    """Complete a (possibly partial) relative path under cwd."""
    frag = frag.replace("\\", "/")
    dir_part, _, name_part = frag.rpartition("/")
    base_dir = os.path.join(cwd, dir_part) if dir_part else cwd

    if not os.path.isdir(base_dir):
        return []

    results = []
    try:
        entries = sorted(os.listdir(base_dir))
    except OSError:
        return []

    skip = {".git", "__pycache__", "node_modules", ".venv", "venv"}
    for entry in entries:
        if entry in skip:
            continue
        if not entry.lower().startswith(name_part.lower()):
            continue
        rel = f"{dir_part}/{entry}" if dir_part else entry
        if os.path.isdir(os.path.join(base_dir, entry)):
            rel += "/"
        results.append(rel)
        if len(results) >= max_results:
            break
    return results


# ---------------------------------------------------------------------------
# @file reference expansion
# ---------------------------------------------------------------------------

def expand_file_references(text: str, cwd: str, max_bytes: int = 50_000) -> str:
    """
    Expand @path tokens in the user's message by appending the file contents,
    so the model sees the referenced files without an extra Read round-trip.
    """
    import re as _re

    refs = _re.findall(r"@([\w./\\~-]+)", text)
    if not refs:
        return text

    attachments = []
    for ref in refs:
        path = os.path.expanduser(ref)
        if not os.path.isabs(path):
            path = os.path.join(cwd, path)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(max_bytes)
        except OSError:
            continue
        attachments.append(f'<file path="{ref}">\n{content}\n</file>')

    if not attachments:
        return text
    return text + "\n\n" + "\n\n".join(attachments)
