"""Skill registry: discovery, loading, and management."""

import fnmatch
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

from .frontmatter import normalize_metadata, parse_frontmatter


class Skill:
    """Represents a single skill (loaded from file or bundled)."""

    def __init__(
        self,
        name: str,
        description: str = "",
        when_to_use: str = "",
        user_invocable: bool = True,
        argument_hint: str = "",
        allowed_tools: Optional[list[str]] = None,
        model: Optional[str] = None,
        context: str = "inline",  # "inline" or "fork"
        paths: Optional[str] = None,
        source: str = "unknown",  # "bundled", "user", "project"
        source_path: Optional[str] = None,
        get_prompt: Optional[Callable[[str], str]] = None,
        prompt_text: str = "",
        skill_dir: Optional[str] = None,
    ):
        self.name = name
        self.description = description
        self.when_to_use = when_to_use
        self.user_invocable = user_invocable
        self.argument_hint = argument_hint
        self.allowed_tools = allowed_tools or []
        self.model = model
        self.context = context
        self.paths = paths  # Glob patterns for conditional activation
        self.source = source
        self.source_path = source_path
        self._get_prompt = get_prompt
        self._prompt_text = prompt_text
        self.skill_dir = skill_dir

    def get_prompt_for_command(self, args: str = "") -> str:
        """Get the full prompt content for this skill."""
        if self._get_prompt:
            return self._get_prompt(args)

        prompt = self._prompt_text

        # Variable substitution
        if self.skill_dir:
            prompt = prompt.replace("${CLAUDE_SKILL_DIR}", self.skill_dir)

        # Execute inline shell commands: !`command`
        prompt = _execute_inline_commands(prompt)

        # Append args
        if args:
            prompt += f"\n\nUser arguments: {args}"

        return prompt

    def matches_path(self, file_path: str) -> bool:
        """Check if a file path matches this skill's conditional paths."""
        if not self.paths:
            return False
        patterns = [p.strip() for p in self.paths.split(",")]
        for pattern in patterns:
            if fnmatch.fnmatch(file_path, pattern):
                return True
        return False


def _execute_inline_commands(text: str) -> str:
    """Execute !`command` inline shell expressions and substitute output."""
    def _run_cmd(match):
        cmd = match.group(1)
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=10,
            )
            return result.stdout.strip()
        except Exception:
            return f"(error running: {cmd})"

    return re.sub(r"!`([^`]+)`", _run_cmd, text)


# ---------------------------------------------------------------------------
# Skill Registry (global singleton)
# ---------------------------------------------------------------------------

class SkillRegistry:
    """Central registry for all skills."""

    def __init__(self):
        self._skills: dict[str, Skill] = {}
        self._conditional_skills: dict[str, Skill] = {}  # With paths, not yet activated
        self._activated_conditional: set[str] = set()
        self._disabled: set[str] = set()  # skills turned off by the active profile

    # -- enable / disable (profile-controlled) ------------------------------

    def set_disabled(self, names: list[str]):
        """Replace the disabled set (e.g. when applying a profile)."""
        self._disabled = {n.lstrip("/") for n in names}

    def disable(self, name: str):
        self._disabled.add(name.lstrip("/"))

    def enable(self, name: str):
        self._disabled.discard(name.lstrip("/"))

    def is_disabled(self, name: str) -> bool:
        return name.lstrip("/") in self._disabled

    def disabled_names(self) -> list[str]:
        return sorted(self._disabled)

    def known_names(self) -> list[str]:
        """All registered skill names, including disabled ones."""
        return sorted(set(self._skills) | set(self._conditional_skills))

    def register(self, skill: Skill):
        """Register a skill."""
        if skill.paths and skill.source != "bundled":
            self._conditional_skills[skill.name] = skill
        else:
            self._skills[skill.name] = skill

    def get(self, name: str) -> Optional[Skill]:
        """Find an *enabled* skill by name (disabled skills are hidden)."""
        # Normalize: strip leading /
        name = name.lstrip("/")
        if name in self._disabled:
            return None
        return self._skills.get(name)

    def get_all(self) -> list[Skill]:
        """Get all active (enabled) skills."""
        return [s for s in self._skills.values() if s.name not in self._disabled]

    def get_user_invocable(self) -> list[Skill]:
        """Get enabled skills the user can invoke via /command."""
        return [s for s in self._skills.values()
                if s.user_invocable and s.name not in self._disabled]

    def get_model_invocable(self) -> list[Skill]:
        """Get enabled skills the model can auto-invoke."""
        return [s for s in self._skills.values()
                if s.when_to_use and s.name not in self._disabled]

    def activate_for_paths(self, file_paths: list[str]):
        """Activate conditional skills matching the given file paths."""
        for name, skill in list(self._conditional_skills.items()):
            if name in self._activated_conditional:
                continue
            for fp in file_paths:
                if skill.matches_path(fp):
                    self._skills[name] = skill
                    self._activated_conditional.add(name)
                    break

    def clear(self):
        """Clear all skills."""
        self._skills.clear()
        self._conditional_skills.clear()
        self._activated_conditional.clear()


# Global registry
_registry = SkillRegistry()


def get_registry() -> SkillRegistry:
    return _registry


# ---------------------------------------------------------------------------
# Loading skills from disk
# ---------------------------------------------------------------------------

def _load_skill_from_file(skill_path: str, source: str = "project") -> Optional[Skill]:
    """Load a single skill from a SKILL.md or .md file."""
    try:
        content = Path(skill_path).read_text(encoding="utf-8")
    except Exception:
        return None

    raw_meta, body = parse_frontmatter(content)
    meta = normalize_metadata(raw_meta)

    # Determine skill name
    parent_dir = os.path.basename(os.path.dirname(skill_path))
    file_name = Path(skill_path).stem
    name = meta.get("name", parent_dir if file_name.upper() == "SKILL" else file_name)

    # Parse allowed_tools
    allowed_tools_str = meta.get("allowed_tools", "")
    allowed_tools = [t.strip() for t in allowed_tools_str.split(",") if t.strip()] if allowed_tools_str else []

    return Skill(
        name=name,
        description=meta.get("description", ""),
        when_to_use=meta.get("when_to_use", ""),
        user_invocable=meta.get("user_invocable", True),
        argument_hint=meta.get("argument_hint", ""),
        allowed_tools=allowed_tools,
        model=meta.get("model"),
        context=meta.get("context", "inline"),
        paths=meta.get("paths"),
        source=source,
        source_path=skill_path,
        prompt_text=body,
        skill_dir=os.path.dirname(skill_path),
    )


def _scan_skills_dir(directory: str, source: str) -> list[Skill]:
    """Scan a directory for skill definitions."""
    skills = []
    if not os.path.isdir(directory):
        return skills

    for entry in os.listdir(directory):
        entry_path = os.path.join(directory, entry)

        # Directory format: skill-name/SKILL.md
        if os.path.isdir(entry_path):
            skill_file = os.path.join(entry_path, "SKILL.md")
            if os.path.isfile(skill_file):
                skill = _load_skill_from_file(skill_file, source)
                if skill:
                    skills.append(skill)
            continue

        # Flat file format: skill-name.md
        if entry.endswith(".md") and not entry.startswith("."):
            skill = _load_skill_from_file(entry_path, source)
            if skill:
                skills.append(skill)

    return skills


def load_skills(cwd: str):
    """
    Load skills from all sources (following original Claude Code precedence):
    1. User skills:    ~/.claude/skills/
    2. Project skills: .claude/skills/ (walk up to home)
    3. Legacy:         .claude/commands/ (deprecated)
    """
    registry = get_registry()

    # 1. User skills: ~/.claude/skills/
    user_skills_dir = os.path.join(Path.home(), ".claude", "skills")
    for skill in _scan_skills_dir(user_skills_dir, "user"):
        registry.register(skill)

    # 2. Project skills: walk up from cwd to home looking for .claude/skills/
    current = os.path.abspath(cwd)
    home = str(Path.home())
    seen_dirs: set[str] = set()

    while True:
        skills_dir = os.path.join(current, ".claude", "skills")
        if skills_dir not in seen_dirs and os.path.isdir(skills_dir):
            seen_dirs.add(skills_dir)
            for skill in _scan_skills_dir(skills_dir, "project"):
                if not registry.get(skill.name):
                    registry.register(skill)

        # Also check legacy .claude/commands/
        commands_dir = os.path.join(current, ".claude", "commands")
        if commands_dir not in seen_dirs and os.path.isdir(commands_dir):
            seen_dirs.add(commands_dir)
            for skill in _scan_skills_dir(commands_dir, "project"):
                if not registry.get(skill.name):
                    registry.register(skill)

        parent = os.path.dirname(current)
        if parent == current or not current.startswith(home):
            break
        current = parent
