"""`[repo]` — what the loop cannot infer about the TARGET repository, which
used to be constants naming this repository by name.

Two claims, and the second is why this file is longer than the section:

1. **Every default equals the constant it replaced.** "Behaviour here is
   unchanged" is the entire premise under which a hardcoded value was allowed
   to become configuration, so the first test pins it. Everything after it
   proves the configured value actually reaches its consumer — a setting that
   loads and is then ignored is worse than a constant, since it reads as
   configured while behaving as before.
2. **The section holds no authorization surface.** The always-approved tracker
   list was configurable for one unshipped round and was withdrawn in review
   (`docs/SECURITY.md` S31): `.autoloop/config.toml` is gitignored, so a
   `[repo].tracker_paths` edit could widen every scoped task's write scope
   with no diff anyone reads, and the "documentation only" suffix blocklist
   that was supposed to bound it does not — `.env`, `Makefile`, `Dockerfile`
   and any extensionless script carry no refused suffix. Section 2 below is
   what stops that design coming back by accident.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from autoloop import dashboard, tasks
from autoloop.config import (
    DEFAULT_AUDIT_REPORT_GLOB,
    DEFAULT_ENV_EXAMPLE_DB_KEY,
    DEFAULT_ENV_EXAMPLE_FILE,
    RETIRED_TRACKER_PATHS_KEY,
    RepoConfig,
    load_config,
)
from autoloop.errors import ConfigError
from autoloop.tasks import TRACKER_PATHS, effective_approved_paths
from autoloop.validation_env import load_validation_env, repo_declared_db_name

CONFIG_HEAD = '[browser]\nconversation_url = "https://chatgpt.com/c/x"\n\n'


def write_config(tmp_path: Path, repo_section: str = "") -> Path:
    """A minimal loadable config, plus whatever `[repo]` body is under test."""
    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG_HEAD
        + f'[paths]\nworkers_root = "{tmp_path / "w"}"\n\n'
        + repo_section,
        encoding="utf-8",
    )
    return path


def _accessor_for(config):
    """`Orchestrator._tracker_paths` bound to a loop carrying `config`.

    Built with `__new__` because the accessor reads no collaborator — which is
    itself the property under test: the list must not be a function of anything
    an operator can edit at runtime. Going through the real accessor (rather
    than asserting on `TRACKER_PATHS` directly) is what makes these tests fail
    if a future edit wires it back to `self._config`.
    """
    from autoloop.orchestrator import Orchestrator

    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator._config = config
    return orchestrator._tracker_paths


# ---- 1. the defaults ARE the constants they replaced -------------------------


def test_defaults_are_exactly_the_previously_hardcoded_constants():
    """The premise of the whole change. If any of these drifts, a repository
    that never opted in silently gets different behaviour — which is the one
    outcome turning constants into configuration was not allowed to have."""
    defaults = RepoConfig()

    assert defaults.env_example_file == ".env.example"
    assert defaults.env_example_db_key == "DB_NAME"
    assert defaults.audit_report_glob == "docs/AUDIT_*.md"


def test_the_duplicated_default_spellings_agree():
    """`validation_env` and `dashboard` each repeat a default rather than
    importing it from `config` — `config` imports `tasks`, and the dashboard
    must render against a checkout `load_config` would refuse, so neither can
    take the back-import. Duplication is only safe while it is pinned."""
    from autoloop import validation_env

    assert validation_env.DEFAULT_ENV_EXAMPLE_FILE == DEFAULT_ENV_EXAMPLE_FILE
    assert validation_env.DEFAULT_ENV_EXAMPLE_DB_KEY == DEFAULT_ENV_EXAMPLE_DB_KEY
    assert dashboard.DEFAULT_AUDIT_REPORT_GLOB == DEFAULT_AUDIT_REPORT_GLOB


def test_a_config_with_no_repo_section_loads_the_defaults(tmp_path):
    config = load_config(write_config(tmp_path))

    assert config.repo == RepoConfig()


# ---- 2. the tracker list is NOT in this section, and cannot get back in ------
#
# The withdrawn design (`docs/SECURITY.md` S31) and why these tests exist: a
# tracker is granted to EVERY scoped task without appearing in its
# `approved_paths`, so the list is authorization surface. Sourcing it from
# `.autoloop/config.toml` — gitignored, under the state directory — means an
# edit nobody reviews can widen every task at once. The bound offered for that
# was a filename-suffix blocklist, which cannot hold: the set of
# behaviour-changing filenames is open-ended.


#: Files that change behaviour and carry NO suffix the withdrawn blocklist
#: refused. Each one would have been accepted as "documentation" and handed to
#: every scoped task. This list is the argument against the heuristic, kept as
#: test data so it cannot be forgotten and reintroduced.
BEHAVIOUR_CHANGING_FILES = [
    ".env",                    # credentials the whole validation boundary exists to contain
    "Makefile",                # runs arbitrary commands
    "Dockerfile",              # defines the runtime image
    "Gemfile",                 # dependency set
    ".gitignore",              # decides what the escape detector and reviewers see
    "scripts/deploy",          # an extensionless executable
]


def test_tracker_paths_is_not_a_repo_config_field():
    """The structural pin. Every behavioural test below could be satisfied by a
    field that merely happens to be unread today; this one fails the moment the
    key is reintroduced, which is the event the review objected to."""
    assert RETIRED_TRACKER_PATHS_KEY not in {
        f.name for f in dataclasses.fields(RepoConfig)
    }
    assert not hasattr(RepoConfig(), "tracker_paths")


def test_the_suffix_blocklist_and_its_validator_are_gone():
    """Deleted rather than left caller-less. A blocklist still sitting in
    `tasks.py` keeps the disproven claim ("refusing these extensions makes the
    grant documentation-only") available for the next caller to trust."""
    assert not hasattr(tasks, "validate_tracker_paths")
    assert not hasattr(tasks, "_NON_TRACKER_SUFFIXES")


@pytest.mark.parametrize("dangerous", BEHAVIOUR_CHANGING_FILES)
def test_a_config_edit_cannot_newly_authorize_a_behaviour_changing_file(
    tmp_path, dangerous
):
    """THE regression the review asked for, end to end.

    A config naming these still LOADS — refusing would make every command fail
    on a deployment whose config predates this decision — but the value is
    discarded, so no scoped task gains write access to `.env` or `Makefile`
    without the reviewed diff that naming it in `approved_paths` requires.
    """
    config = load_config(
        write_config(tmp_path, f'[repo]\ntracker_paths = ["{dangerous}"]\n')
    )

    assert not hasattr(config.repo, "tracker_paths")
    granted = effective_approved_paths(("src/thing.py",), _accessor_for(config)())
    assert dangerous not in granted
    assert set(granted) == {"src/thing.py"} | set(TRACKER_PATHS)


def test_the_dropped_key_is_reported_rather_than_silently_ignored(tmp_path):
    """A setting that loads and does nothing reads as configured while behaving
    otherwise — the exact failure `_check_keys` exists to prevent. The operator
    is told the value was dropped and where the real list lives, so they learn
    it at startup rather than from a task parking on an unauthorized path."""
    config = load_config(
        write_config(tmp_path, '[repo]\ntracker_paths = ["HANDBOOK.md"]\n')
    )

    notice = "\n".join(config.migration_notices)
    assert "repo.tracker_paths" in notice
    assert "DROPPED" in notice
    assert "HANDBOOK.md" in notice, "the discarded value is quoted back"
    assert "autoloop/tasks.py" in notice, "and the real source of the list is named"


def test_an_unscoped_task_gains_nothing_whatever_the_trackers_are():
    """docs/SECURITY.md finding #2 (circular ownership): an empty
    `approved_paths` means "no scope authorized yet" and must keep refusing
    dispatch. That is a property of the TASK, so no tracker list can change
    it — including a long one."""
    assert effective_approved_paths((), ("a.md", "b.md", "c.md")) == ()


def test_no_trackers_at_all_is_legal_and_grants_nothing_extra():
    """The `trackers` parameter still means what it says, which is what lets a
    ported copy of this package declare a different list (or none) by editing
    `TRACKER_PATHS` — a reviewed commit in that repository's own history."""
    assert effective_approved_paths(("src/thing.py",), ()) == ("src/thing.py",)


def test_the_default_argument_is_this_repositorys_own_list():
    """Callers that pass no `trackers` get the constant, so the parameter can
    never quietly mean something else for a subset of call sites."""
    assert effective_approved_paths(("src/thing.py",)) == effective_approved_paths(
        ("src/thing.py",), TRACKER_PATHS
    )


# ---- 3. the production-database marker ---------------------------------------


def make_repo_root(tmp_path: Path, filename: str, body: str) -> Path:
    repo = tmp_path / "checkout"
    (repo / ".autoloop").mkdir(parents=True)
    (repo / filename).write_text(body, encoding="utf-8")
    return repo


def test_the_declared_db_name_is_read_from_the_configured_file_and_key(tmp_path):
    repo = make_repo_root(tmp_path, "env.sample", "POSTGRES_DB=shop_production\n")

    assert repo_declared_db_name(repo, "env.sample", "POSTGRES_DB") == "shop_production"
    # And the defaults do NOT see it — the marker is genuinely configured, not
    # guessed at by scanning for anything that looks like a database name.
    assert repo_declared_db_name(repo) == ""


def test_the_marker_says_where_to_look_never_what_to_refuse(tmp_path):
    """The value is still read out of the repository. A config cannot forbid a
    name the repo does not actually declare, which is what keeps this a marker
    rather than a denylist."""
    repo = make_repo_root(tmp_path, "env.sample", "# nothing declared here\n")

    assert repo_declared_db_name(repo, "env.sample", "POSTGRES_DB") == ""


def test_an_empty_env_example_file_disables_the_lookup(tmp_path):
    repo = make_repo_root(tmp_path, ".env.example", "DB_NAME=german_vocabulary\n")

    assert repo_declared_db_name(repo, "") == ""


def test_load_validation_env_refuses_the_configured_repos_database(tmp_path):
    """End to end through the consumer: a validation env file pointed at the
    application database of a repo that spells its declaration differently is
    still refused."""
    repo = make_repo_root(tmp_path, "env.sample", "POSTGRES_DB=shop_production\n")
    env_file = tmp_path / "validation.env"
    env_file.write_text(
        "DB_HOST=127.0.0.1\nDB_PORT=5432\nDB_NAME=shop_production\n"
        "DB_USER=validation_user\nDB_PASSWORD=super-secret-password\n"
        "SECRET_KEY=jwt-signing-key-for-tests\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    with pytest.raises(ConfigError) as exc:
        load_validation_env(
            env_file,
            repo_root=repo,
            state_dir=repo / ".autoloop",
            env_example_file="env.sample",
            env_example_db_key="POSTGRES_DB",
        )
    assert "env.sample" in str(exc.value)
    # The same file against the DEFAULT marker is fine — proving the refusal
    # came from the configured lookup and not from something else.
    loaded = load_validation_env(env_file, repo_root=repo, state_dir=repo / ".autoloop")
    assert loaded.keys() == ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD",
                             "SECRET_KEY")


def test_an_absolute_or_traversing_env_example_file_is_refused(tmp_path):
    for bad in ("/etc/passwd", "../elsewhere/.env"):
        with pytest.raises(ConfigError):
            load_config(write_config(tmp_path, f'[repo]\nenv_example_file = "{bad}"\n'))


#: Values that LOOK like the documented empty opt-out, or like a valid path, and
#: are neither. Whitespace-only is the dangerous half — see the test below — and
#: padding is refused with it so one rule covers both keys.
BLANK_OR_PADDED = ["   ", "\t", " ", " .env.example", ".env.example ", "\t.env.example"]


@pytest.mark.parametrize("key", ["env_example_file", "audit_report_glob"])
@pytest.mark.parametrize("padded", BLANK_OR_PADDED)
def test_a_whitespace_only_or_padded_path_is_refused_for_both_keys(tmp_path, key, padded):
    """Only the EXACT empty string is the opt-out. Anything that merely strips
    to empty is a typo, and a typo must not silently become a second, undeclared
    way to switch a setting off — which is the failure the next test spells
    out."""
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path, f'[repo]\n{key} = "{padded}"\n'))

    assert f"repo.{key}" in str(exc.value)


@pytest.mark.parametrize("key", ["env_example_file", "audit_report_glob"])
def test_the_exact_empty_string_is_still_the_documented_opt_out(tmp_path, key):
    """The other half of the rule, pinned so tightening padding cannot later be
    widened into removing the opt-out itself. `config.example.toml` documents
    `env_example_file = ""` for a repo that declares no application database,
    and an empty `audit_report_glob` for one that files no audit reports."""
    config = load_config(write_config(tmp_path, f'[repo]\n{key} = ""\n'))

    assert getattr(config.repo, key) == ""


def test_a_whitespace_env_example_file_cannot_disable_the_database_guard(tmp_path):
    """The consequence, end to end, and the reason padding is refused rather
    than stripped.

    `repo_declared_db_name` reads a blank marker as "this repository declares no
    application database" and returns `""` — after which `load_validation_env`
    has nothing to refuse and validation may point at the real database. That is
    correct for the documented `""` opt-out. It must be UNREACHABLE by accident,
    so the loader refuses the blank-looking spellings that would reach it.
    """
    repo = make_repo_root(tmp_path, ".env.example", "DB_NAME=german_vocabulary\n")
    env_file = tmp_path / "validation.env"
    env_file.write_text(
        "DB_HOST=127.0.0.1\nDB_PORT=5432\nDB_NAME=german_vocabulary\n"
        "DB_USER=validation_user\nDB_PASSWORD=super-secret-password\n"
        "SECRET_KEY=jwt-signing-key-for-tests\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    # 1. The guard is real: with the marker as configured, the application
    #    database is refused.
    with pytest.raises(ConfigError) as exc:
        load_validation_env(env_file, repo_root=repo, state_dir=repo / ".autoloop")
    assert "application" in str(exc.value)

    # 2. A blank marker would turn it OFF — the same file, pointed at the same
    #    application database, now loads without complaint. (`ValidationEnv`
    #    exposes no values by design, so the assertion is that it loaded at
    #    all: reaching this line is the whole finding.)
    assert repo_declared_db_name(repo, "   ") == ""
    loaded = load_validation_env(
        env_file,
        repo_root=repo,
        state_dir=repo / ".autoloop",
        env_example_file="   ",
    )
    assert "DB_NAME" in loaded.keys()

    # 3. Which is why no config can put the loop in that state: the only way to
    #    reach step 2 is the exact empty string, written on purpose.
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, '[repo]\nenv_example_file = "   "\n'))


def test_a_malformed_db_key_is_refused_rather_than_silently_never_matching(tmp_path):
    """A key with an '=' or a space in it could never match a parsed line, so
    the refusal it would produce is 'no marker declared' — the silent wrong
    answer this section exists to avoid."""
    for bad in ("DB NAME", "DB_NAME=x", ""):
        with pytest.raises(ConfigError):
            load_config(write_config(tmp_path, f'[repo]\nenv_example_db_key = "{bad}"\n'))


# ---- 4. the dashboard's audit-report location --------------------------------


def make_dashboard_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".autoloop").mkdir(parents=True)
    return repo


REPORT = "#### backend:api-01 — Admin-gate the endpoints\n\nseverity **high**\n"


def test_app_tasks_reads_the_configured_report_location(tmp_path):
    repo = make_dashboard_repo(tmp_path)
    (repo / "reports").mkdir()
    (repo / "reports" / "review-2026-08-16.md").write_text(REPORT, encoding="utf-8")

    assert dashboard.app_tasks(repo) == [], "the default location holds nothing"
    found = dashboard.app_tasks(repo, report_glob="reports/review-*.md")
    assert [t["id"] for t in found] == ["backend:api-01"]
    assert found[0]["source"] == "review-2026-08-16.md"


def test_collect_uses_the_configured_report_glob(tmp_path):
    """The wiring, not just the parameter: `collect` must read `[repo]` from
    the loop's own config, or the setting is inert on the page that uses it."""
    repo = make_dashboard_repo(tmp_path)
    (repo / "reports").mkdir()
    (repo / "reports" / "review-2026-08-16.md").write_text(REPORT, encoding="utf-8")
    (repo / ".autoloop" / "config.toml").write_text(
        CONFIG_HEAD + '[repo]\naudit_report_glob = "reports/review-*.md"\n',
        encoding="utf-8",
    )

    assert [t["id"] for t in dashboard.collect(repo)["app_tasks"]] == ["backend:api-01"]


def test_an_unsafe_or_empty_report_glob_yields_nothing_rather_than_raising(tmp_path):
    """`Path.glob` raises on an absolute pattern, which would 500 a page whose
    entire contract is to keep rendering. And a tracker pointed at one checkout
    has no business reading files outside it."""
    repo = make_dashboard_repo(tmp_path)

    for bad in ("/etc/*.md", "../*.md", ""):
        assert dashboard.app_tasks(repo, report_glob=bad) == []


def test_a_checkout_with_no_config_still_renders_the_default_location(tmp_path):
    """The dashboard reads the raw TOML precisely so it survives a checkout
    `load_config` would refuse. An unconfigured one must keep working."""
    repo = make_dashboard_repo(tmp_path)
    (repo / "docs").mkdir()
    (repo / "docs" / "AUDIT_2026-08-16.md").write_text(REPORT, encoding="utf-8")

    assert dashboard._audit_report_glob(repo) == DEFAULT_AUDIT_REPORT_GLOB
    assert [t["id"] for t in dashboard.collect(repo)["app_tasks"]] == ["backend:api-01"]


def test_an_unparseable_config_falls_back_instead_of_taking_the_page_down(tmp_path):
    repo = make_dashboard_repo(tmp_path)
    config = repo / ".autoloop" / "config.toml"
    config.write_text("this is not toml {{", encoding="utf-8")

    assert dashboard._audit_report_glob(repo) == DEFAULT_AUDIT_REPORT_GLOB
    assert dashboard._config_toml(repo) == {}

    # A key holding a SCALAR where a table belongs is the shape that raises
    # `AttributeError` on the next `.get` — a 500 rather than a fallback, which
    # is the one thing reading the raw TOML was supposed to rule out.
    config.write_text('repo = "docs"\npaths = 7\n', encoding="utf-8")
    assert dashboard._config_section(repo, "repo") == {}
    assert dashboard._audit_report_glob(repo) == DEFAULT_AUDIT_REPORT_GLOB
    assert dashboard._inbox_dir(repo) == Path.home() / ".autoloop" / "inbox"


# ---- 5. the orchestrator's tracker list comes from the reviewed source -------


def orchestrator_source() -> str:
    return (Path(__file__).resolve().parents[1] / "orchestrator.py").read_text(
        encoding="utf-8"
    )


def test_every_orchestrator_call_site_reads_the_one_accessor():
    """The claim the behavioural tests CANNOT make on their own.

    Asserting what `_tracker_paths()` returns proves the accessor is right, not
    that anything calls it — reverting a single
    `effective_approved_paths(task.approved_paths, self._tracker_paths())` to
    some other list leaves every behavioural test green while that site
    authorizes something different. And a site disagreeing with its neighbour
    is worse than one that lags: the dispatch seed and the every-dispatch
    re-sync compare and assign the same value, so two lists rewrite the
    execution record forever.

    A source scan, in the same spirit as `test_dashboard.py`'s `PAGE`
    interpolation checks and the verification greps in `docs/SECURITY.md` S31 —
    the property is "no call site was missed", which is about the file, not
    about one call's return value.
    """
    # The `from .tasks import` line names the function without a '(', so it is
    # not picked up here; every remaining occurrence is a call.
    calls = [
        line.strip()
        for line in orchestrator_source().splitlines()
        if "effective_approved_paths(" in line and not line.lstrip().startswith("#")
    ]
    assert len(calls) == 3, (
        "expected exactly three call sites (dispatch seed, every-dispatch "
        f"re-sync, post-commit ownership check), found {len(calls)}: {calls}"
    )
    for line in calls:
        assert "self._tracker_paths()" in line, (
            f"call site does not read the one accessor: {line!r}"
        )


def test_the_accessor_returns_the_reviewed_constant_and_reads_no_config(tmp_path):
    """Where the per-repository tracker declaration comes from, stated as a
    test: `tasks.TRACKER_PATHS`, which lives in git-tracked source, so changing
    it for a target repository is a commit in that repository's history.

    Both halves matter. The value pin alone would still pass if the accessor
    read a config field that happened to default to the constant — which is
    precisely the withdrawn design — so the source of the accessor's body is
    scanned for a config read as well.
    """
    from autoloop.config import AutoloopConfig, BrowserConfig
    from autoloop.policy import PolicyConfig

    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url="https://chatgpt.com/c/x"),
        policy=PolicyConfig(),
        state_dir=tmp_path / ".autoloop",
        workers_root=tmp_path / "w",
        repo=RepoConfig(env_example_file="env.sample"),
    )

    assert _accessor_for(config)() == TRACKER_PATHS

    body = orchestrator_source().split("def _tracker_paths(")[1].split("\n    def ")[0]
    statement = [line.strip() for line in body.splitlines() if line.strip()][-1]
    assert statement == "return TRACKER_PATHS", (
        f"_tracker_paths must return the reviewed constant, found {statement!r} — "
        "a config read here reopens docs/SECURITY.md S31"
    )


# ---- 6. the section is still strict ------------------------------------------


def test_an_unknown_repo_key_is_refused_not_ignored(tmp_path):
    """The whole config loader's rule (`_check_keys`): a typo'd setting must
    never silently fall back to a default. `tracker_paths` is the one key
    handled ahead of this check rather than by it — consumed, reported and
    discarded (see section 2) — and a near miss like `tracker_path` must still
    be refused outright rather than reading as that key."""
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path, '[repo]\ntracker_path = ["a.md"]\n'))

    assert "repo" in str(exc.value)


def test_a_non_table_repo_value_is_refused_by_the_loader_itself(tmp_path):
    """`repo = "x"` must produce this loader's own `ConfigError`, not `dict()`'s
    raw "dictionary update sequence element" complaint — strict config means a
    malformed section is reported as one, naming the section and the shape it
    should have had.

    The scalar is written BEFORE any table header on purpose: TOML binds a bare
    key to the section above it, so `repo = "x"` appended after `[paths]` would
    be `paths.repo` and would be refused by that section's key check instead —
    a green test that never reaches the guard under test.
    """
    path = tmp_path / "config.toml"
    path.write_text(
        'repo = "x"\n\n' + CONFIG_HEAD + f'[paths]\nworkers_root = "{tmp_path / "w"}"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as exc:
        load_config(path)

    message = str(exc.value)
    assert "repo" in message and "table" in message


def test_json_serialisable_defaults_survive_a_round_trip():
    """`RepoConfig` values reach `state.json` / the review packet through
    `asdict`-shaped payloads; a value that is not JSON-safe would fail there
    rather than here."""
    payload = json.dumps(dataclasses.asdict(RepoConfig()))

    assert json.loads(payload)["audit_report_glob"] == DEFAULT_AUDIT_REPORT_GLOB
    assert json.loads(payload)["env_example_file"] == DEFAULT_ENV_EXAMPLE_FILE
