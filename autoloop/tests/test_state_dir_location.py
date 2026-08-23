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
"""

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
