"""Suggest the files a task will probably touch, from its own words.

Typing `approved_paths` by hand is the tedious part of authoring a task, and
tedium is how a scope ends up copied from a neighbouring task or left empty.
This proposes a list; the operator reads it and submits it.

**A suggestion is not an authorization, and the distinction is the whole
design.** `approved_paths` is what a write-capable agent may write, and
`docs/SECURITY.md` finding #2 exists because the executor's own report must
never define its own scope. Anything that derived the scope automatically at
merge or dispatch time would rebuild that circularity with extra steps — the
task would arrive carrying its own permission slip. So this runs at AUTHORING
time, into a form field a human then reads, edits and submits. Nothing here
writes to the inbox, the registry, or anything else.

Deliberately NOT an LLM. Every suggestion is a mechanical consequence of text
the operator wrote plus files that exist, so each one can be explained in a
short phrase — and a suggestion you cannot explain is one you cannot check.
The three sources, most trustworthy first:

1. **A path the text already names**, which exists, or whose parent directory
   exists (a file the task is about to create).
2. **A bare filename** that resolves to exactly ONE tracked file. Ambiguous
   names are dropped rather than guessed — offering the wrong `models.py`
   is worse than offering nothing.
3. **An identifier** (`snake_case` or `CamelCase`) defined in exactly one
   tracked file. Same rule: one match or none.

Read-only, and every git call carries `--no-optional-locks` — a plain
`git status`/`ls-files` can rewrite `.git/index`, and a dirty checkout makes
the escape detector refuse the loop's next write-capable task.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Bound on what comes back. A form pre-filled with forty paths is not read,
#: it is accepted — which would defeat the confirmation this exists to keep.
MAX_SUGGESTIONS = 12

#: Extensions worth proposing. Excludes lockfiles, images and build output:
#: a task that genuinely needs one of those is rare enough to type by hand.
CODE_SUFFIXES = frozenset(
    {".py", ".ts", ".tsx", ".js", ".jsx", ".md", ".sql", ".yml", ".yaml", ".toml", ".ini"}
)

#: A repo-relative path written out in the text.
_PATH_RE = re.compile(r"(?<![\w/.-])([A-Za-z0-9_.][A-Za-z0-9_./-]*/[A-Za-z0-9_./-]+)")
#: A bare filename with a known suffix.
_FILENAME_RE = re.compile(r"(?<![\w/.-])([A-Za-z0-9_.-]+\.(?:py|ts|tsx|js|jsx|md|sql|ya?ml|toml|ini))")
#: An identifier that might be defined somewhere — deliberately SHAPED rather
#: than blocklisted. A bare lowercase word ("report", "count", "failed") is
#: prose that happens to collide with some function name, and matching those
#: produced exactly one confident, wrong suggestion on the first try. Real
#: identifiers in this repo are snake_case or CamelCase, so requiring that
#: shape drops the whole class of collision without a word list to maintain.
_IDENT_RE = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+|[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+)\b")

#: The few snake_case/CamelCase tokens that still are not identifiers. Short
#: on purpose — the shape rule above does the real work, and a long blocklist
#: is a sign the shape rule is wrong rather than a fix for it.
_STOPWORDS = frozenset({"approved_paths", "task_id", "file_path"})


@dataclass(frozen=True)
class Suggestion:
    path: str
    reason: str

    def as_dict(self) -> dict:
        return {"path": self.path, "reason": self.reason}


def tracked_files(repo: Path) -> list[str]:
    """Every tracked path, via git. `--no-optional-locks` because a plain git
    read can rewrite `.git/index`, and a dirty checkout makes the escape
    detector refuse the loop's next write-capable task."""
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "ls-files"],
            cwd=str(repo), capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def _defining_file(repo: Path, identifier: str, files: list[str]) -> str | None:
    """The one tracked file that DEFINES `identifier`, or None.

    A definition, not a mention: `run_validation_commands` is referenced from
    a dozen places and defined in one, and the definition is what a task about
    it will edit. Ambiguity resolves to None — proposing one of four plausible
    files is worse than proposing none, because it looks considered.
    """
    # POSIX ERE, not Python's dialect: `git grep -E` rejects `\s`, `(?:…)`
    # and `\b` outright ("repetition-operator operand invalid"). The first
    # version used them, git failed, and the empty stdout read as "no match" —
    # so identifier detection was silently off. Hence the returncode check
    # below: 0 = matched, 1 = no match, anything else is a broken query, and a
    # broken query must not masquerade as a clean negative.
    pattern = (
        r"^[[:space:]]*(async def|def|class)[[:space:]]+"
        + re.escape(identifier)
        + r"([[:space:]]|\(|:|$)"
    )
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "grep", "-lE", pattern],
            cwd=str(repo), capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode not in (0, 1):
        return None
    hits = [h for h in result.stdout.splitlines() if h.strip() and h in set(files)]
    return hits[0] if len(hits) == 1 else None


def suggest(text: str, repo: Path, limit: int = MAX_SUGGESTIONS) -> list[Suggestion]:
    """Paths this task probably touches, each with the reason it was proposed.

    Ordered by how much the text committed to them: an explicit path first, a
    resolved filename next, an inferred definition last. The reason travels
    with the path so the operator can judge each one rather than trusting the
    list — a suggestion whose reason does not fit the task is the one to
    delete before submitting.
    """
    files = tracked_files(repo)
    tracked = set(files)
    by_name: dict[str, list[str]] = {}
    for path in files:
        by_name.setdefault(Path(path).name, []).append(path)

    out: list[Suggestion] = []
    seen: set[str] = set()

    def add(path: str, reason: str) -> None:
        if path in seen or len(out) >= limit:
            return
        seen.add(path)
        out.append(Suggestion(path, reason))

    # 1. Paths the text names outright.
    for raw in _PATH_RE.findall(text):
        candidate = raw.rstrip(".,;:)")
        if candidate in tracked:
            add(candidate, "named in the description")
        elif candidate.endswith("/") and any(f.startswith(candidate) for f in tracked):
            add(candidate, "directory named in the description")
        elif any(f.startswith(candidate + "/") for f in tracked):
            # A directory written without its trailing slash ("autoloop/tests").
            # The slash is what makes it a prefix rather than an exact file, so
            # it is added here rather than left for the operator to remember.
            add(candidate + "/", "directory named in the description")
        elif (
            Path(candidate).suffix in CODE_SUFFIXES
            and (repo / candidate).parent.is_dir()
        ):
            add(candidate, "named in the description (new file)")

    # 2. Bare filenames, only when they resolve uniquely.
    for name in _FILENAME_RE.findall(text):
        matches = by_name.get(name, [])
        if len(matches) == 1:
            add(matches[0], f"'{name}' resolves here")

    # 3. Identifiers defined in exactly one file.
    for ident in dict.fromkeys(_IDENT_RE.findall(text)):
        if len(out) >= limit:
            break
        if ident.lower() in _STOPWORDS:
            continue
        found = _defining_file(repo, ident, files)
        if found:
            add(found, f"defines {ident}")

    return out[:limit]
