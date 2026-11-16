"""Read project metadata without treating document prose as authorization."""

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable, List, Mapping, Optional

from .errors import BlockedError


_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_HEADING = re.compile(r" {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FIELD = re.compile(
    r" {0,3}[-*+][ \t]+(agent_development|状態|優先度)[ \t]*[:：][ \t]*(.*?)[ \t]*$"
)
_STATES = frozenset(("構想", "開発中", "完了", "中断"))
_ACTIVE_STATES = frozenset(("構想", "開発中"))
_EXCLUDED_RUNTIME_STATES = frozenset(("review", "completed", "blocked"))


@dataclass(frozen=True)
class Idea:
    id: str
    title: str
    content: str
    development: str
    status: str
    priority: Optional[int]


def _visible_lines(content: str) -> Iterable[str]:
    # HTML comments and fenced examples are document content, not metadata.
    uncommented = re.sub(r"<!--.*?(?:-->|\Z)", "", content, flags=re.DOTALL)
    fence_char = None
    fence_length = 0
    for line in uncommented.splitlines():
        opening = re.match(r" {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence_char is not None:
            if re.fullmatch(r" {0,3}" + re.escape(fence_char) + "{" + str(fence_length) + r",}[ \t]*", line):
                fence_char = None
            continue
        if opening:
            marker, suffix = opening.groups()
            if marker[0] == "`" and "`" in suffix:
                # Backticks in a backtick fence's info string are invalid.
                yield line
                continue
            fence_char = marker[0]
            fence_length = len(marker)
            continue
        yield line


def parse_idea(id: str, content: str) -> Idea:
    """Return effective permissions; malformed metadata is always denied.

    No file is rewritten. Invalid status or priority also makes the project
    ineligible, even when the original document contains ``allowed``.
    """
    title = id
    basic_count = 0
    in_basic = False
    fields = {"agent_development": [], "状態": [], "優先度": []}
    for line in _visible_lines(content):
        heading = _HEADING.fullmatch(line)
        if heading:
            level, name = heading.groups()
            if len(level) == 1 and title == id:
                title = name.strip()
            if len(level) <= 2:
                in_basic = len(level) == 2 and name.strip() == "基本情報"
                if in_basic:
                    basic_count += 1
            # Nested headings are not part of the basic-information contract.
            elif in_basic:
                in_basic = False
            continue
        if in_basic:
            match = _FIELD.fullmatch(line)
            if match:
                fields[match.group(1)].append(match.group(2))

    unique = basic_count == 1 and all(len(values) == 1 for values in fields.values())
    status = fields["状態"][0] if len(fields["状態"]) == 1 else ""
    priority = None
    valid_priority = False
    if len(fields["優先度"]) == 1:
        raw_priority = fields["優先度"][0]
        if raw_priority == "未設定":
            valid_priority = True
        elif re.fullmatch(r"[1-9][0-9]*", raw_priority):
            try:
                priority = int(raw_priority)
                valid_priority = True
            except ValueError:
                pass

    allowed = (
        bool(_ID.fullmatch(id))
        and unique
        and fields["agent_development"] == ["allowed"]
        and status in _STATES
        and valid_priority
    )
    return Idea(id, title, content, "allowed" if allowed else "denied", status, priority)


def read_ideas(root: Path) -> List[Idea]:
    """Discover exactly ``<root>/<kebab-id>/idea.md``, without following links."""
    root = Path(root)
    if not root.is_dir():
        raise BlockedError("The idea repository directory is unavailable.", "ideas_unavailable")
    ideas = []
    for directory in sorted(root.iterdir(), key=lambda path: path.name):
        if not _ID.fullmatch(directory.name) or directory.is_symlink() or not directory.is_dir():
            continue
        file = directory / "idea.md"
        if file.is_symlink() or not file.is_file():
            continue
        try:
            content = file.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            # Unreadable files must never become development candidates.
            continue
        ideas.append(parse_idea(directory.name, content))
    return ideas


def select_candidates(ideas: Iterable[Idea], projects: Mapping) -> List[Idea]:
    """Order authorized projects deterministically without changing metadata."""
    candidates = []
    for idea in ideas:
        if idea.development != "allowed" or idea.status not in _ACTIVE_STATES:
            continue
        if idea.priority is not None and (type(idea.priority) is not int or idea.priority <= 0):
            continue
        project = projects.get(idea.id, {})
        if not isinstance(project, Mapping):
            continue
        if project.get("status") in _EXCLUDED_RUNTIME_STATES:
            continue
        candidates.append(idea)

    def order(idea):
        runtime = projects.get(idea.id, {})
        unfinished = (idea.status == "開発中" or runtime.get("status") in ("paused", "running")
                      or runtime.get("status") == "failed" and bool(runtime.get("repository_id")))
        return (not unfinished, idea.priority is None, idea.priority or 0, idea.id)

    return sorted(candidates, key=order)
