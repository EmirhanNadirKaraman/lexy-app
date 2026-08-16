"""`[repo]` — the three things about the TARGET repository the loop cannot
infer, which used to be constants naming this repository by name.

The load-bearing test in this file is the FIRST one: every default must equal
the constant that was hardcoded, because "behaviour here is unchanged" is the
entire premise under which the hardcoding was allowed to become configuration.
Everything after it proves the configured value actually reaches the consumer —
a setting that loads and is then ignored is worse than a constant, since it
reads as configured while behaving as before.

The one that needed the most care is `tracker_paths`: it grants every scoped
task write access to files it never names, so the tests below spend most of
their length on what a config edit may NOT buy.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoloop import dashboard
from autoloop.config import (
    DEFAULT_AUDIT_REPORT_GLOB,
    DEFAULT_ENV_EXAMPLE_DB_KEY,
    DEFAULT_ENV_EXAMPLE_FILE,
    RepoConfig,
    load_config,
)
from autoloop.errors import ConfigError, TaskGraphError
from autoloop.tasks import (
    TRACKER_PATHS,
    effective_approved_paths,
    validate_tracker_paths,
)
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


# ---- 1. the defaults ARE the constants they replaced -------------------------


def test_defaults_are_exactly_the_previously_hardcoded_constants():
    """The premise of the whole change. If any of these drifts, a repository
    that never opted in silently gets different behaviour — which is the one
    outcome turning constants into configuration was not allowed to have."""
    defaults = RepoConfig()

    assert defaults.tracker_paths == TRACKER_PATHS
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


# ---- 2. tracker_paths --------------------------------------------------------


def test_configured_trackers_replace_this_repos_own(tmp_path):
    config = load_config(
        write_config(tmp_path, '[repo]\ntracker_paths = ["HANDBOOK.md", "doc/notes.rst"]\n')
    )

    assert config.repo.tracker_paths == ("HANDBOOK.md", "doc/notes.rst")
    effective = effective_approved_paths(("src/thing.py",), config.repo.tracker_paths)
    assert set(effective) == {"src/thing.py", "HANDBOOK.md", "doc/notes.rst"}
    # REPLACED, not merged: this repository's own trackers mean nothing to
    # another repository, and carrying them along would authorize paths the
    # operator never declared.
    assert "docs/SUMMARY.md" not in effective


def test_an_unscoped_task_gains_nothing_however_the_trackers_are_configured():
    """docs/SECURITY.md finding #2 (circular ownership): an empty
    `approved_paths` means "no scope authorized yet" and must keep refusing
    dispatch. That is a property of the TASK, so no tracker list can change
    it — including a long one."""
    assert effective_approved_paths((), ("a.md", "b.md", "c.md")) == ()


def test_configuring_no_trackers_at_all_is_legal_and_grants_nothing_extra():
    """An honest reading for a repository that imposes no doc obligations: the
    task gets exactly what it declared. It must not fall back to this
    repository's list, which would be the hardcoding all over again."""
    assert effective_approved_paths(("src/thing.py",), ()) == ("src/thing.py",)


def test_the_default_argument_preserves_every_pre_existing_caller():
    """Callers written before the parameter existed keep their exact meaning —
    this is what lets the change be behaviour-preserving without editing every
    test that pins tracker behaviour."""
    assert effective_approved_paths(("src/thing.py",)) == effective_approved_paths(
        ("src/thing.py",), TRACKER_PATHS
    )


@pytest.mark.parametrize(
    "bad",
    [
        "docs/*.md",             # a glob would grant an open-ended set
        "../outside.md",         # traversal
        "/etc/notes.md",         # absolute
        "~/notes.md",            # home-relative
        "docs/",                 # a directory prefix grants a whole tree
        "src/thing.py",          # code
        "pyproject.toml",        # configuration
        "ci/deploy.sh",          # executable
        "app/state.JSON",        # extension check is case-insensitive
    ],
)
def test_a_tracker_that_is_not_an_exact_document_is_refused(bad):
    """The replacement for the bound that was given up. `tracker_paths` widens
    EVERY scoped task at once, and the config file is gitignored — so unlike
    the constant it replaced, an edit here is not a reviewed diff. These
    refusals are what stands in its place."""
    with pytest.raises(TaskGraphError):
        validate_tracker_paths([bad])


def test_a_bad_tracker_refuses_the_whole_config_by_name(tmp_path):
    """At load time, not at the first dispatch that tried to use it: a loop
    that starts and then refuses every task is much harder to diagnose than one
    that will not start and says which key is wrong."""
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path, '[repo]\ntracker_paths = ["src/thing.py"]\n'))

    assert "repo.tracker_paths" in str(exc.value)


def test_a_bare_string_tracker_list_is_refused(tmp_path):
    """The per-character split `_validate_superseded_by` documents: iterating
    `"a.md"` yields five one-character "trackers", each of which would then be
    granted to every task."""
    with pytest.raises(TaskGraphError):
        validate_tracker_paths("a.md")
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, '[repo]\ntracker_paths = "a.md"\n'))


def test_a_duplicate_tracker_is_refused():
    with pytest.raises(TaskGraphError):
        validate_tracker_paths(["a.md", "a.md"])


def test_this_repos_own_trackers_pass_their_own_validator():
    """The default must survive the check applied to a configured value —
    otherwise the loop would refuse a config that merely spells out what it
    already does."""
    assert validate_tracker_paths(list(TRACKER_PATHS)) == TRACKER_PATHS


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


# ---- 5. the orchestrator reads the configured trackers -----------------------


def test_every_orchestrator_call_site_passes_the_configured_trackers():
    """The claim the test below CANNOT make on its own.

    Asserting that `_tracker_paths()` returns the config value proves the
    accessor works, not that anything calls it — reverting a single
    `effective_approved_paths(task.approved_paths, self._tracker_paths())` to
    its one-argument form leaves every behavioural test green while that site
    silently falls back to this repository's constant. And a site that
    disagreed with its neighbour would be worse than one that lagged: the
    dispatch seed and the every-dispatch re-sync compare and assign the same
    value, so two different lists rewrite the execution record forever.

    A source scan, in the same spirit as `test_dashboard.py`'s `PAGE`
    interpolation checks and the verification greps in `docs/SECURITY.md` S31 —
    the property is "no call site was missed", which is about the file, not
    about one call's return value.
    """
    source = (Path(__file__).resolve().parents[1] / "orchestrator.py").read_text(
        encoding="utf-8"
    )
    # The `from .tasks import` line names the function without a '(', so it is
    # not picked up here; every remaining occurrence is a call.
    calls = [
        line.strip()
        for line in source.splitlines()
        if "effective_approved_paths(" in line and not line.lstrip().startswith("#")
    ]
    assert len(calls) == 3, (
        "expected exactly three call sites (dispatch seed, every-dispatch "
        f"re-sync, post-commit ownership check), found {len(calls)}: {calls}"
    )
    for line in calls:
        assert "self._tracker_paths()" in line, (
            f"call site does not pass the configured trackers: {line!r}"
        )


def test_the_orchestrator_seeds_and_resyncs_from_the_configured_trackers(tmp_path):
    """Both `effective_approved_paths` call sites in `_dispatch_task_postcommit`
    go through `_tracker_paths`. If the seed and the re-sync ever read different
    lists, the execution record reads dirty on every dispatch and is rewritten
    forever — so the accessor, not the constant, is what they must share."""
    from autoloop.config import AutoloopConfig, BrowserConfig
    from autoloop.orchestrator import Orchestrator
    from autoloop.policy import PolicyConfig

    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url="https://chatgpt.com/c/x"),
        policy=PolicyConfig(),
        state_dir=tmp_path / ".autoloop",
        workers_root=tmp_path / "w",
        repo=RepoConfig(tracker_paths=("HANDBOOK.md",)),
    )
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator._config = config

    assert orchestrator._tracker_paths() == ("HANDBOOK.md",)
    assert set(effective_approved_paths(("src/a.py",), orchestrator._tracker_paths())) == {
        "src/a.py",
        "HANDBOOK.md",
    }


def test_the_default_config_still_seeds_this_repos_own_trackers(tmp_path):
    """The unchanged-behaviour half of the test above."""
    from autoloop.config import AutoloopConfig, BrowserConfig
    from autoloop.orchestrator import Orchestrator
    from autoloop.policy import PolicyConfig

    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url="https://chatgpt.com/c/x"),
        policy=PolicyConfig(),
        state_dir=tmp_path / ".autoloop",
        workers_root=tmp_path / "w",
    )
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator._config = config

    assert orchestrator._tracker_paths() == TRACKER_PATHS


# ---- 6. the section is still strict ------------------------------------------


def test_an_unknown_repo_key_is_refused_not_ignored(tmp_path):
    """The whole config loader's rule (`_check_keys`): a typo'd setting must
    never silently fall back to a default. That applies to the section whose
    settings decide authorization more than to any other."""
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path, '[repo]\ntracker_path = ["a.md"]\n'))

    assert "repo" in str(exc.value)


def test_json_serialisable_defaults_survive_a_round_trip():
    """`RepoConfig` values reach `state.json` / the review packet through
    `asdict`-shaped payloads; a tuple that is not JSON-safe would fail there
    rather than here."""
    payload = json.dumps(
        {
            "tracker_paths": list(RepoConfig().tracker_paths),
            "audit_report_glob": RepoConfig().audit_report_glob,
        }
    )
    assert json.loads(payload)["tracker_paths"] == list(TRACKER_PATHS)
