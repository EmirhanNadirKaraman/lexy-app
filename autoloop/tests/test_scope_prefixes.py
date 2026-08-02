"""Directory prefixes in `Task.approved_paths`.

Exact paths alone made scope authoring the loop's main source of friction:
rt-01 was refused twice for paths it genuinely had to touch. Prefixes relax how
scope is EXPRESSED without relaxing what is enforced -- the executor's own
report still never defines its authorization (docs/SECURITY.md finding #2).
"""

from __future__ import annotations

import pytest

from autoloop.errors import TaskGraphError
from autoloop.tasks import (
    Task,
    TaskRegistry,
    is_directory_prefix,
    unauthorized_paths,
)

PREFIX = ("lexy-app/backend/routers/", "docs/SECURITY.md")


def test_a_prefix_authorizes_everything_beneath_it():
    assert unauthorized_paths(
        {"lexy-app/backend/routers/books.py",
         "lexy-app/backend/routers/nested/deep.py"}, PREFIX
    ) == set()


def test_a_prefix_stops_at_the_segment_boundary():
    """The trailing slash is what makes this safe: `routers/` must never
    authorize `routers_backup/`, which a bare string-prefix check would."""
    assert unauthorized_paths({"lexy-app/backend/routers_backup/secret.py"}, PREFIX) == {
        "lexy-app/backend/routers_backup/secret.py"
    }


def test_an_exact_entry_authorizes_only_that_file():
    """Naming a file must not quietly authorize its directory."""
    assert unauthorized_paths({"docs/TESTS.md"}, PREFIX) == {"docs/TESTS.md"}
    assert unauthorized_paths({"docs/SECURITY.md"}, PREFIX) == set()


def test_unrelated_paths_are_still_refused():
    assert unauthorized_paths({"autoloop/policy.py"}, PREFIX) == {"autoloop/policy.py"}


def test_is_directory_prefix_is_the_trailing_slash():
    assert is_directory_prefix("a/b/")
    assert not is_directory_prefix("a/b.py")


# ---- what a prefix may NOT be ------------------------------------------------


def test_a_bare_separator_cannot_authorize_the_whole_repo():
    """Refused as an ABSOLUTE path, which is the rule that fires first. Worth a
    test anyway: "/" authorizing the entire repository is the worst outcome
    prefixes could produce, so the behaviour is pinned regardless of which
    check catches it."""
    with pytest.raises(TaskGraphError, match="repository-relative"):
        TaskRegistry().add_many([Task(id="t", title="T", description="d",
                                      approved_paths=("/",))])


@pytest.mark.parametrize("bad", ["../escape/", "a/../b/", "a//b/", "-rf/", "a b/", "src/*/"])
def test_prefixes_are_held_to_the_same_rules_as_paths(bad):
    """Relaxing expression must not relax validation: no traversal, no globs,
    no whitespace, no leading '-'."""
    with pytest.raises(TaskGraphError, match="bad_approved_path"):
        TaskRegistry().add_many([Task(id="t", title="T", description="d",
                                      approved_paths=(bad,))])


# ---- leading dot / underscore, previously unrepresentable --------------------


@pytest.mark.parametrize("path", [
    "lexy-app/backend/tests/_auth_helper.py",
    ".gitignore",
    "docs/.keep",
])
def test_ordinary_files_starting_with_dot_or_underscore_are_accepted(path):
    """The old pattern demanded an alphanumeric first character, so real
    repository files were unrepresentable while the error text claimed '_' was
    legal. '.' and '..' segments are refused separately -- that is the check
    that actually matters."""
    registry = TaskRegistry()
    registry.add_many([Task(id="t", title="T", description="d", approved_paths=(path,))])
    assert registry.get("t").approved_paths == (path,)


def test_a_leading_dash_is_still_refused():
    """Still refused: a leading '-' is the flag-injection habit this validator
    exists to stop."""
    with pytest.raises(TaskGraphError, match="bad_approved_path"):
        TaskRegistry().add_many([Task(id="t", title="T", description="d",
                                      approved_paths=("-rf.py",))])
