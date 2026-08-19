"""Audit charters shipped by the TARGET repository (port-03).

The per-domain charters in `audit/executor.py` describe THIS repository — its
two-backend split, its ingestion pipeline, the docs it keeps — and that
specificity is what makes the audit produce findings worth reading. It is also
what stopped the loop being pointed anywhere else: the knowledge lived in the
loop's source instead of beside the code it describes. A target repository may
now ship `docs/audit_charters.toml` (path configurable, `[repo]
audit_charters_file`) and the built-in `DEFAULT_DOMAINS` is the fallback.

Four claims, in the order this file makes them:

1. **A repository that ships no file behaves exactly as before.** That
   equivalence is the premise under which built-in charters were allowed to
   become a file at all, so it is pinned first — as an identity between the
   prompts, not merely as "six domains ran".
2. **The move is lossless.** `render_charter_file` → `parse_charter_domains`
   returns `DEFAULT_DOMAINS` byte for byte, which is what makes "the two-backend
   wording can live in the repository rather than in executable code" a fact
   rather than an intention.
3. **A malformed file fails closed.** It must not degrade to the built-ins:
   auditing this repository's domains inside somebody else's checkout would
   produce a report that looks complete and describes the wrong codebase.
4. **Loading is read-only and repository-scoped.** Per call, from the root of
   the checkout being audited — so one process auditing two repositories uses
   each one's charters and neither leaks into the other.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from autoloop.audit.agents import AgentResult
from autoloop.audit.executor import (
    CHARTER_MODELS,
    DEFAULT_DOMAINS,
    AuditExecutor,
    _agent_prompt,
    load_charter_domains,
    parse_charter_domains,
    render_charter_file,
)
from autoloop.audit.markdown import MarkdownPolicy
from autoloop.config import DEFAULT_AUDIT_CHARTERS_FILE, RepoConfig, load_config
from autoloop.contract import Decision, Directive
from autoloop.errors import AuditError, ConfigError
from autoloop.git_gateway import GitGateway
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.tasks import Task, TaskRegistry

CONFIG_HEAD = '[browser]\nconversation_url = "https://chatgpt.com/c/x"\n\n'


def run_git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def make_repo(root: Path) -> Path:
    """A git repository with a `docs/` directory, ready to be audited."""
    (root / "docs").mkdir(parents=True)
    run_git(root, "init", "-q", "-b", "main")
    run_git(root, "config", "user.email", "t@e.c")
    run_git(root, "config", "user.name", "T")
    run_git(root, "config", "commit.gpgsign", "false")
    (root / "README.md").write_text("hi", encoding="utf-8")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "repo")


class FakeRunner:
    """Records the specs it is handed; returns an empty finding set."""

    def __init__(self):
        self.specs = []

    def run(self, spec):
        self.specs.append(spec)
        return AgentResult(
            domain=spec.domain,
            raw_text=json.dumps({"findings": []}),
            returncode=0,
            duration_seconds=0.1,
            command=("claude",),
        )


def ok_command(argv, **kwargs):
    class Proc:
        returncode = 0
        stdout = "All checks passed!\n"
        stderr = ""

    return Proc()


def build_executor(repo: Path, tmp_path: Path, runner=None, **kwargs) -> AuditExecutor:
    return AuditExecutor(
        git=GitGateway(repo, PolicyEngine(PolicyConfig())),
        agent_runner=runner or FakeRunner(),
        markdown=MarkdownPolicy(repo),
        registry=TaskRegistry(),
        run_dir_base=tmp_path / "runs",
        validation_commands=(),
        max_parallel_agents=2,
        command_runner=ok_command,
        **kwargs,
    )


def audit_directive(scope=None):
    return Directive(decision=Decision.AUDIT, reason="r", scope=scope)


def ship_charters(repo: Path, body: str, relative: str = DEFAULT_AUDIT_CHARTERS_FILE) -> Path:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def domain_tuples(runner: FakeRunner) -> list[tuple[str, str, str]]:
    return [(s.domain, s.title, s.model) for s in runner.specs]


#: A charter set for an imaginary target repository: different domains,
#: different order, different models, and prose that shares nothing with the
#: built-ins. Its second domain carries a two-backend distinction of its own —
#: the point being that "this repo has two backends" is knowledge a repository
#: states about itself, not a fact the loop's source has to know.
OTHER_REPO_CHARTERS = """
[[domain]]
slug = "api_contracts"
title = "Public API contracts"
model = "sonnet"
charter = '''Compare openapi.yaml against the handlers in cmd/server. Report
endpoints documented but unimplemented, and implemented but undocumented.'''

[[domain]]
slug = "service_split"
title = "Service split"
model = "haiku"
charter = '''This repository has TWO backends: the Go service in cmd/server
(production) and the Python prototype in research/, which nothing deploys.
Report code that assumes they share a database.'''
"""

OTHER_SLUGS = ["api_contracts", "service_split"]


# ---- 1. no file at all is exactly the previous behaviour ---------------------


def test_a_repository_that_ships_no_charter_file_gets_the_built_in_domains(repo, tmp_path):
    """The compatibility claim, as an identity between PROMPTS rather than a
    count. A count would still pass if the fallback silently reworded a
    charter on the way through."""
    runner = FakeRunner()
    outcome = build_executor(repo, tmp_path, runner).execute(audit_directive(), None)

    assert outcome.status == "ok"
    assert not (repo / DEFAULT_AUDIT_CHARTERS_FILE).exists()
    assert [(s.domain, s.title, s.model, s.prompt) for s in runner.specs] == [
        (slug, title, model, _agent_prompt(title, charter, None, None))
        for slug, title, charter, model in DEFAULT_DOMAINS
    ]


def test_the_summary_says_nothing_about_charters_when_the_built_ins_are_used(repo, tmp_path):
    """A run against the built-ins produces the summary it always produced —
    the charter clause appears only when it is true."""
    outcome = build_executor(repo, tmp_path).execute(audit_directive(), None)

    assert "across 6 domains" in outcome.summary
    assert "Domain charters" not in outcome.summary


def test_an_absent_file_is_compatibility_and_never_an_error(tmp_path):
    """Both no-op paths return the sentinel that means "use the built-ins":
    nothing there, and the operator's explicit opt-out."""
    repo = make_repo(tmp_path / "bare")

    assert load_charter_domains(repo, DEFAULT_AUDIT_CHARTERS_FILE) is None
    assert load_charter_domains(repo, "") is None


def test_the_opt_out_ignores_a_file_that_is_present(repo, tmp_path):
    """`audit_charters_file = ""` means "never look", so a checkout that
    happens to contain one is not read."""
    ship_charters(repo, OTHER_REPO_CHARTERS)
    runner = FakeRunner()

    build_executor(repo, tmp_path, runner, charters_file="").execute(audit_directive(), None)

    assert [s.domain for s in runner.specs] == [slug for slug, *_ in DEFAULT_DOMAINS]


# ---- 2. the built-in charters can move into the file, verbatim ---------------


def test_the_built_in_charters_round_trip_through_the_file_format():
    """THE portability claim: names, order, model routing and the full prompt
    text survive rendering to a file and parsing back, with no normalization.
    Everything else in this file rests on this equality."""
    rendered = render_charter_file(DEFAULT_DOMAINS)

    assert parse_charter_domains(rendered, "rendered") == DEFAULT_DOMAINS


def test_the_two_backend_wording_is_carried_by_the_file_not_by_code():
    """The repository-specific knowledge named in the task, shown crossing the
    boundary: it is in the rendered file's bytes, and it comes back out of the
    parser unchanged."""
    two_backend = "the two-backend split (lexy-app/backend vs root pipeline modules)"
    rendered = render_charter_file(DEFAULT_DOMAINS)

    assert two_backend in rendered
    parsed = dict((slug, charter) for slug, _title, charter, _model in
                  parse_charter_domains(rendered, "rendered"))
    assert two_backend in parsed["repo_structure"]


def test_a_repository_shipping_the_built_in_charters_produces_identical_prompts(
    repo, tmp_path
):
    """End to end: with this repository's own charters moved into the file, the
    audit is the same audit. That is what "moved verbatim" has to mean."""
    ship_charters(repo, render_charter_file(DEFAULT_DOMAINS))
    from_file, built_in = FakeRunner(), FakeRunner()

    build_executor(repo, tmp_path, from_file).execute(audit_directive(), None)
    build_executor(make_repo(tmp_path / "clean"), tmp_path, built_in).execute(
        audit_directive(), None
    )

    assert [s.prompt for s in from_file.specs] == [s.prompt for s in built_in.specs]


def test_a_charter_holding_the_literal_delimiter_is_refused_by_the_renderer():
    """`render_charter_file` writes `'''` literal strings. A charter containing
    that delimiter cannot be written this way, and emitting a file that would
    parse as something else is the one outcome a round-trip helper must not
    have."""
    with pytest.raises(AuditError) as exc:
        render_charter_file((("d", "T", "quotes ''' inside", "haiku"),))

    assert "'''" in str(exc.value)


# ---- 3. a target file replaces the built-ins ---------------------------------


def test_a_target_file_overrides_the_built_in_domains_text_and_order(repo, tmp_path):
    ship_charters(repo, OTHER_REPO_CHARTERS)
    runner = FakeRunner()

    outcome = build_executor(repo, tmp_path, runner).execute(audit_directive(), None)

    assert outcome.status == "ok"
    assert domain_tuples(runner) == [
        ("api_contracts", "Public API contracts", "sonnet"),
        ("service_split", "Service split", "haiku"),
    ]
    # The file's own two-backend wording reaches its agent...
    assert "the Go service in cmd/server" in runner.specs[1].prompt
    # ...and nothing from this repository's built-in charters comes along.
    for spec in runner.specs:
        assert "lexy-app/backend" not in spec.prompt
        assert "docs/INGESTION_PIPELINE.md" not in spec.prompt


def test_the_ground_rules_still_frame_a_repository_supplied_charter(repo, tmp_path):
    """The charter is prose the repository chooses; the read-only ground rules
    and the findings schema are not. A file cannot drop them by omission."""
    ship_charters(repo, OTHER_REPO_CHARTERS)
    runner = FakeRunner()

    build_executor(repo, tmp_path, runner).execute(audit_directive(scope="uploads"), None)

    for spec in runner.specs:
        assert "READ-ONLY" in spec.prompt
        assert "Stay in your domain." in spec.prompt
        assert "NARROWS your own domain" in spec.prompt
        assert "uploads" in spec.prompt


def test_the_report_and_summary_describe_the_domains_that_actually_ran(repo, tmp_path):
    """The lie this test exists to prevent: agents fanned out over the file's
    domains while the coverage table and the domain COUNT still described the
    built-ins. Every reader of the report would have been misinformed, and every
    prompt-shaped test above would still pass."""
    ship_charters(repo, OTHER_REPO_CHARTERS)

    outcome = build_executor(repo, tmp_path).execute(audit_directive(), None)

    assert "across 2 domains" in outcome.summary
    assert f"Domain charters came from {DEFAULT_AUDIT_CHARTERS_FILE}" in outcome.summary
    assert "## Domain coverage — 2/2 domains reported usable output" in outcome.details
    for slug in OTHER_SLUGS:
        assert f"`{slug}`" in outcome.details
    assert "`ingestion_pipeline`" not in outcome.details


def test_raw_agent_output_is_filed_under_the_files_own_slugs(repo, tmp_path):
    ship_charters(repo, OTHER_REPO_CHARTERS)

    build_executor(repo, tmp_path).execute(audit_directive(), None)

    [run_dir] = (tmp_path / "runs").iterdir()
    assert sorted(p.name for p in (run_dir / "raw").glob("*.txt")) == [
        "api_contracts.txt",
        "service_split.txt",
    ]


# ---- 4. malformed content fails closed --------------------------------------

GOOD_ENTRY = """
[[domain]]
slug = "a"
title = "A"
model = "haiku"
charter = '''audit a'''
"""

#: (label, body, a phrase the refusal must contain). Each one is a way a
#: charter file can be wrong that MUST NOT degrade to a built-in or partial
#: audit — the report would look complete either way.
MALFORMED = [
    ("not toml", "this is not toml {{", "not valid TOML"),
    ("no domain array", 'title = "A"', "unknown top-level key"),
    ("domain is a table", "[domain]\nslug = \"a\"", "array of"),
    ("empty array", "domain = []", "defines no domains"),
    (
        "missing charter",
        '[[domain]]\nslug = "a"\ntitle = "A"\nmodel = "haiku"\n',
        "missing required field",
    ),
    (
        "unknown field",
        GOOD_ENTRY + 'extra = "x"\n',
        "unknown field",
    ),
    (
        "non-string value",
        '[[domain]]\nslug = "a"\ntitle = "A"\nmodel = "haiku"\ncharter = 7\n',
        "must be a string",
    ),
    (
        "blank charter",
        '[[domain]]\nslug = "a"\ntitle = "A"\nmodel = "haiku"\ncharter = "   "\n',
        "is empty",
    ),
    ("duplicate slug", GOOD_ENTRY + GOOD_ENTRY, "duplicate slug"),
    (
        "slug with a path separator",
        '[[domain]]\nslug = "../evil"\ntitle = "A"\nmodel = "h"\ncharter = "c"\n',
        "`slug` must be",
    ),
    (
        "unusable model",
        '[[domain]]\nslug = "a"\ntitle = "A"\nmodel = "gpt-9"\ncharter = "c"\n',
        "`model` must be one of",
    ),
]


@pytest.mark.parametrize("label,body,phrase", MALFORMED, ids=[m[0] for m in MALFORMED])
def test_malformed_charter_content_is_refused_with_an_actionable_message(
    tmp_path, label, body, phrase
):
    repo = make_repo(tmp_path / "r")
    path = ship_charters(repo, body)

    with pytest.raises(AuditError) as exc:
        load_charter_domains(repo, DEFAULT_AUDIT_CHARTERS_FILE)

    message = str(exc.value)
    assert phrase in message, message
    assert str(path) in message, "the refusal must name the file it read"


def test_a_malformed_file_stops_the_run_before_any_agent_is_launched(repo, tmp_path):
    """Fails CLOSED, and cheaply: no agents, no report, no run directory — the
    honest answer to a configuration fault is to have done nothing yet."""
    ship_charters(repo, "domain = []")
    runner = FakeRunner()

    outcome = build_executor(repo, tmp_path, runner).execute(audit_directive(), None)

    assert outcome.status == "error"
    assert "audit charters could not be loaded" in outcome.summary
    assert "defines no domains" in outcome.summary
    assert outcome.validation == "not run"
    assert runner.specs == []
    assert not (tmp_path / "runs").exists()
    assert list((repo / "docs").glob("AUDIT_*.md")) == []


def test_a_malformed_file_never_degrades_to_the_built_in_charters(repo, tmp_path):
    """The specific wrong repair. Falling back would audit THIS repository's
    domains inside another repository's checkout and file a report that reads
    as complete."""
    ship_charters(repo, "domain = []")
    runner = FakeRunner()

    outcome = build_executor(repo, tmp_path, runner).execute(audit_directive(), None)

    assert outcome.status == "error"
    assert [s.domain for s in runner.specs] == []


def test_an_unreported_failure_is_impossible_because_it_is_an_outcome(repo, tmp_path):
    """Reported as an `ExecutionOutcome`, not raised: `Orchestrator.run`'s
    handler chain catches browser/git/state faults, so an `AuditError` escaping
    the executor would take the process down with a traceback instead of
    reaching the reviewer who can answer it."""
    ship_charters(repo, "this is not toml {{")

    outcome = build_executor(repo, tmp_path).execute(audit_directive(), None)

    assert outcome.status == "error"
    assert DEFAULT_AUDIT_CHARTERS_FILE.split("/")[-1] in outcome.summary


def test_the_model_set_excludes_the_leads_tier(tmp_path):
    """`DEFAULT_DOMAINS` never delegates to the expensive tier, and a file the
    operator paying for the run does not own is not where that gets reversed."""
    assert "opus" not in CHARTER_MODELS
    assert "fable" not in CHARTER_MODELS
    assert set(model for *_, model in DEFAULT_DOMAINS) <= set(CHARTER_MODELS)


# ---- 5. read-only, repository-scoped ----------------------------------------


def test_a_run_does_not_modify_the_charter_file(repo, tmp_path):
    body = OTHER_REPO_CHARTERS
    path = ship_charters(repo, body)

    build_executor(repo, tmp_path).execute(audit_directive(), None)

    assert path.read_text(encoding="utf-8") == body


def test_a_symlinked_charter_file_is_refused_rather_than_followed(tmp_path):
    """The charters must be part of the repository under audit. A link can
    point anywhere, so it is a clear refusal rather than a silent read."""
    repo = make_repo(tmp_path / "r")
    outside = tmp_path / "elsewhere.toml"
    outside.write_text(OTHER_REPO_CHARTERS, encoding="utf-8")
    link = repo / DEFAULT_AUDIT_CHARTERS_FILE
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)

    with pytest.raises(AuditError) as exc:
        load_charter_domains(repo, DEFAULT_AUDIT_CHARTERS_FILE)

    assert "symlink" in str(exc.value)


@pytest.mark.parametrize(
    "bad", ["/etc/charters.toml", "../elsewhere/charters.toml", " docs/c.toml", "docs/../../c"]
)
def test_the_loader_refuses_a_path_that_would_read_outside_the_repository(tmp_path, bad):
    """Defense in depth: `load_config` already refuses these, but this function
    is the one that turns a string into a filesystem read, and `AuditExecutor`
    can be constructed directly."""
    repo = make_repo(tmp_path / "r")

    with pytest.raises(AuditError) as exc:
        load_charter_domains(repo, bad)

    assert "relative to the repository root" in str(exc.value)


def worker_executor(main_repo: Path, roots: dict[str, Path], tmp_path: Path, runner):
    """An executor wired the way `cli._build_executor` wires production: bound
    to the main checkout, re-rooted per call onto that task's worker repo."""
    return build_executor(
        main_repo,
        tmp_path,
        runner,
        worker_repo_root_for=lambda task_id: roots[task_id],
        policy=PolicyEngine(PolicyConfig()),
    )


def test_the_charters_read_are_those_of_the_checkout_the_call_is_rooted_at(tmp_path):
    """The audit runs inside its own worker repo, so that is the repository
    whose charters apply — never the main checkout the executor was built
    against, and never the loop's own source tree."""
    main = make_repo(tmp_path / "main")
    worker = make_repo(tmp_path / "worker")
    ship_charters(main, render_charter_file(DEFAULT_DOMAINS))
    ship_charters(worker, OTHER_REPO_CHARTERS)
    runner = FakeRunner()

    outcome = worker_executor(main, {"audit-1": worker}, tmp_path, runner).execute(
        audit_directive(), Task(id="audit-1", title="audit", description="")
    )

    assert outcome.status == "ok"
    assert [s.domain for s in runner.specs] == OTHER_SLUGS
    assert list((main / "docs").glob("AUDIT_*.md")) == [], "the report goes to the worker"
    assert len(list((worker / "docs").glob("AUDIT_*.md"))) == 1


def test_two_repositories_audited_by_one_executor_do_not_leak_charters(tmp_path):
    """No process-global state: the charter set is resolved per call from that
    call's own repo root, so a first repository's domains cannot describe a
    second — and re-auditing the first still gets the first's."""
    main = make_repo(tmp_path / "main")
    first, second = make_repo(tmp_path / "first"), make_repo(tmp_path / "second")
    ship_charters(first, OTHER_REPO_CHARTERS)
    ship_charters(
        second,
        '[[domain]]\nslug = "only_here"\ntitle = "Only here"\nmodel = ""\n'
        "charter = '''audit the second repository'''\n",
    )
    runner = FakeRunner()
    executor = worker_executor(
        main, {"a-1": first, "a-2": second}, tmp_path, runner
    )

    def domains_for(task_id):
        before = len(runner.specs)
        executor.execute(audit_directive(), Task(id=task_id, title="t", description=""))
        return [s.domain for s in runner.specs[before:]]

    assert domains_for("a-1") == OTHER_SLUGS
    assert domains_for("a-2") == ["only_here"]
    assert domains_for("a-1") == OTHER_SLUGS, "the second run did not stick"


def test_a_repository_with_no_file_falls_back_even_when_a_sibling_ships_one(tmp_path):
    """There is no search and no shared location: an audit of a repository that
    ships nothing gets the built-ins, whatever the checkout next door holds."""
    main = make_repo(tmp_path / "main")
    with_file, without = make_repo(tmp_path / "with"), make_repo(tmp_path / "without")
    ship_charters(with_file, OTHER_REPO_CHARTERS)
    runner = FakeRunner()
    executor = worker_executor(main, {"a-1": with_file, "a-2": without}, tmp_path, runner)

    executor.execute(audit_directive(), Task(id="a-1", title="t", description=""))
    executor.execute(audit_directive(), Task(id="a-2", title="t", description=""))

    assert [s.domain for s in runner.specs[len(OTHER_SLUGS):]] == [
        slug for slug, *_ in DEFAULT_DOMAINS
    ]


# ---- 6. the config surface follows the port-02 convention -------------------


def write_config(tmp_path: Path, repo_section: str = "") -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG_HEAD + f'[paths]\nworkers_root = "{tmp_path / "w"}"\n\n' + repo_section,
        encoding="utf-8",
    )
    return path


def test_the_default_is_the_documented_location_and_needs_no_config(tmp_path):
    assert RepoConfig().audit_charters_file == DEFAULT_AUDIT_CHARTERS_FILE
    assert DEFAULT_AUDIT_CHARTERS_FILE == "docs/audit_charters.toml"
    assert load_config(write_config(tmp_path)).repo == RepoConfig()


def test_a_configured_relative_path_is_accepted_and_reaches_the_loader(tmp_path):
    config = load_config(
        write_config(tmp_path, '[repo]\naudit_charters_file = "tools/charters.toml"\n')
    )
    repo = make_repo(tmp_path / "r")
    ship_charters(repo, OTHER_REPO_CHARTERS, "tools/charters.toml")

    assert config.repo.audit_charters_file == "tools/charters.toml"
    loaded = load_charter_domains(repo, config.repo.audit_charters_file)
    assert [slug for slug, *_ in loaded] == OTHER_SLUGS
    # ...and the default location is genuinely not consulted.
    assert load_charter_domains(repo, DEFAULT_AUDIT_CHARTERS_FILE) is None


@pytest.mark.parametrize(
    "bad", ["/etc/charters.toml", "../charters.toml", "  ", " docs/c.toml", "docs/c.toml "]
)
def test_an_absolute_traversing_or_padded_path_is_refused_at_load(tmp_path, bad):
    """Same rule, same refusals as `env_example_file` — including that only the
    EXACT empty string is the opt-out, so a typo cannot switch the setting off
    while reading as configured."""
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path, f'[repo]\naudit_charters_file = "{bad}"\n'))

    assert "repo.audit_charters_file" in str(exc.value)


def test_a_glob_is_refused_because_this_names_exactly_one_file(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, '[repo]\naudit_charters_file = "docs/*.toml"\n'))


def test_the_exact_empty_string_is_the_opt_out(tmp_path):
    config = load_config(write_config(tmp_path, '[repo]\naudit_charters_file = ""\n'))

    assert config.repo.audit_charters_file == ""


def test_the_cli_hands_the_configured_path_to_the_audit_executor():
    """The wiring, not just the setting: a config value the executor never
    receives is a setting that reads as configured and does nothing — the
    failure `_check_keys` exists to prevent, one layer up. A source scan for
    the same reason `test_config_repo_section.py` scans `orchestrator.py`:
    the property is about the call site, not about a return value.
    """
    source = (Path(__file__).resolve().parents[1] / "cli.py").read_text(encoding="utf-8")
    build = source.split("def _build_executor(")[1].split("\ndef ")[0]

    assert "charters_file=config.repo.audit_charters_file" in build
