"""Entry point for Open Claude: python -m open_claude"""

import argparse
import os
import sys


def main():
    parser = argparse.ArgumentParser(
        prog="open-claude",
        description="Open Claude - AI coding assistant",
    )
    parser.add_argument(
        "--version", "-v", action="store_true",
        help="Show version and exit",
    )
    parser.add_argument(
        "--cwd", type=str, default=None,
        help="Working directory (default: current directory)",
    )
    parser.add_argument(
        "--model", type=str, default=None,
        help="Model to use (overrides CLAUDE_MODEL env var)",
    )
    parser.add_argument(
        "-p", "--prompt", type=str, default=None,
        help="Run a single prompt (non-interactive mode) and exit",
    )
    parser.add_argument(
        "--dangerously-skip-permissions", action="store_true",
        help="Auto-approve all tool executions without asking (use with caution)",
    )
    parser.add_argument(
        "-c", "--continue", dest="continue_last", action="store_true",
        help="Continue the most recent session for this directory",
    )
    parser.add_argument(
        "--resume", nargs="?", const="", default=None, metavar="SESSION_ID",
        help="Resume a session by ID (omit the ID to pick interactively)",
    )
    parser.add_argument(
        "--profile", type=str, default=None, metavar="NAME",
        help="Start with a saved agent profile (see /profile in the REPL)",
    )

    args = parser.parse_args()

    if args.version:
        from . import __version__
        print(f"{__version__} (Open Claude)")
        return

    # Set model override (resolve friendly aliases like "opus" -> "claude-opus-4-8")
    if args.model:
        from .config import resolve_model
        os.environ["CLAUDE_MODEL"] = resolve_model(args.model)

    cwd = args.cwd or os.getcwd()
    if not os.path.isdir(cwd):
        print(f"Error: Directory not found: {cwd}", file=sys.stderr)
        sys.exit(1)

    # Check dependencies
    try:
        import anthropic
    except ImportError:
        print("Error: 'anthropic' package not installed.", file=sys.stderr)
        print("Run: pip install anthropic", file=sys.stderr)
        sys.exit(1)

    try:
        import rich
    except ImportError:
        print("Error: 'rich' package not installed.", file=sys.stderr)
        print("Run: pip install rich", file=sys.stderr)
        sys.exit(1)

    # Check the API key for the selected model's provider.
    from .config import get_api_key_for, get_model, get_model_provider, PROVIDERS
    provider = get_model_provider(get_model())
    if not get_api_key_for(provider):
        spec = PROVIDERS.get(provider, {})
        envs = " or ".join(spec.get("env", [])) or "the provider API key"
        label = spec.get("label", provider)
        print(f"Error: No API key found for {label}.", file=sys.stderr)
        print(f"Set {envs} environment variable, or add it to ~/.claude/config.json "
              f'(\"api_keys\": {{\"{provider}\": \"...\"}}).', file=sys.stderr)
        sys.exit(1)

    permission_mode = "always_allow" if args.dangerously_skip_permissions else "default"

    if args.prompt:
        # Non-interactive: single prompt mode
        _run_single_prompt(args.prompt, cwd, permission_mode, args.profile)
        return

    # Resolve --resume: empty string means "pick interactively"
    resume_session_id = None
    if args.resume is not None:
        if args.resume:
            resume_session_id = args.resume
        else:
            resume_session_id = _pick_session(cwd)
            if resume_session_id is None:
                print("No previous sessions for this directory.", file=sys.stderr)

    # Interactive REPL
    from .repl import run_repl
    run_repl(
        cwd,
        permission_mode=permission_mode,
        resume_session_id=resume_session_id,
        continue_last=args.continue_last,
        profile_name=args.profile,
    )


def _pick_session(cwd: str):
    """Interactive session picker for `--resume` without an ID."""
    from .sessions import list_sessions

    sessions = list_sessions(cwd)
    if not sessions:
        return None

    print("Recent sessions:")
    for i, s in enumerate(sessions, start=1):
        print(f"  {i}. [{s['modified']}] ({s['messages']} msgs) {s['preview']}")
    try:
        choice = input(f"Resume which session? [1-{len(sessions)}, Enter to cancel] > ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if choice.isdigit() and 1 <= int(choice) <= len(sessions):
        return sessions[int(choice) - 1]["id"]
    return None


def _run_single_prompt(prompt: str, cwd: str, permission_mode: str = "default",
                       profile_name: str = None):
    """Run a single prompt and exit."""
    from rich.console import Console
    from .repl import Conversation
    from .profile import load_profile

    console = Console()
    profile = load_profile(profile_name, cwd) if profile_name else None
    conv = Conversation(cwd, permission_mode=permission_mode, profile=profile)
    conv.add_user_message(prompt)

    try:
        conv.run_turn()
    except KeyboardInterrupt:
        console.print("\n[dim](interrupted)[/dim]")
    except Exception as e:
        console.print(f"[bold red]Error: {e}[/bold red]")
        sys.exit(1)
    finally:
        conv.mcp.shutdown()


if __name__ == "__main__":
    main()
