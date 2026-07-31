"""Targeted, atomic rewrite of `browser.conversation_url` in the config file.

A rotation abandons a wedged conversation. If only the state moved, the next
session would load the config, read the old URL, and walk straight back into the
chat the loop just escaped — so the config has to follow. That is the entire
purpose of this module.

Three properties it exists to guarantee:

* **Surgical.** It rewrites exactly the `conversation_url` line inside
  `[browser]` and copies every other byte through unchanged. The config is a
  hand-maintained file full of comments and deliberate settings; round-tripping
  it through a TOML serialiser would silently reformat all of it.
* **Atomic.** Temp file in the same directory + `os.replace`, like
  `StateStore.save`. A crash mid-write can never leave a config that does not
  parse — which, given `load_config` is strict, would make the loop unstartable.
* **Untracked.** The config lives under `state_dir` (`.autoloop/`, gitignored).
  `assert_untracked` refuses to write to a path git is tracking, so a rotation
  can never produce a repository change the operator did not ask for. It is a
  hard refusal, not a warning: a loop that can quietly edit tracked files while
  recovering from a browser fault is a much worse problem than a failed heal.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .errors import ConfigError

#: `conversation_url = "..."` at the start of a line, single or double quoted.
#: Anchored to line start so a commented-out example is never rewritten.
_URL_LINE = re.compile(
    r'^(?P<indent>[ \t]*)conversation_url(?P<gap>[ \t]*=[ \t]*)(?P<quote>["\']).*?(?P=quote)'
    r"(?P<trailer>[ \t]*(?:#.*)?)$",
    re.MULTILINE,
)
#: Section headers, used to confine the rewrite to `[browser]`.
_SECTION = re.compile(r"^[ \t]*\[(?P<name>[^\]]+)\][ \t]*$", re.MULTILINE)


def assert_untracked(path: Path, repo_root: Path) -> None:
    """Raise unless git is verifiably NOT tracking `path`.

    Fails closed: if git cannot be consulted at all (not installed, not a
    repository), that is not treated as "safe to write" — the caller asked for
    a guarantee this function could not obtain.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", str(path)],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConfigError(
            f"cannot verify that {path} is untracked ({exc}); refusing to write it"
        ) from exc
    if result.returncode == 0:
        raise ConfigError(
            f"{path} is tracked by git — refusing to rewrite it automatically. "
            "The autoloop config is expected to live under the gitignored state "
            "directory; move it there, or update conversation_url by hand."
        )


def replace_conversation_url(text: str, new_url: str) -> str:
    """Return `text` with `[browser].conversation_url` set to `new_url`.

    Raises ConfigError when the key is absent from `[browser]` — writing a key
    that was never there could land it in the wrong section, and a config that
    means something other than what the operator wrote is worse than no heal.
    """
    if '"' in new_url or "\n" in new_url:
        # The URL comes from the browser's own address bar, and the caller has
        # already matched it against the conversation-URL pattern. This is the
        # belt to that braces: a quote here would end the TOML string early and
        # turn the rest of the line into syntax.
        raise ConfigError(f"refusing to write a conversation URL containing quotes: {new_url!r}")

    browser_start, browser_end = _browser_span(text)
    section = text[browser_start:browser_end]
    replacement = rf'\g<indent>conversation_url\g<gap>\g<quote>{new_url}\g<quote>\g<trailer>'
    patched, count = _URL_LINE.subn(replacement, section, count=1)
    if count == 0:
        raise ConfigError(
            "no conversation_url key found in the [browser] section; refusing to "
            "invent one — update the config by hand"
        )
    return text[:browser_start] + patched + text[browser_end:]


def _browser_span(text: str) -> tuple[int, int]:
    for match in _SECTION.finditer(text):
        if match.group("name").strip() != "browser":
            continue
        start = match.end()
        following = _SECTION.search(text, start)
        return start, following.start() if following else len(text)
    raise ConfigError("no [browser] section found in the config file")


def update_conversation_url(config_path: Path, new_url: str, repo_root: Path) -> None:
    """Point the config at `new_url`, atomically, refusing tracked files."""
    config_path = Path(config_path)
    assert_untracked(config_path, repo_root)
    original = config_path.read_text(encoding="utf-8")
    patched = replace_conversation_url(original, new_url)
    tmp = config_path.with_name(config_path.name + ".tmp")
    tmp.write_text(patched, encoding="utf-8")
    os.replace(tmp, config_path)
