"""Per-commit test selection: which tests a validated commit actually needs.

The thing under test is `validation.select_validation_commands` and its
integration into `orchestrator._run_post_commit_validation`. Two properties
matter more than the rest and are pinned first:

* a test file the commit did NOT touch still runs when it can reach the changed
  code through the import graph — the failure a filename-matching rule shipped
  on 2026-08-06 (auto-01's f06454b5), reproduced here against the REAL
  repository rather than a fixture, because a fixture would only prove the
  fixture;
* every case where the answer cannot be established runs the whole suite. Read
  that with the third block below: since select-01 the unestablished thing is a
  single PATH rather than the whole commit, so the widening is per-path — but
  nothing narrows on an answer the model does not have.

A block in the middle pins the resolution rule those two properties rest on: an
import NAME is looked up in the importing file's own directory context, not only
at the repo root, because `autoloop/tests/` is not a package and its modules
import each other by bare name — including this one, twice, at the top.

A block after that one pins what happens to a changed path the graph CANNOT
resolve — a `.md`, a `.toml`, a fixture. Until select-01 (2026-08-26) one such
path discarded the answer for every path that did resolve and the run went
full-suite, which fired on 146 of 154 rounds because the documentation trackers
are edited by design on every round. The fallback is now per-path: an
unresolvable path is attributed its own conservative set from repository content
references, the whole run widens only when a SPECIFIC path can be attributed
nothing, and the reason names that path. The soundness claim those tests carry
is the one that matters — no test that names a tracker is ever skipped on a
tracker change — and it is asserted against the REAL repository over the whole
population, not against a fixture that would only prove the fixture.

A third block, at the bottom, pins BOTH PHASES. A loop round validates twice —
once inside `ImplementExecutor` before the commit, once against the committed
worktree — and since val-04 (2026-08-27) both are narrowed by this same
selector. The block asserts that against a real executor over a real git worker
repo: the authoritative pre-commit run narrows, both refusal rules still refuse,
the operator's one setting reaches both ends, a failing narrowed run still
throws the round away while saying what it ran, and the two phases execute the
SAME argv for the same change. It also pins the consequence the review packet
now states (`validation.PRECOMMIT_EVIDENCE`): a narrowed round runs no full
suite at either phase, so the sentence claiming the subset was ADDED to a
full-suite run cannot quietly come back.
"""

from __future__ import annotations

from inspect import signature
from pathlib import Path

import pytest

from autoloop.config import AuditConfig, AutoloopConfig, BrowserConfig
from autoloop.git_gateway import GitGateway
from autoloop.implement_executor import ImplementExecutor
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.tasks import Task
from autoloop.validation import (
    TEST_SELECTION_FULL,
    TEST_SELECTION_REACHABLE,
    _files_referencing,
    _reference_tokens,
    build_import_graph,
    select_validation_commands,
)
from autoloop.worktask import TaskExecution

# Sibling test modules, importable because pytest's prepend import mode puts
# this directory on `sys.path` — the same borrowing `test_rounds_and_restart.py`
# and `test_codex_provider.py` already do for `build`/`FakeGit`.
from test_implement_executor import (
    FakeAgentRunner,
    implement_directive,
    make_agent_runner_factory,
    run_git,
)
from test_orchestrator import URL, build_postcommit

REPO_ROOT = Path(__file__).resolve().parents[2]

#: `build_import_graph(REPO_ROOT)` costs an `ast.parse` of every `.py` file in
#: the checkout, and several tests here need the same answer about the same
#: tree — which does not change while the suite runs. Cached per process, so
#: adding a real-repository test costs an assertion rather than another walk.
_REAL_GRAPH = None


def real_repository_graph():
    global _REAL_GRAPH
    if _REAL_GRAPH is None:
        _REAL_GRAPH = build_import_graph(REPO_ROOT)
    return _REAL_GRAPH


RUFF = ("ruff", "check", ".")
SUITE = ("python3", "-m", "pytest", "suite", "-q", "-n", "auto", "-p", "no:cacheprovider")
OTHER_SUITE = ("python3", "-m", "pytest", "other", "-q", "-p", "no:cacheprovider")
COMMANDS = (RUFF, SUITE)


def write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def scaffold(root: Path) -> Path:
    """The fixture's file tree, without pytest wiring, so a test that needs the
    same shape inside a real git repository can build one (see
    `git_scaffold`)."""
    write(root, "pkg/__init__.py", "")
    write(root, "pkg/publisher.py", "def publish():\n    return 1\n")
    write(
        root,
        "pkg/orchestrator.py",
        "from .publisher import publish\n\n\ndef run():\n    return publish()\n",
    )
    write(root, "pkg/lonely.py", "def unused():\n    return 0\n")
    write(
        root,
        "suite/test_lonely.py",
        "from pkg.lonely import unused\n\n\ndef test_unused():\n"
        "    assert unused() == 0\n",
    )
    write(root, "suite/conftest.py", "import pytest\n")
    write(
        root,
        "suite/test_smoke.py",
        "from pkg.publisher import publish\n\n\ndef test_publish():\n    assert publish()\n",
    )
    write(
        root,
        "suite/test_orchestra.py",
        "from pkg.orchestrator import run\n\n\ndef test_run():\n    assert run()\n",
    )
    write(
        root,
        "suite/test_unrelated.py",
        "def test_arithmetic():\n    assert 1 + 1 == 2\n",
    )
    write(root, "docs/NOTES.md", "notes\n")
    return root


def git_scaffold(root: Path, branch: str) -> Path:
    """`scaffold`, committed, in a real git repository — what an executor round
    needs so `git status` reports only what the agent went on to change."""
    root.mkdir(parents=True, exist_ok=True)
    run_git(root, "init", "-q", "-b", branch)
    run_git(root, "config", "user.email", "t@e.c")
    run_git(root, "config", "user.name", "T")
    run_git(root, "config", "commit.gpgsign", "false")
    scaffold(root)
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def repo(tmp_path):
    """A miniature repository with the shape the selector cares about.

    `suite/test_smoke.py` is the fixture's version of `test_v1_smoke.py`: it
    imports a production module directly and shares no part of its name with
    anything, so only reachability can find it.

    `pkg/lonely.py` is the opposite shape — reached by exactly ONE test and by
    nothing else — which is what makes "an unreachable test is omitted"
    testable. It deliberately has a test of its own: a module no test reaches at
    all widens to the full suite (see
    `test_zero_reachable_tests_runs_the_full_suite`, which pins that on its own
    fixture), so without `suite/test_lonely.py` a change here would prove the
    widening rule rather than the omission.

    Named `project/` rather than `repo/` so it cannot collide with the real git
    checkout `build_postcommit` creates at `tmp_path / "repo"`.
    """
    return scaffold(tmp_path / "project")


def selection(repo, changed, commands=COMMANDS, **kwargs):
    return select_validation_commands(commands, changed, repo, **kwargs)


# ---- the property the whole feature exists for ------------------------------


def test_an_untouched_test_file_runs_when_the_code_it_imports_changed(repo):
    """The commit touched `pkg/publisher.py` and nothing else. Nothing in the
    repository is called `test_publisher.py`, so a name-matching rule selects
    NOTHING here — the exact shape of auto-01's regression."""
    chosen = selection(repo, ["pkg/publisher.py"])

    assert not chosen.widened
    assert "suite/test_smoke.py" in chosen.selected, "direct importer"
    assert "suite/test_orchestra.py" in chosen.selected, "transitive importer"
    assert "suite/test_unrelated.py" not in chosen.selected


def test_the_real_repository_selects_test_v1_smoke_for_a_publisher_change():
    """The 2026-08-06 case itself, against the real checkout.

    auto-01's f06454b5 changed the publisher and the protected-branch push
    guards and updated the four test files it touched; all five failures were in
    `autoloop/tests/test_v1_smoke.py`, which it did not touch. That file imports
    `autoloop.publisher` directly, so reachability selects it — and the second
    assertion is what makes the first one mean something: it is selected by an
    import edge, not because the model gave up and marked it opaque.
    """
    graph = real_repository_graph()
    assert "autoloop/tests/test_v1_smoke.py" not in graph.opaque

    chosen = selection(
        REPO_ROOT,
        ["autoloop/publisher.py"],
        commands=(
            RUFF,
            ("python3", "-m", "pytest", "autoloop/tests", "-q", "-n", "auto"),
        ),
    )

    assert not chosen.widened
    assert "autoloop/tests/test_v1_smoke.py" in chosen.selected
    assert len(chosen.selected) < len(graph.test_files), (
        "a run that selects every test file is not a subset, and the assertion "
        "above would pass trivially"
    )
    narrowed = chosen.commands[1]
    assert "autoloop/tests" not in narrowed, "the directory is replaced by files"
    assert "autoloop/tests/test_v1_smoke.py" in narrowed


def test_selection_is_deterministic_for_the_same_diff(repo):
    """Same diff, same answer — including the evidence string, which is what a
    reviewer compares between rounds. Input ORDER must not matter either: git
    hands back a set."""
    first = selection(repo, ["pkg/publisher.py", "pkg/orchestrator.py"])
    second = selection(repo, ["pkg/orchestrator.py", "pkg/publisher.py"])

    assert first.commands == second.commands
    assert first.selected == second.selected
    assert first.evidence() == second.evidence()
    assert list(first.selected) == sorted(first.selected)


def test_an_unreachable_test_is_omitted(repo):
    """The only permission to skip a test: nothing links it to the change.

    `pkg/lonely.py` is imported by `suite/test_lonely.py` and by nothing else,
    so exactly one test file is selected and the other three are omitted —
    each of them because no chain of imports reaches the change, which is the
    ONE ground this selector ever omits a test on.
    """
    chosen = selection(repo, ["pkg/lonely.py"])

    assert not chosen.widened
    assert chosen.selected == ("suite/test_lonely.py",)
    assert "suite/test_unrelated.py" not in chosen.selected
    assert "suite/test_smoke.py" not in chosen.selected


# ---- bare sibling imports are real edges ------------------------------------
#
# `autoloop/tests/` has no `__init__.py`, so pytest's prepend import mode puts it
# on `sys.path` and the files in it import each other by BARE name — this module
# does it twice, at the top. Resolving an import name only against the repo root
# made `test_implement_executor` match nothing and dropped the edge silently, so
# a change to an imported test/helper module selected the changed file and
# omitted the untouched modules that import and execute it (found in review,
# 2026-08-20). Both tests below fail on that resolution and pass on the current
# one.


def test_a_bare_sibling_import_is_a_real_edge_in_the_real_repository():
    """The repository's own pattern, asserted as an EDGE rather than as a
    selection.

    The selection assertion alone would be vacuous here: this file contains the
    string `"python3"` (the `SUITE` constant above), which puts it in
    `graph.opaque` and therefore on the frontier for every change — it would come
    back "selected" even with the edge missing. The `importers` assertion is the
    one that can tell the two apart, and it is false unless the bare name
    resolves in this file's own directory.
    """
    graph = real_repository_graph()
    me = "autoloop/tests/test_test_selection.py"

    for imported in (
        "autoloop/tests/test_implement_executor.py",
        "autoloop/tests/test_orchestrator.py",
    ):
        assert me in graph.importers.get(imported, frozenset()), (
            f"the bare `from ... import` of {imported} at the top of this module "
            "is a real import edge and must be in the graph"
        )

    chosen = selection(
        REPO_ROOT,
        ["autoloop/tests/test_implement_executor.py"],
        commands=(
            RUFF,
            ("python3", "-m", "pytest", "autoloop/tests", "-q", "-n", "auto"),
        ),
    )
    assert not chosen.widened
    assert me in chosen.selected


def sibling_tree(root: Path) -> Path:
    """A non-package test directory whose files import each other by bare name.

    Deliberately free of the literals `_file_is_opaque` watches for — no
    `"python"`, no `importlib` — so nothing here is opaque and every selection
    below is carried by an import edge rather than by the frontier.

    The chain is `pkg/core.py` → `suite/helper.py` → `suite/test_middle.py` →
    `suite/test_outer.py`, and only the FIRST hop is a dotted import; the other
    two are bare sibling names. `suite/test_apart.py` joins nothing, which is
    what distinguishes reachability from "everything in the directory".
    """
    write(root, "pkg/__init__.py", "")
    write(root, "pkg/core.py", "def core():\n    return 7\n")
    write(
        root,
        "suite/helper.py",
        "from pkg.core import core\n\n\ndef helped():\n    return core()\n",
    )
    write(
        root,
        "suite/test_middle.py",
        "from helper import helped\n\n\ndef test_middle():\n    assert helped() == 7\n",
    )
    write(
        root,
        "suite/test_outer.py",
        "from test_middle import test_middle\n\n\ndef test_outer():\n"
        "    test_middle()\n",
    )
    write(root, "suite/test_apart.py", "def test_apart():\n    assert True\n")
    return root


def test_a_sibling_import_edge_is_transitive_and_deterministic(tmp_path):
    """Two bare hops away from the changed production module, and still selected.

    `suite/test_outer.py` imports no production code at all — its only route to
    `pkg/core.py` runs through two sibling test modules — which is the shape the
    repo-root-only resolution could not see.
    """
    root = sibling_tree(tmp_path / "siblings")
    graph = build_import_graph(root)

    assert not graph.opaque, "an opaque file would make the assertions below vacuous"
    assert "suite/test_middle.py" in graph.importers["suite/helper.py"]
    assert "suite/test_outer.py" in graph.importers["suite/test_middle.py"]

    first = select_validation_commands(COMMANDS, ["pkg/core.py"], root)
    second = select_validation_commands(COMMANDS, ["pkg/core.py"], root)

    assert not first.widened
    assert first.selected == ("suite/test_middle.py", "suite/test_outer.py")
    assert "suite/test_apart.py" not in first.selected
    assert first.selected == second.selected, "same diff, same answer"
    assert first.commands == second.commands
    assert first.evidence() == second.evidence()


def test_a_changed_sibling_helper_selects_the_modules_that_import_it(tmp_path):
    """The reviewer's case in miniature: the changed file is itself a module
    other test files import by bare name, and they run too."""
    root = sibling_tree(tmp_path / "helper-change")

    chosen = select_validation_commands(COMMANDS, ["suite/helper.py"], root)

    assert not chosen.widened
    assert chosen.selected == ("suite/test_middle.py", "suite/test_outer.py")


def test_an_ambiguous_bare_import_gets_an_edge_to_every_candidate(tmp_path):
    """Two files can answer to one bare name — one at the repo root, one beside
    the importer — and which wins depends on `sys.path` order this cannot know.
    So it does not choose: both get the edge, because an extra edge only runs
    more tests while a wrong choice drops a test that executes the change."""
    root = tmp_path / "ambiguous"
    write(root, "shared.py", "VALUE = 1\n")
    write(root, "suite/shared.py", "VALUE = 2\n")
    write(
        root,
        "suite/test_uses.py",
        "from shared import VALUE\n\n\ndef test_value():\n    assert VALUE\n",
    )
    graph = build_import_graph(root)

    assert "suite/test_uses.py" in graph.importers["shared.py"]
    assert "suite/test_uses.py" in graph.importers["suite/shared.py"]
    for changed in ("shared.py", "suite/shared.py"):
        chosen = select_validation_commands(COMMANDS, [changed], root)
        assert chosen.selected == ("suite/test_uses.py",), changed


# Nothing here pins the ABSENCE of an edge. `_import_roots` excludes package
# directories (Python 3 has no implicit relative imports, so a bare name inside
# `autoloop/` does not reach `autoloop/publisher.py` — `from .publisher import`
# does, and `_imported_modules` already resolves that), but that is a precision
# choice, not a guarantee. Asserting it would make an under-selection into a
# contract, and the direction this model is allowed to be wrong in is the other
# one: a directory that gains an `__init__.py` while still being imported from by
# bare name is a reason to widen the rule, not to defend a test of it.


# ---- what the graph cannot see is never assumed away ------------------------


def test_a_test_using_a_dynamic_import_is_selected_for_every_change(repo):
    write(
        repo,
        "suite/test_dynamic.py",
        "import importlib\n\n\ndef test_dynamic():\n"
        "    assert importlib.import_module('pkg.publisher')\n",
    )
    graph = build_import_graph(repo)
    assert "suite/test_dynamic.py" in graph.opaque

    chosen = selection(repo, ["pkg/lonely.py"])
    assert "suite/test_dynamic.py" in chosen.selected


def test_a_test_that_spawns_an_interpreter_is_selected_for_every_change(repo):
    """Subprocess coupling is invisible to an import graph, so it is not
    modelled — the file is put on the frontier instead."""
    write(
        repo,
        "suite/test_subprocess.py",
        "import subprocess\n\n\ndef test_cli():\n"
        "    subprocess.run(['python3', '-m', 'pkg'], check=False)\n",
    )
    chosen = selection(repo, ["pkg/lonely.py"])
    assert "suite/test_subprocess.py" in chosen.selected


def test_a_production_module_naming_an_interpreter_is_not_opaque(repo):
    """The mirror of the rule above. `run_validation_commands` mentions
    `python3` because launching one is its job; treating every such module as
    opaque would put it on every change's frontier and drag in everything that
    imports it, which is the whole suite by another route."""
    write(
        repo,
        "pkg/runner.py",
        "import subprocess\n\n\ndef go():\n"
        "    return subprocess.run(['python3', '-c', 'pass'], check=False)\n",
    )
    graph = build_import_graph(repo)
    assert "pkg/runner.py" not in graph.opaque


def test_an_unparseable_module_is_selected_for_every_change(repo):
    write(repo, "suite/test_broken.py", "def test_x(:\n")
    graph = build_import_graph(repo)
    assert "suite/test_broken.py" in graph.opaque

    chosen = selection(repo, ["pkg/lonely.py"])
    assert "suite/test_broken.py" in chosen.selected


def test_a_conftest_change_selects_every_test_it_configures(repo):
    """pytest applies a conftest to its whole directory tree and no file
    imports it, so the edge is added explicitly."""
    chosen = selection(repo, ["suite/conftest.py"])

    assert not chosen.widened
    assert "suite/test_unrelated.py" in chosen.selected
    assert "suite/test_smoke.py" in chosen.selected


def test_a_package_init_change_selects_everything_that_imports_the_package(repo):
    chosen = selection(repo, ["pkg/__init__.py"])

    assert not chosen.widened
    assert "suite/test_smoke.py" in chosen.selected


def test_a_changed_test_file_selects_itself(repo):
    chosen = selection(repo, ["suite/test_unrelated.py"])

    assert not chosen.widened
    assert chosen.selected == ("suite/test_unrelated.py",)


# ---- everything ambiguous widens --------------------------------------------


def test_a_non_python_change_runs_the_full_suite(repo):
    """A `.md`, a `.toml` a test reads, a fixture — the import graph models none
    of them.

    Since select-01 that is no longer the END of the story (an unresolvable path
    gets a conservative set of its own — see the block below), but it is still
    the story HERE: not one `.py` file this fixture writes contains `docs`,
    `.md`, `NOTES` or `NOTES.md`, so nothing can be attributed to
    `docs/NOTES.md` and the run widens exactly as it always did. This test is
    left byte-identical on purpose — the local fallback is a narrowing of WHEN
    the full-suite rule fires, not a replacement for it, and a rule that had to
    rewrite this assertion would be the other thing.
    """
    chosen = selection(repo, ["pkg/publisher.py", "docs/NOTES.md"])

    assert chosen.widened
    assert chosen.commands == COMMANDS
    assert "docs/NOTES.md" in chosen.reason


def test_a_changed_path_absent_from_the_tree_runs_the_full_suite(repo):
    """A deleted module has no file to parse and no edges to follow."""
    chosen = selection(repo, ["pkg/deleted.py"])

    assert chosen.widened
    assert chosen.commands == COMMANDS


# ---- one unresolvable path does not veto the others -------------------------
#
# The defect select-01 fixed: ONE changed path the graph could not resolve threw
# away the answer for every path that did. Measured 2026-08-25, that fired on
# 146 of 154 rounds, and on the last day 18 of 18 — always on the documentation
# trackers, which every task edits by design. The tests below pin the union that
# replaced it, the per-path widening that survived it, and the coverage claim
# the whole rule stands on.


def attribution_repo(root: Path) -> Path:
    """`scaffold` plus ONE test file that names the markdown file by hand.

    Nothing else in `scaffold` contains `docs`, `.md`, `NOTES` or `NOTES.md`
    (see `test_a_non_python_change_runs_the_full_suite`), so this single file is
    the entire reference set for `docs/NOTES.md` and every count below is
    exact rather than approximate.
    """
    scaffold(root)
    write(
        root,
        "suite/test_reads_notes.py",
        'from pathlib import Path\n\n\n'
        'def test_notes():\n'
        '    assert Path("docs/NOTES.md").name\n',
    )
    return root


def test_the_reference_tokens_cover_every_way_a_file_can_name_the_path():
    """The rule stated as data: path, basename, stem, extension, ancestors.

    The extension is the one that carries the tracker argument — a file that
    sweeps a directory (`rglob("*.md")`) names no tracker and no `docs/`, and it
    is caught by `.md` alone.
    """
    tokens = set(_reference_tokens("docs/SUMMARY.md"))

    assert {"docs/SUMMARY.md", "SUMMARY.md", "SUMMARY", ".md", "docs"} <= tokens


def test_the_ancestor_walk_terminates_on_a_doubled_root_slash():
    """A hang, not a widening, is what an unbounded ancestor walk costs.

    `PurePosixPath("//x").parent` is `//`, whose own parent is `//` again —
    POSIX leaves a leading double slash implementation-defined and pathlib
    preserves it — so stopping on a LIST of known roots never terminates here.
    Git reports repo-relative paths and never this shape, and nothing in the
    selector validates that, which is exactly why the walk is bounded by the
    parent getting shorter instead. If this regresses the suite hangs rather
    than failing, so it is worth having.
    """
    tokens = set(_reference_tokens("//weird/x.md"))

    assert {"x.md", ".md", "x"} <= tokens
    assert "weird" in tokens


def test_an_unresolvable_path_no_longer_vetoes_the_paths_that_resolved(tmp_path):
    """The whole point. One diff, two kinds of path, both used.

    `suite/test_smoke.py` is selected by the import graph from
    `pkg/publisher.py`; `suite/test_reads_notes.py` is selected by content
    reference from `docs/NOTES.md`. Before this change the second path threw the
    first one's answer away and all four test files ran.
    """
    root = attribution_repo(tmp_path / "union")

    chosen = select_validation_commands(
        COMMANDS, ["pkg/publisher.py", "docs/NOTES.md"], root
    )

    assert not chosen.widened, chosen.reason
    assert chosen.resolved == ("pkg/publisher.py",)
    assert chosen.attributed == (("docs/NOTES.md", 1),)
    assert "suite/test_smoke.py" in chosen.selected, "from the resolved half"
    assert "suite/test_reads_notes.py" in chosen.selected, "from the unresolved half"
    assert "suite/test_unrelated.py" not in chosen.selected
    assert "suite/test_lonely.py" not in chosen.selected


def test_a_documentation_only_round_narrows_instead_of_widening(tmp_path):
    """The commonest revise-round shape: no Python changed at all.

    Nothing resolves, so the resolved half contributes nothing and the whole
    selection is attribution. It must still be a SUBSET rather than a full run,
    or a docs-only round pays for the entire suite to learn nothing.
    """
    root = attribution_repo(tmp_path / "docs-only")

    chosen = select_validation_commands(COMMANDS, ["docs/NOTES.md"], root)

    assert not chosen.widened, chosen.reason
    assert chosen.resolved == ()
    assert chosen.selected == ("suite/test_reads_notes.py",)


def test_a_module_that_names_the_path_drags_in_the_tests_that_import_it(tmp_path):
    """Why attribution is CLOSED over import edges instead of stopping at tests.

    `suite/test_via_module.py` never says `docs`, `.md` or `NOTES` — its only
    route to the changed file runs through a production module that holds the
    path. A rule that selected only test files naming the path would skip it,
    and it is the shape a test of a config/tracker READER always has.
    """
    root = tmp_path / "indirect"
    scaffold(root)
    write(root, "pkg/notes.py", 'PATH = "docs/NOTES.md"\n')
    write(
        root,
        "suite/test_via_module.py",
        "from pkg.notes import PATH\n\n\ndef test_path():\n    assert PATH\n",
    )

    chosen = select_validation_commands(COMMANDS, ["docs/NOTES.md"], root)

    assert not chosen.widened, chosen.reason
    assert chosen.selected == ("suite/test_via_module.py",)


def test_an_unparseable_file_is_still_selected_on_a_documentation_only_round(tmp_path):
    """The opaque frontier survives the new path too.

    `test_broken.py` names nothing at all — not `docs`, not `.md` — and is
    selected anyway, because `reachable_from` unions `graph.opaque` into every
    seed set including an attribution one. A file the model cannot read is not
    argued away on the new path any more than it was on the old.
    """
    root = attribution_repo(tmp_path / "opaque-docs")
    write(root, "suite/test_broken.py", "def test_x(:\n")
    graph = build_import_graph(root)
    assert "suite/test_broken.py" in graph.opaque

    chosen = select_validation_commands(COMMANDS, ["docs/NOTES.md"], root)

    assert not chosen.widened, chosen.reason
    assert "suite/test_broken.py" in chosen.selected


def test_a_file_that_cannot_be_read_becomes_a_seed_for_every_path(tmp_path):
    """The fail-open this function could have been, closed rather than argued.

    A scan that quietly DROPS unreadable input is a check that silently passes
    — the alarm never fires and nothing says so. Leaning on
    `build_import_graph` having marked the same file `opaque` would not close
    it either: a file that parsed during the walk and became unreadable a
    moment later is in neither set, so correctness would rest on a race. It is
    seeded for every unresolved path instead, which can only run more tests.
    """
    root = attribution_repo(tmp_path / "unreadable")

    hits = _files_referencing(
        root,
        ["suite/test_reads_notes.py", "suite/vanished.py", "suite/test_unrelated.py"],
        {
            "docs/NOTES.md": _reference_tokens("docs/NOTES.md"),
            "config/settings.toml": _reference_tokens("config/settings.toml"),
        },
    )

    assert hits["docs/NOTES.md"] == frozenset(
        {"suite/test_reads_notes.py", "suite/vanished.py"}
    )
    assert hits["config/settings.toml"] == frozenset({"suite/vanished.py"}), (
        "a path nothing NAMES still gets the unreadable file, and nothing else"
    )


def test_an_unresolvable_path_nothing_names_widens_and_the_reason_names_it(repo):
    """The fallback that survived, and the evidence requirement on it: a reader
    gets the PATH that forced the full run, not a count of paths."""
    chosen = selection(repo, ["pkg/publisher.py", "docs/NOTES.md"])

    assert chosen.widened
    assert chosen.unattributed == ("docs/NOTES.md",)
    assert chosen.resolved == ("pkg/publisher.py",)
    assert "docs/NOTES.md" in chosen.evidence()
    assert "no repository file names them" in chosen.reason


def test_a_deleted_python_module_is_named_as_the_cause(repo):
    """A `.py` path the graph does not hold is the one unresolvable kind that
    cannot be attributed: its importers name it as a dotted module, never as a
    path, so a content scan cannot find the very files a deletion breaks."""
    chosen = selection(repo, ["pkg/deleted.py"])

    assert chosen.widened
    assert chosen.unattributed == ("pkg/deleted.py",)
    assert "pkg/deleted.py" in chosen.reason
    assert "absent from the import graph" in chosen.reason


def test_one_unattributable_path_widens_but_the_accounting_shows_the_rest(tmp_path):
    """A widened run still has to account for the paths that DID resolve.

    "Nothing could be established" and "one path out of three forced this" are
    different evidence, and the counts are what tells them apart.
    """
    root = attribution_repo(tmp_path / "mixed")

    chosen = select_validation_commands(
        COMMANDS,
        ["pkg/publisher.py", "docs/NOTES.md", "config/settings.toml"],
        root,
    )

    assert chosen.widened
    assert chosen.commands == COMMANDS
    assert chosen.resolved == ("pkg/publisher.py",)
    assert chosen.attributed == (("docs/NOTES.md", 1),)
    assert chosen.unattributed == ("config/settings.toml",)
    evidence = chosen.evidence()
    assert "FULL SUITE" in evidence
    assert "config/settings.toml" in evidence
    assert "3 changed path(s)" in evidence
    assert "1 resolved as Python modules" in evidence
    assert "docs/NOTES.md -> 1 test file(s)" in evidence


def test_the_accounting_is_absent_when_the_graph_was_never_consulted(repo):
    """`mode="full"` short-circuits before any graph work. Reporting "0
    resolved" there would read as a failure to resolve rather than as work never
    done, which is the kind of true-but-misleading count this record exists to
    avoid."""
    asked = selection(repo, ["pkg/publisher.py"], mode=TEST_SELECTION_FULL)
    consulted = selection(repo, ["pkg/publisher.py", "docs/NOTES.md"])

    assert "Path accounting" not in asked.evidence()
    assert asked.graph_consulted is False
    assert "Path accounting" in consulted.evidence()
    assert consulted.graph_consulted is True


def test_a_narrowed_round_reports_its_accounting(tmp_path):
    root = attribution_repo(tmp_path / "narrowed-accounting")

    evidence = select_validation_commands(
        COMMANDS, ["pkg/publisher.py", "docs/NOTES.md"], root
    ).evidence()

    assert "SUBSET" in evidence
    assert "2 changed path(s)" in evidence
    assert "1 resolved as Python modules" in evidence
    assert "docs/NOTES.md -> 1 test file(s)" in evidence


def test_attribution_is_deterministic_and_order_independent(tmp_path):
    """Same diff, same answer — including the evidence string, which now carries
    per-path counts a reviewer compares between rounds."""
    root = attribution_repo(tmp_path / "stable")

    first = select_validation_commands(
        COMMANDS, ["docs/NOTES.md", "pkg/publisher.py"], root
    )
    second = select_validation_commands(
        COMMANDS, ["pkg/publisher.py", "docs/NOTES.md"], root
    )

    assert first.selected == second.selected
    assert first.attributed == second.attributed
    assert first.commands == second.commands
    assert first.evidence() == second.evidence()


# ---- the same rule against the REAL repository ------------------------------


TRACKER_NAMES = (
    "SUMMARY.md",
    "TESTS.md",
    "COMMON_ERRORS.md",
    "SECURITY.md",
    "SCHEMA.md",
    "CLAUDE.md",
    "AUTOLOOP.md",
)

AUTOLOOP_SUITE = ("python3", "-m", "pytest", "autoloop/tests", "-q", "-n", "auto")
ROOT_SUITE = ("python3", "-m", "pytest", "tests/", "-q", "-n", "auto")

#: The DONE-WHEN round, computed once for this module for the same reason
#: `real_repository_graph` is: `select_validation_commands` walks the whole
#: checkout, and both real-repository tests below ask about the SAME round.
_TRACKER_ROUND = None


def tracker_round_selection():
    """One `autoloop/` module beside the tracker markdown every task updates."""
    global _TRACKER_ROUND
    if _TRACKER_ROUND is None:
        _TRACKER_ROUND = selection(
            REPO_ROOT,
            ["autoloop/merge_sweep.py", "docs/SUMMARY.md", "docs/TESTS.md"],
            commands=(RUFF, AUTOLOOP_SUITE, ROOT_SUITE),
        )
    return _TRACKER_ROUND


def test_a_module_plus_tracker_markdown_narrows_in_the_real_repository():
    """DONE-WHEN, asserted against the checkout this loop actually validates.

    This is the round shape that produced 18 of 18 full-suite decisions on
    2026-08-25: one `autoloop/` module beside the tracker markdown every task
    updates. It must now come back NARROWED, with each tracker carrying a set
    attributed to it alone.
    """
    chosen = tracker_round_selection()

    assert not chosen.widened, chosen.reason
    assert chosen.resolved == ("autoloop/merge_sweep.py",)
    assert [path for path, _ in chosen.attributed] == ["docs/SUMMARY.md", "docs/TESTS.md"]
    assert all(count > 0 for _, count in chosen.attributed)
    assert "autoloop/tests/test_merge_sweep.py" in chosen.selected


def test_every_test_naming_a_tracker_still_runs_on_a_tracker_change():
    """The soundness claim, over the whole population rather than a sample.

    The brief for select-01 named ~34 test files under `autoloop/tests/` that
    reference a tracker filename and warned that a rule treating markdown as
    inert would skip real coverage — `test_markdown_policy.py` exists to assert
    on markdown handling. This asserts the opposite property directly: not one
    of them is missing from a tracker change's selection. Whether a given file
    reads the repository's own copy or builds its own fixture of the same name
    is never decided, and does not need to be: naming the file is enough.

    The last assertion is what stops the rest being vacuous. This round also
    changes `autoloop/merge_sweep.py`, so a file could in principle be selected
    by ordinary reachability rather than by the markdown at all —
    `test_markdown_policy.py` is NOT reachable from that module (it imports
    `autoloop.audit.markdown` and `autoloop.errors`, neither of which touches
    the sweep), so its presence in `selected` can only have come from
    attribution.
    """
    graph = real_repository_graph()
    referencing = sorted(
        rel
        for rel in graph.test_files
        if rel.startswith("autoloop/tests/")
        and any(
            name in (REPO_ROOT / rel).read_text(encoding="utf-8")
            for name in TRACKER_NAMES
        )
    )
    assert len(referencing) >= 30, "the population the rule has to justify"

    chosen = tracker_round_selection()

    assert not chosen.widened, chosen.reason
    assert [rel for rel in referencing if rel not in chosen.selected] == []
    assert "autoloop/tests/test_docs_merge.py" in chosen.selected
    policy_test = "autoloop/tests/test_markdown_policy.py"
    assert policy_test in chosen.selected
    assert policy_test not in graph.reachable_from(["autoloop/merge_sweep.py"]), (
        "if the module change reached it, this test would pass without the "
        "markdown attribution having done anything"
    )


def test_no_changed_paths_runs_the_full_suite(repo):
    chosen = selection(repo, [])

    assert chosen.widened
    assert chosen.commands == COMMANDS


def test_zero_reachable_tests_runs_the_full_suite(tmp_path):
    """A change that reaches no test at all is likelier a gap in the model than
    a truth about the repository, so it is not treated as licence to run
    nothing."""
    root = tmp_path / "bare"
    write(root, "pkg/__init__.py", "")
    write(root, "pkg/solo.py", "x = 1\n")
    write(root, "suite/test_nothing.py", "def test_ok():\n    assert True\n")

    chosen = select_validation_commands(COMMANDS, ["pkg/solo.py"], root)

    assert chosen.widened
    assert chosen.commands == COMMANDS
    assert "no test file at all" in chosen.reason


# ---- asking for the full suite gets the full suite --------------------------


def test_explicit_full_mode_bypasses_selection(repo):
    chosen = selection(repo, ["pkg/publisher.py"], mode=TEST_SELECTION_FULL)

    assert chosen.widened
    assert chosen.commands == COMMANDS
    assert 'test_selection = "full"' in chosen.reason


def test_a_caller_supplied_reason_bypasses_selection(repo):
    chosen = selection(
        repo, ["pkg/publisher.py"], full_reason="the task declared its own validation"
    )

    assert chosen.widened
    assert chosen.commands == COMMANDS
    assert "declared its own validation" in chosen.evidence()


# ---- command rewriting ------------------------------------------------------


def test_non_test_commands_are_never_touched(repo):
    """ruff still lints the whole tree on a narrowed round — selection decides
    which TESTS run, not which checks."""
    chosen = selection(repo, ["pkg/publisher.py"])

    assert chosen.commands[0] == RUFF


def test_the_narrowed_command_keeps_its_flags_and_names_files(repo):
    chosen = selection(repo, ["pkg/publisher.py"])
    pytest_command = chosen.commands[1]

    assert pytest_command[:3] == ("python3", "-m", "pytest")
    assert "suite" not in pytest_command, "the directory is replaced by files"
    assert "suite/test_smoke.py" in pytest_command
    for flag in ("-q", "-n", "auto", "-p", "no:cacheprovider"):
        assert flag in pytest_command


def test_a_marker_value_is_not_mistaken_for_a_test_path(repo):
    """`-m isolated` selects a marker. Reading `isolated` as a path would make
    the command collect nothing and pass vacuously."""
    marked = ("python3", "-m", "pytest", "suite", "-q", "-m", "isolated")
    chosen = selection(repo, ["pkg/publisher.py"], commands=(marked,))

    assert not chosen.widened
    rewritten = chosen.commands[0]
    assert rewritten[-2:] == ("-m", "isolated")
    assert "suite/test_smoke.py" in rewritten


def test_a_command_with_no_reachable_test_is_skipped_and_says_so(repo):
    """The one case a command is dropped WITHOUT widening the run.

    "No selected test lives under `other/`" is a reachability ANSWER — every
    changed path resolved, the closure was computed, nothing landed there — not
    an unknown. So it is dropped and disclosed while the rest of the run still
    narrows, which is what keeps a repository with more than one test tree from
    widening on every round. Contrast the `_RETARGET_BLOCKED` cases below, where
    the selector cannot read the command at all and the whole run widens.
    """
    write(repo, "other/test_far.py", "def test_far():\n    assert True\n")
    chosen = selection(repo, ["pkg/publisher.py"], commands=(SUITE, OTHER_SUITE))

    assert chosen.widened is False
    assert OTHER_SUITE not in chosen.commands
    assert chosen.skipped and chosen.skipped[0][0] == OTHER_SUITE
    assert "SUBSET" in chosen.evidence()
    assert "SKIPPED" in chosen.evidence()
    assert "other" in chosen.evidence()


def assert_full_suite_because(chosen, original, fragment):
    """A command that could not be retargeted widens the WHOLE run.

    The argv assertion is the weakest one here and used to be the only one:
    it is not enough for the command to come back unchanged, because the
    SELECTION must also report itself as a full-suite run. Reporting
    `widened=False` while handing back a command that narrowed nothing is what
    made `evidence()` claim "each configured pytest command ran the selected
    files" about a command that ran the whole tree (found in review,
    2026-08-20), and a test asserting only the argv passed happily through it.
    """
    assert chosen.widened is True
    assert chosen.commands == (original,), "guessing which token is a path is worse"
    assert "FULL SUITE" in chosen.evidence()
    assert "SUBSET" not in chosen.evidence()
    assert " ".join(original) in chosen.reason, "the reason names the offending command"
    assert fragment in chosen.reason, "and says what about it could not be read"


def test_a_command_with_an_unrecognised_flag_widens_the_whole_run(repo):
    exotic = ("python3", "-m", "pytest", "--nonsense-flag", "suite")
    chosen = selection(repo, ["pkg/publisher.py"], commands=(exotic,))

    assert_full_suite_because(chosen, exotic, "does not recognise")


def test_a_command_declaring_no_paths_widens_the_whole_run(repo):
    """Its surface comes from `pytest.ini`'s `testpaths`; injecting paths would
    change what the command means, and running it whole while calling the round
    a subset would misdescribe it."""
    bare = ("python3", "-m", "pytest", "-q")
    chosen = selection(repo, ["pkg/publisher.py"], commands=(bare,))

    assert_full_suite_because(chosen, bare, "no test paths")


def test_a_node_id_target_widens_the_whole_run(repo):
    pinned = ("python3", "-m", "pytest", "suite/test_smoke.py::test_publish")
    chosen = selection(repo, ["pkg/publisher.py"], commands=(pinned,))

    assert_full_suite_because(chosen, pinned, "node id")


def test_one_unretargetable_command_widens_its_narrowable_neighbours_too(repo):
    """The blocked command does not just keep itself: the run it is part of is
    no longer a subset run, so every OTHER command goes back to configured too.
    A summary saying "SUBSET, these files" beside a command that ran the whole
    tree is precisely the evidence mismatch this rule removes."""
    bare = ("python3", "-m", "pytest", "-q")
    chosen = selection(repo, ["pkg/publisher.py"], commands=(RUFF, SUITE, bare))

    assert chosen.widened is True
    assert chosen.commands == (RUFF, SUITE, bare)
    assert chosen.selected == (), "a widened run selected nothing; it ran everything"


def test_every_unretargetable_command_is_named_not_just_the_first(repo):
    """A reviewer reading FULL SUITE is owed the whole cause, so the blocked
    commands are collected and reported together."""
    bare = ("python3", "-m", "pytest", "-q")
    pinned = ("python3", "-m", "pytest", "suite/test_smoke.py::test_publish")
    chosen = selection(repo, ["pkg/publisher.py"], commands=(bare, pinned))

    assert chosen.widened is True
    assert "no test paths" in chosen.reason
    assert "node id" in chosen.reason


def test_a_configured_list_without_pytest_reports_nothing(repo):
    """No pytest command, no selection to make, nothing to say — the summary a
    ruff-only deployment produces is unchanged."""
    chosen = selection(repo, ["pkg/publisher.py"], commands=(RUFF,))

    assert chosen.commands == (RUFF,)
    assert chosen.evidence() == ""


# ---- the evidence a reviewer reads ------------------------------------------


def test_the_evidence_states_what_was_selected_and_why_it_is_sufficient(repo):
    chosen = selection(repo, ["pkg/publisher.py"])
    evidence = chosen.evidence()

    assert "SUBSET" in evidence
    assert "import-graph reachability" in evidence
    assert "pkg/publisher.py" in evidence, "the changed input considered"
    assert "suite/test_smoke.py" in evidence, "a selected file"
    assert "did not touch" in evidence, "why an untouched file is still covered"
    assert "dynamic import" in evidence, "what the model cannot see"
    assert 'test_selection = "full"' in evidence, "how to widen"


def test_the_evidence_says_so_when_nothing_was_narrowed(repo):
    evidence = selection(repo, ["docs/NOTES.md"]).evidence()

    assert "FULL SUITE" in evidence
    assert "docs/NOTES.md" in evidence


def test_the_evidence_is_bounded(repo):
    """It becomes `state.last_validation` — state.json, the transcript, blocker
    records and the CONTEXT block of every message — so it cannot grow with the
    size of the suite."""
    for index in range(40):
        write(
            repo,
            f"suite/test_generated_{index:02d}.py",
            f"from pkg.publisher import publish\n\n\ndef test_{index}():\n"
            "    assert publish()\n",
        )
    chosen = selection(repo, ["pkg/publisher.py"])

    assert len(chosen.selected) > 20
    assert "more)" in chosen.evidence()
    assert len(chosen.evidence()) < 3000


# ---- integration: what the reviewer actually receives ------------------------


def postcommit_orchestrator(tmp_path, repo, commands, mode=TEST_SELECTION_REACHABLE):
    """An Orchestrator whose post-commit validation grades `repo`, with the
    command list and selection mode under test."""
    orch, *_ = build_postcommit(tmp_path)
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=orch._config.policy,
        state_dir=orch._config.state_dir,
        audit=AuditConfig(validation_commands=commands, test_selection=mode),
    )
    orch._config = config
    ran = []

    def runner(argv, **kwargs):
        ran.append(tuple(argv))
        return _Completed(list(argv))

    orch._validation_runner = runner
    execution = TaskExecution(
        task_id="sel-1",
        task_branch="autoloop/sel-1",
        worktree_path=str(repo),
        task_base_sha="0" * 40,
        candidate_sha="1" * 40,
    )
    return orch, execution, ran


class _Completed:
    """The subset of `subprocess.CompletedProcess` `run_validation_commands`
    reads."""

    def __init__(self, args):
        self.args = args
        self.returncode = 0
        self.stdout = ""
        self.stderr = ""


def test_the_post_commit_summary_carries_the_selection_evidence(tmp_path, repo):
    orch, execution, ran = postcommit_orchestrator(tmp_path, repo, (RUFF, SUITE))

    ok, summary = orch._run_post_commit_validation(execution, ["pkg/publisher.py"])

    assert ok
    assert "test selection: SUBSET" in summary
    assert "suite/test_smoke.py" in summary
    assert any("suite/test_smoke.py" in argv for argv in ran)
    assert not any("suite" in argv for argv in ran), "the whole tree did not run"


def test_a_full_suite_request_runs_every_configured_command(tmp_path, repo):
    orch, execution, ran = postcommit_orchestrator(
        tmp_path, repo, (RUFF, SUITE), mode=TEST_SELECTION_FULL
    )

    _ok, summary = orch._run_post_commit_validation(execution, ["pkg/publisher.py"])

    assert "test selection: FULL SUITE" in summary
    assert any("suite" in argv for argv in ran)
    assert not any(any("test_smoke" in token for token in argv) for argv in ran)


def test_a_task_declaring_its_own_validation_is_never_narrowed(tmp_path, repo):
    orch, execution, ran = postcommit_orchestrator(tmp_path, repo, (RUFF,))
    execution.validation_commands = (SUITE,)

    _ok, summary = orch._run_post_commit_validation(execution, ["pkg/publisher.py"])

    assert "test selection: FULL SUITE" in summary
    assert "declares its own validation" in summary
    assert not any(any("test_smoke" in token for token in argv) for argv in ran)


def test_a_declared_validation_cwd_is_never_narrowed(tmp_path, repo):
    """Selection resolves repo-relative paths against the repo root; a command
    running from a subdirectory takes paths relative to THAT directory."""
    orch, execution, _ran = postcommit_orchestrator(tmp_path, repo, (RUFF, SUITE))
    execution.validation_cwd = "suite"

    _ok, summary = orch._run_post_commit_validation(execution, ["pkg/publisher.py"])

    assert "test selection: FULL SUITE" in summary
    assert "not the repo" in summary


def test_a_task_declaring_the_configured_default_still_runs_it_in_full(tmp_path, repo):
    """The per-task full-suite lever, exercised as an operator would use it.

    `[audit] test_selection = "full"` is a global switch; declaring the SAME
    commands the config already sets is how one task — the one under review —
    demands the whole suite while every other task keeps narrowing. It has to
    work even though the declared list is byte-identical to the default, which
    is the case a "did the task override anything?" check by value would get
    wrong.
    """
    orch, execution, ran = postcommit_orchestrator(tmp_path, repo, (RUFF, SUITE))
    execution.validation_commands = (RUFF, SUITE)

    _ok, summary = orch._run_post_commit_validation(execution, ["pkg/publisher.py"])

    assert "test selection: FULL SUITE" in summary
    assert "declares its own validation" in summary
    assert any("suite" in argv for argv in ran), "the whole tree ran"
    assert not any(any("test_smoke" in token for token in argv) for argv in ran)


# ---- the pre-commit phase: the round's own authoritative run -----------------


def precommit_round(
    tmp_path,
    *,
    commands=(RUFF, SUITE),
    task=None,
    fails_on=None,
    **kwargs,
):
    """One REAL `ImplementExecutor` round over a REAL git worker repo.

    Returns `(outcome, ran, worker)` — the round's outcome, every argv the
    validation runner was handed in order, and the worker repo the round ran
    against (so a second phase can be pointed at the same tree).

    Real on purpose, at both ends. The claim these tests carry is about the
    executor's own call site, so a fake executor would prove a fake; and
    selection reads an import graph off the filesystem, so a fake tree would
    prove a fake graph. `fails_on`, when given, is a TOKEN — a command whose argv
    contains it exits 1, which is how the failing-run cases pick out exactly the
    narrowed command.

    `advisory_zero_call_returns=0`: since advis-01 (2026-08-26) a round whose
    agent never uses the advisory channel is handed back once and then WITHHELD
    from review, and a withheld round never reaches the validation run these
    tests measure. `FakeAgentRunner` cannot ask, so that contract is pinned off
    here and graded in `test_agent_self_validation.py` §10a.
    """
    main = git_scaffold(tmp_path / "main", "main")
    worker = git_scaffold(tmp_path / "worker", "autoloop/sel-2")
    ran: list[tuple[str, ...]] = []

    def recorder(argv, **kw):
        ran.append(tuple(argv))
        proc = _Completed(list(argv))
        if fails_on is not None and fails_on in tuple(argv):
            proc.returncode = 1
            proc.stdout = "FAILED suite/test_smoke.py::test_publish\n1 failed"
        return proc

    policy = PolicyEngine(PolicyConfig())
    executor = ImplementExecutor(
        git=GitGateway(main, policy),
        # Standalone binding, never reached: `worker_repo_root_for` + `policy`
        # win, exactly as in production.
        agent_runner=FakeAgentRunner(),
        validation_commands=commands,
        command_runner=recorder,
        worker_repo_root_for=lambda task_id: worker,
        policy=policy,
        agent_runner_factory=make_agent_runner_factory(
            write_files={"pkg/publisher.py": "def publish():\n    return 2\n"}
        ),
        advisory_zero_call_returns=0,
        **kwargs,
    )

    outcome = executor.execute(
        implement_directive(task_id="sel-2"),
        task or Task(id="sel-2", title="publisher", description="change the publisher"),
    )
    return outcome, ran, worker


def test_the_pre_commit_run_is_narrowed_too(tmp_path):
    """THE CLAIM (val-04, 2026-08-27), and the test that used to assert its
    opposite.

    `ImplementExecutor` runs the configured commands once BEFORE the commit —
    the authoritative run, the one that sets `validation` and decides the
    round's status. Until this change it executed every configured command in
    full while the cheap post-commit re-run narrowed, which is the wrong way
    round: the expensive run paid full price.

    Three assertions, and the third is the one constraint 4 is about:

    * the reachable test really runs, so the change is still exercised;
    * the whole tree does NOT, so the round is actually cheaper;
    * the round's own `validation` string SAYS it narrowed and what to. A
      narrowed run that cannot say so is the evidence gap that gets a packet
      refused.
    """
    outcome, ran, _worker = precommit_round(tmp_path)

    assert outcome.status == "ok"
    assert outcome.changed_paths == ("pkg/publisher.py",)
    assert any("suite/test_smoke.py" in argv for argv in ran), "the reachable test ran"
    assert any("suite/test_orchestra.py" in argv for argv in ran), "transitively too"
    assert not any("suite" in argv for argv in ran), "the whole tree did not run"
    assert not any("suite/test_unrelated.py" in argv for argv in ran)
    assert any(argv[0] == "ruff" for argv in ran), "the non-test command is untouched"

    assert "test selection: SUBSET" in outcome.validation
    assert "suite/test_smoke.py" in outcome.validation


def test_the_pre_commit_evidence_no_longer_claims_a_full_run(tmp_path):
    """The sentence that had to change in the same commit.

    `PRECOMMIT_EVIDENCE` told the reviewer the recorded subset was ADDED to a
    full-suite run of the same tree. Narrowing this call site makes that false:
    a full-suite run is no longer guaranteed at either phase, and a round that
    narrowed at both has none at all. A reviewer reading the old guarantee would
    be reading one that no longer holds.
    """
    outcome, _ran, _worker = precommit_round(tmp_path)

    # Both directions on purpose. The negative alone would still pass if the old
    # sentence were reintroduced ALONGSIDE the new one in a different casing, and
    # the positive alone would still pass if it were reintroduced beside it.
    assert "ADDED to a full-suite run" not in outcome.validation
    assert "NOT a subset added to a full-suite run" in outcome.validation
    assert "BOTH runs of this round" in outcome.validation
    assert "no full-suite run is guaranteed at either phase" in outcome.validation


def test_a_task_declaring_its_own_validation_is_never_narrowed_pre_commit(tmp_path):
    """The first refusal rule, and the per-task way to demand a full run.

    Also pins constraint 3 from the other side: the declared list is what runs,
    so the configured `ruff` never launches. Both phases resolve
    `tuple(task.validation) or <configured>` and selection is applied strictly
    downstream of that one resolution.
    """
    task = Task(
        id="sel-2",
        title="publisher",
        description="change the publisher",
        validation=(SUITE,),
    )
    outcome, ran, _worker = precommit_round(tmp_path, commands=(RUFF,), task=task)

    assert "test selection: FULL SUITE" in outcome.validation
    assert "declares its own validation" in outcome.validation
    assert any("suite" in argv for argv in ran), "the whole declared tree ran"
    assert not any(any("test_smoke" in token for token in argv) for argv in ran)
    assert not any(argv[0] == "ruff" for argv in ran), "the configured list lost"


def test_a_record_with_no_validation_tuple_refuses_to_raise(tmp_path):
    """`Task.validation = None` — the malformed record `_advisory_for` already
    guards against — must not reach the selector as a crash.

    It is read through `or ()`, so it means "declared nothing" and the round
    narrows normally. Asserted on `_select_validation` directly because the
    round would die earlier, in `_validation_commands_for`'s own unguarded
    `tuple(task.validation)`; that is pre-existing behaviour this change
    deliberately does not paper over, and this test says which of the two reads
    is guarded.
    """
    worker = git_scaffold(tmp_path / "worker", "autoloop/sel-3")
    policy = PolicyEngine(PolicyConfig())
    executor = ImplementExecutor(
        git=GitGateway(worker, policy),
        agent_runner=FakeAgentRunner(),
        validation_commands=(RUFF, SUITE),
    )
    task = Task(id="sel-3", title="t", description="d")
    task.validation = None

    chosen = executor._select_validation(task, (RUFF, SUITE), ["pkg/publisher.py"], worker)

    assert not chosen.widened
    assert "suite/test_smoke.py" in chosen.selected


def test_a_declared_validation_cwd_is_never_narrowed_pre_commit(tmp_path):
    """The second refusal rule. Selection resolves repo-relative paths against
    the repo root; a command running from a subdirectory takes its paths
    relative to THAT directory, and the two would not line up."""
    task = Task(
        id="sel-2",
        title="publisher",
        description="change the publisher",
        validation_cwd="suite",
    )
    outcome, ran, _worker = precommit_round(tmp_path, task=task)

    assert "test selection: FULL SUITE" in outcome.validation
    assert "not the repo" in outcome.validation
    assert any("suite" in argv for argv in ran), "the command ran as configured"


def test_the_operator_full_switch_is_honoured_at_the_pre_commit_phase(tmp_path):
    """`[audit] test_selection = "full"` is what the evidence tells a reviewer
    restores the pre-2026-08-20 behaviour. That is only true if BOTH phases read
    it, which is why `cli._build_executor` threads it here."""
    outcome, ran, _worker = precommit_round(tmp_path, test_selection=TEST_SELECTION_FULL)

    assert "test selection: FULL SUITE" in outcome.validation
    assert 'test_selection = "full"' in outcome.validation
    assert any("suite" in argv for argv in ran)
    assert not any(any("test_smoke" in token for token in argv) for argv in ran)


def test_a_failing_selected_command_still_fails_the_round_and_says_what_it_ran(tmp_path):
    """Constraint 4, on the path that matters most: the run stays authoritative.

    A narrowed run that fails throws the round away exactly as a full one did,
    and the summary a reviewer reads carries BOTH the failure and the selection
    — otherwise a red narrowed run looks like a red full run and nobody can tell
    what was actually executed.
    """
    outcome, ran, _worker = precommit_round(tmp_path, fails_on="suite/test_smoke.py")

    assert outcome.status == "error"
    assert "validation failed after implementation" in outcome.summary
    assert "FAIL" in outcome.validation
    assert "test selection: SUBSET" in outcome.validation
    assert any("suite/test_smoke.py" in argv for argv in ran)


def test_both_phases_run_the_same_commands_for_the_same_change(tmp_path):
    """The two ends agree BY CONSTRUCTION, and this is what that buys.

    Same command list (`tuple(task.validation) or <configured>` at both ends),
    same selector, same tree, same changed paths — so the argv the executor ran
    before the commit is the argv the orchestrator re-runs after it. A drift
    between the two would mean the reviewed commit was graded by a different
    suite from the one that produced it.
    """
    outcome, pre_ran, worker = precommit_round(tmp_path)
    orch, execution, post_ran = postcommit_orchestrator(tmp_path, worker, (RUFF, SUITE))

    ok, summary = orch._run_post_commit_validation(execution, outcome.changed_paths)

    assert ok
    assert pre_ran == post_ran, "the two phases executed different commands"
    assert "test selection: SUBSET" in summary


def test_the_operator_setting_is_wired_into_the_production_executor():
    """Structural, and it carries what no behavioural test here can: production
    gets this because `cli._build_executor` — the ONE place a write-capable
    executor is constructed from configuration — passes the operator's setting,
    and because the constructor's own default is production's default rather
    than a third "unwired" state the selector does not model."""
    source = (Path(__file__).resolve().parents[1] / "cli.py").read_text(encoding="utf-8")
    build = source.split("def _build_executor(")[1].split("\ndef ")[0]

    assert "test_selection=config.audit.test_selection" in build
    assert (
        signature(ImplementExecutor).parameters["test_selection"].default
        == TEST_SELECTION_REACHABLE
    )


def test_the_evidence_names_both_narrowed_phases_and_how_to_widen(repo):
    """What stops a narrowed packet reading as LESS evidence than the round
    before it. It no longer claims a full run happened alongside — it says
    outright that none is guaranteed at either phase — and it still names two
    ways to demand one."""
    evidence = selection(repo, ["pkg/publisher.py"]).evidence()

    assert "PRE-COMMIT" in evidence
    assert "NOT a subset added to a full-suite run" in evidence
    assert "no full-suite run is guaranteed at either phase" in evidence
    assert 'test_selection = "full"' in evidence, "the global lever"
    assert "task-add --validation" in evidence, "the per-task lever"
