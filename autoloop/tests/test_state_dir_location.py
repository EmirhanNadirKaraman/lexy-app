"""Writable loop state lives outside the tree the escape detector snapshots.

`[paths].state_dir` defaulted to `.autoloop` — a cwd-relative path inside the
checkout — which made it the last of the loop's own writable paths still living
in the tree `escape_detector` observes. That detector enumerates IGNORED paths
on purpose (`.autoloop/` is gitignored in production, and `tasks.json` holds
`approved_paths`, so an agent forging state there is exactly what it exists to
catch), so every write the loop made to its own state mid-round was a diff
indistinguishable from an agent writing where it may not. The inbox, the PAUSE
flag, the heartbeat and the mutation ledger each moved beside `workers_root`
for that reason; this is the same move for the state dir (port-01, 2026-08-23).

What is pinned here, and nothing wider:

* an unconfigured default resolves beside `workers_root`, absolute, outside
  the checkout;
* an explicit `[paths].state_dir` is honoured verbatim — the compatibility
  contract, and the one line an existing deployment writes to stay put;
* every path the loop WRITES resolves under `state_dir` and only `state_dir`;
* the single legacy read that exists (`workers_dir`) finds the old location,
  and the new location wins whenever both are there.

Deliberately NOT pinned, because it is deliberately not implemented: there is
no migration. `state.json`, `tasks.json` and the lock are not read from the old
directory — see `docs/AUTOLOOP.md` §3h.

Since port-06 (2026-08-24) it also pins that the rule has ONE implementation:
the dashboard resolves through `config.resolve_state_dir` rather than its own
copy, so the loop and the page name the same directory under every shape of
`[paths]`, and a reader with nothing to resolve from raises instead of guessing.
The final section drives both readers against one config file — see
`docs/AUTOLOOP.md` §3h-bis.
"""

import json
import subprocess
from pathlib import Path

import pytest

from autoloop.config import (
    DEFAULT_STATE_DIR_NAME,
    LEGACY_STATE_DIR_NAME,
    AutoloopConfig,
    BrowserConfig,
    PolicyConfig,
    load_config,
)
from autoloop.errors import ConfigError

URL = "https://chatgpt.com/c/state-dir-location"


def write_config(checkout, workers_root, state_dir_line=""):
    """A config in the PRODUCTION shape: `<checkout>/.autoloop/config.toml`.

    That location is what `legacy_state_dir_for` reads, so a test writing its
    config to a bare `tmp_path` would silently exercise the no-legacy branch
    and prove nothing about the compatibility path.
    """
    cfg_dir = checkout / LEGACY_STATE_DIR_NAME
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg_dir / "config.toml"
    cfg.write_text(
        f'[browser]\nconversation_url = "{URL}"\n\n'
        f'[paths]\n{state_dir_line}workers_root = "{workers_root}"\n',
        encoding="utf-8",
    )
    return cfg


@pytest.fixture
def checkout(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    return path


@pytest.fixture
def workers_root(tmp_path):
    return tmp_path / "external" / "workers"


# --- the move itself ----------------------------------------------------------


def test_the_default_state_dir_sits_beside_the_workers_root(checkout, workers_root):
    config = load_config(write_config(checkout, workers_root))

    assert config.state_dir == workers_root.parent / DEFAULT_STATE_DIR_NAME
    assert config.state_dir.parent == workers_root.parent, (
        "beside workers_root, like the inbox, the pause flag, the heartbeat "
        "and the mutation ledger"
    )
    assert config.state_dir.is_absolute(), (
        "the old default was relative and resolved against each command's own "
        "cwd, so a run from the wrong directory reported on an empty state dir"
    )


def test_the_default_state_dir_is_not_inside_the_checkout(checkout, workers_root):
    config = load_config(write_config(checkout, workers_root))

    assert checkout not in config.state_dir.parents
    assert config.state_dir != checkout


def test_an_explicit_state_dir_is_honoured_verbatim(checkout, workers_root):
    """The compatibility contract. An operator who already said where their
    state lives keeps it there, byte for byte — including the relative value
    `config.example.toml` still ships."""
    for written in (str(checkout / ".al"), ".autoloop"):
        cfg = write_config(checkout, workers_root, f'state_dir = "{written}"\n')
        config = load_config(cfg)
        assert config.state_dir == Path(written)
        assert config.legacy_state_dir is None, (
            "nothing moved out from under this deployment, so there is no "
            "older location to look in"
        )


def test_a_workers_root_that_collides_with_the_default_is_refused(checkout, tmp_path):
    """`workers_root` ending in the default state dir's own name derives a
    state dir equal to it. Refused at load, naming the remedy, rather than
    surfacing later as `validate_workers_root`'s "nested beneath the state
    directory" to an operator who configured no state directory at all."""
    colliding = tmp_path / "external" / DEFAULT_STATE_DIR_NAME
    cfg = write_config(checkout, colliding)

    with pytest.raises(ConfigError, match="collides with the default state"):
        load_config(cfg)

    # And it is only ever the derived default that collides: naming the same
    # path explicitly is the operator's own call and still loads.
    explicit = write_config(checkout, colliding, f'state_dir = "{tmp_path / "s"}"\n')
    assert load_config(explicit).state_dir == tmp_path / "s"


# --- write only to the new one ------------------------------------------------

#: Paths that are NOT under `state_dir`, by design, each with its own reason:
#: the first two are the operator-writable flags that moved out first, the
#: third is git-tracked source beside the package, and `workers_dir` is the one
#: read-only path allowed a legacy fallback (see below).
EXTERNAL_BY_DESIGN = frozenset(
    {"pause_file", "heartbeat_file", "seed_tasks_file", "workers_dir"}
)


def test_every_path_the_loop_writes_resolves_under_the_state_dir(checkout, workers_root):
    """The property "write only to the new one" rests on, checked by
    REFLECTION rather than by a hand-listed set — a path property added later
    that forgot the rule would slip past a list, which is precisely how state
    ends up back inside the snapshotted tree.

    The legacy directory is POPULATED first, and that is the whole point of
    this test rather than a detail of it. Against an empty one, a read-through
    fallback added to `state_file` later would probe the old path, find
    nothing, return the new path — and this test would still pass, unable to
    tell "no fallback exists" from "the fallback found nothing"."""
    from autoloop.lock import LOCK_FILENAME
    from autoloop.publisher import (
        publisher_hooks_path,
        publisher_repo_path,
        publisher_url_snapshot_path,
    )

    legacy = checkout / LEGACY_STATE_DIR_NAME
    legacy.mkdir(parents=True, exist_ok=True)
    for name in ("state.json", "tasks.json", "pending_upgrade.json", "transcript.jsonl"):
        (legacy / name).write_text("{}\n", encoding="utf-8")
    for name in ("executions", "blockers", "manifests", "publisher.git"):
        (legacy / name).mkdir(exist_ok=True)

    config = load_config(write_config(checkout, workers_root))

    names = [
        name
        for name, attr in vars(AutoloopConfig).items()
        if isinstance(attr, property) and name not in EXTERNAL_BY_DESIGN
    ]
    assert "state_file" in names and "tasks_file" in names, "reflection found nothing"

    targets = [getattr(config, name) for name in names]
    # The four writable paths resolved OUTSIDE this class, by the modules that
    # own them (`lock.py`, `publisher.py`) — they are state paths too, and the
    # claim covers them.
    targets += [
        config.state_dir / LOCK_FILENAME,
        publisher_repo_path(config.state_dir),
        publisher_hooks_path(config.state_dir),
        publisher_url_snapshot_path(config.state_dir),
    ]

    # Both sides resolved: `publisher_repo_path` resolves its own return value,
    # and macOS's `/var` -> `/private/var` symlink would otherwise make two
    # spellings of one path compare unequal.
    state_root = config.state_dir.resolve()
    checkout_root = checkout.resolve()
    for target in targets:
        resolved = Path(target).resolve()
        assert state_root in resolved.parents, f"{target} escaped state_dir"
        assert checkout_root not in resolved.parents, f"{target} is inside the checkout"


def test_writing_the_state_dir_is_not_something_the_escape_detector_sees(
    checkout, workers_root
):
    """The regression, driven through the REAL detector rather than by
    asserting a path shape: write state between the two snapshots and prove
    nothing is reported. Under the old default this is the exact diff that
    reported an escape and parked the loop `loop_fatal`."""
    from autoloop.escape_detector import (
        diff_snapshots,
        enumerate_checkout_paths,
        snapshot_checkout,
    )
    from autoloop.git_gateway import GitGateway
    from autoloop.policy import PolicyEngine

    for argv in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "t@example.com"),
        ("config", "user.name", "T"),
        ("config", "commit.gpgsign", "false"),
    ):
        subprocess.run(["git", *argv], cwd=checkout, check=True, capture_output=True)
    (checkout / "README.md").write_text("hi\n", encoding="utf-8")
    # Gitignored in production — which is why the detector's ignored-path
    # enumeration reaches it at all.
    (checkout / ".gitignore").write_text(f"{LEGACY_STATE_DIR_NAME}/\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=checkout, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=checkout, check=True,
                   capture_output=True)

    config = load_config(write_config(checkout, workers_root))
    git = GitGateway(checkout, PolicyEngine(PolicyConfig()))
    before = snapshot_checkout(checkout, enumerate_checkout_paths(git))

    config.state_file.parent.mkdir(parents=True, exist_ok=True)
    config.state_file.write_text("{}\n", encoding="utf-8")
    config.tasks_file.write_text("[]\n", encoding="utf-8")

    after = snapshot_checkout(checkout, enumerate_checkout_paths(git))
    assert diff_snapshots(before, after) == [], "writing state must not look like an escape"

    # The control: the OLD location still trips it, which is the whole bug.
    legacy = checkout / LEGACY_STATE_DIR_NAME / "state.json"
    legacy.write_text("{}\n", encoding="utf-8")
    after_legacy = snapshot_checkout(checkout, enumerate_checkout_paths(git))
    assert diff_snapshots(before, after_legacy), (
        "the old in-checkout location should still be detectable — if this "
        "passes, the test proves nothing about the move"
    )


# --- the one legacy read ------------------------------------------------------


def test_stray_pre_fix_worker_repos_are_still_found_in_the_old_state_dir(
    checkout, workers_root
):
    """`workers_dir`'s whole job is finding worker repos a PRE-FIX deployment
    left inside the checkout (`doctor`'s `legacy_workers` check, read-only).
    Moving `state_dir` pointed it at a directory no such deployment ever wrote
    to, so without this the check would report nothing while still reading as
    a check that ran."""
    stray = checkout / LEGACY_STATE_DIR_NAME / "workers" / "t1"
    stray.mkdir(parents=True)
    config = load_config(write_config(checkout, workers_root))

    assert config.legacy_state_dir == checkout / LEGACY_STATE_DIR_NAME
    assert config.workers_dir == checkout / LEGACY_STATE_DIR_NAME / "workers"
    assert config.workers_dir.is_dir()


def test_the_new_location_wins_whenever_both_exist(checkout, workers_root):
    """New first, so a fallback can never shadow live state."""
    (checkout / LEGACY_STATE_DIR_NAME / "workers" / "t1").mkdir(parents=True)
    config = load_config(write_config(checkout, workers_root))
    (config.state_dir / "workers").mkdir(parents=True)

    assert config.workers_dir == config.state_dir / "workers"


def test_no_legacy_directory_means_the_new_path_not_a_crash(checkout, workers_root):
    """Absent, and file-shaped rather than absent — both fall through to the
    new path rather than raising or being returned as a directory."""
    config = load_config(write_config(checkout, workers_root))
    assert config.workers_dir == config.state_dir / "workers"

    (checkout / LEGACY_STATE_DIR_NAME / "workers").write_text("not a dir\n")
    assert config.workers_dir == config.state_dir / "workers"


def test_a_stray_file_at_the_new_path_does_not_silence_the_legacy_report(
    checkout, workers_root
):
    """Precedence is "the first that is a real DIRECTORY", not "the first that
    exists". A plain file at `state_dir/workers` is not worker repos, and
    reading it as "the new location is populated" would take `doctor`'s stray
    report quiet on garbage — a check passing because of what it could not
    find."""
    (checkout / LEGACY_STATE_DIR_NAME / "workers" / "t1").mkdir(parents=True)
    config = load_config(write_config(checkout, workers_root))
    config.state_dir.mkdir(parents=True, exist_ok=True)
    (config.state_dir / "workers").write_text("not a dir\n")

    assert config.workers_dir == checkout / LEGACY_STATE_DIR_NAME / "workers"


# --- one resolver, two readers (port-06, 2026-08-24) --------------------------
#
# The loop was not the only thing resolving `[paths].state_dir`. `dashboard.
# _state_dir` resolved it too, and ended `return repo / ".autoloop"` when the key
# was absent — the PRE-port-01 location, inside the checkout — while the loop
# resolved `default_state_dir(workers_root)` outside it. So a deployment that
# followed the shape documented above got a loop writing one directory and a page
# reading another.
#
# It is worse than a stale page because `dashboard._tasks_file` also WRITES: a
# priority set from the page would have gone into an abandoned registry, been
# read back correctly from that same file, and never reached the running loop.
#
# Both readers now call `config.resolve_state_dir`. These tests drive the two of
# them against ONE config file and compare their answers — never against a path
# the test spelled, which would only pin what the test itself supplied.

TASK_ROW = {
    "id": "t-1", "title": "T", "description": "d", "status": "pending",
    "priority": 3, "depends_on": [], "approved_paths": ["docs/A.md"],
}


def page_state_dir(checkout):
    """What the DASHBOARD resolves for this checkout, through its own reader."""
    from autoloop import dashboard

    return dashboard._state_dir(checkout)


def test_with_state_dir_absent_both_readers_resolve_the_same_directory(
    checkout, workers_root
):
    """The divergence itself. Absent is the state a fresh checkout, port-04 and
    port-05 all start in, and the one this deployment does not happen to be in
    — which is why it went unnoticed."""
    cfg = write_config(checkout, workers_root)

    assert page_state_dir(checkout) == load_config(cfg).state_dir
    assert page_state_dir(checkout).is_absolute()
    assert page_state_dir(checkout) != checkout / LEGACY_STATE_DIR_NAME, (
        "the pre-port-01 location is what the page used to answer here"
    )


def test_an_absolute_state_dir_is_honoured_by_both(checkout, workers_root, tmp_path):
    """The compatibility contract, from both sides: an operator who says where
    their state lives is obeyed verbatim by the loop AND by the page."""
    explicit = tmp_path / "elsewhere" / "loop-state"
    cfg = write_config(checkout, workers_root, f'state_dir = "{explicit}"\n')

    assert load_config(cfg).state_dir == explicit
    assert page_state_dir(checkout) == explicit


def test_a_relative_state_dir_resolves_against_the_checkout_for_both(
    checkout, workers_root, monkeypatch
):
    """A relative value is the shape `config.example.toml` still ships, and the
    one where "verbatim" and "the same directory" have to be reconciled rather
    than asserted.

    The loop keeps the operator's literal value and resolves it against its OWN
    cwd; the page resolves it against the checkout. Those agree because the
    loop's cwd IS the checkout — `cli` builds every gateway on `Path.cwd()` and
    reads the relative `.autoloop/config.toml`. `monkeypatch.chdir` is what
    EXECUTES that claim here instead of restating it in prose.
    """
    cfg = write_config(checkout, workers_root, 'state_dir = "loopstate"\n')
    config = load_config(cfg)

    assert config.state_dir == Path("loopstate"), "verbatim — port-01's contract"

    monkeypatch.chdir(checkout)
    assert config.state_dir.resolve() == page_state_dir(checkout).resolve()


@pytest.mark.parametrize("shape", ["absent", "absolute", "relative"])
def test_a_priority_edit_from_the_page_lands_where_the_loop_reads(
    shape, checkout, workers_root, tmp_path, monkeypatch
):
    """The consequence, driven end to end: the dashboard's own write path, read
    back through the LOOP's own path.

    Reading it back through `dashboard._tasks_file` would prove only that the
    page agrees with itself — which is exactly what the broken version did, and
    exactly why an operator could not tell.
    """
    from autoloop import dashboard
    from autoloop.tasks import TASKS_SCHEMA_VERSION, TaskStore

    line = {
        "absent": "",
        "absolute": f'state_dir = "{tmp_path / "explicit-state"}"\n',
        "relative": 'state_dir = "loopstate"\n',
    }[shape]
    cfg = write_config(checkout, workers_root, line)
    # Before `load_config`, so the relative shape is resolved from the directory
    # a real loop runs in rather than from wherever pytest was started.
    monkeypatch.chdir(checkout)
    config = load_config(cfg)

    loop_registry = Path(config.tasks_file)
    loop_registry.parent.mkdir(parents=True, exist_ok=True)
    loop_registry.write_text(
        json.dumps({"schema_version": TASKS_SCHEMA_VERSION, "tasks": [TASK_ROW]}),
        encoding="utf-8",
    )

    applied = dashboard._task_store(checkout).apply_priority("t-1", 1)

    assert applied.priority == 1
    reread = TaskStore(config.tasks_file).load()
    assert reread is not None, f"the page wrote a registry the loop cannot find ({shape})"
    assert reread.get("t-1").priority == 1


def test_a_state_directory_that_cannot_be_resolved_is_an_error_not_a_default(checkout):
    """The rule that keeps this fixed: a reader with nothing to resolve FROM
    says so. A silent fallback is what produced the divergence, and it produced
    it invisibly — the page rendered a complete, plausible, abandoned state
    directory."""
    from autoloop import dashboard

    cfg_dir = checkout / LEGACY_STATE_DIR_NAME
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg_dir / "config.toml"

    # Names neither key: the loop refuses to load, and the page refuses to
    # answer, for the same missing setting.
    cfg.write_text(f'[browser]\nconversation_url = "{URL}"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="workers_root"):
        load_config(cfg)
    with pytest.raises(ConfigError, match="workers_root") as page:
        dashboard._state_dir(checkout)
    assert "could not be resolved" in str(page.value)

    # No config file at all — a checkout nobody has configured. Still an error,
    # and it names the file rather than reading the old directory.
    cfg.unlink()
    with pytest.raises(ConfigError, match="does not exist"):
        dashboard._state_dir(checkout)


def test_a_state_dir_that_is_not_a_usable_path_is_refused_by_both(
    checkout, workers_root
):
    """`Path("")` is `.` — the process's cwd, which is a guess wearing a value's
    clothes and would differ between the two readers by construction. Blank and
    non-string values are refused instead, by the one resolver, so neither
    reader can invent an answer the other would not."""
    from autoloop import dashboard
    from autoloop.config import resolve_state_dir

    for written in ('state_dir = ""\n', 'state_dir = "   "\n', "state_dir = 7\n"):
        cfg = write_config(checkout, workers_root, written)
        with pytest.raises(ConfigError, match="state_dir"):
            load_config(cfg)
        with pytest.raises(ConfigError, match="state_dir"):
            dashboard._state_dir(checkout)

    # And directly, including the shape no TOML file can produce but a caller
    # can: no configured value AND no workers root to derive one from.
    with pytest.raises(ConfigError, match="workers_root"):
        resolve_state_dir(None, None)


def test_the_resolver_is_the_only_state_dir_rule_the_dashboard_has(
    checkout, workers_root, monkeypatch
):
    """The structural half of the claim: `dashboard._state_dir` DELEGATES.

    Asserted by replacing the shared resolver and watching the page's answer
    move with it — a second implementation, however faithfully it agreed today,
    would keep answering the old way and this would fail. A source grep for
    `repo / ".autoloop"` was rejected for the reason `test_dashboard.py` records:
    it pins the literal the test itself supplies.
    """
    import autoloop.dashboard as dashboard_module

    cfg = write_config(checkout, workers_root)
    assert dashboard_module._state_dir(checkout) == load_config(cfg).state_dir

    sentinel = Path("/sentinel/state/dir")
    monkeypatch.setattr(dashboard_module, "resolve_state_dir",
                        lambda *args, **kwargs: sentinel)
    assert dashboard_module._state_dir(checkout) == sentinel


def test_a_hand_built_config_without_a_legacy_dir_is_unchanged(tmp_path):
    """Every direct `AutoloopConfig(...)` construction predates this field and
    leaves it `None`, so `workers_dir` must resolve exactly as it always did —
    no filesystem probing, no fallback."""
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=tmp_path / ".al",
        workers_root=tmp_path / "workers",
    )

    assert config.legacy_state_dir is None
    assert config.workers_dir == tmp_path / ".al" / "workers"
