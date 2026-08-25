"""Operator intake — the exchange, the three entry points, and the gates.

THE CLAIM these tests exist to break: a free-text idea — typed into the
dashboard, passed on the CLI, or dropped in as a `.md`/`.txt` file — produces a
DRAFT task through a question-and-answer exchange with the operator, and that
draft reaches the registry only when the operator submits it.

So the tests below are organised around the ways that claim could be TRUE-ish
and still wrong, not around the functions in `inbox.py`:

* the draft-emission gate passing because the model was silent (fail-open),
* a model reply that is our own prompt handed back (echo),
* an empty evidence section that means "git did not answer" (fabricated
  negative),
* a question reaching a model while a round is in flight (`ask_user` again),
* three entry points that "converge" only in the docstring,
* an abandoned exchange leaving something the loop trips over,
* a scope that arrives carrying its own permission slip.

Hermetic: no database, no network, no real model. `git` IS shelled out to,
because `path_suggest` is the evidence reader and stubbing it would make the
evidence tests prove nothing.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from autoloop import cli
from autoloop.contract import PROTOCOL_VERSION
from autoloop.inbox import (
    INTAKE_MARKER,
    MAX_QUESTIONS_PER_PASS,
    REQUIRED_QUESTIONS,
    DraftTask,
    Evidence,
    IntakeDraft,
    IntakeError,
    IntakeQuestion,
    TaskInbox,
    create_draft,
    create_draft_from_file,
    draft_blockers,
    draft_path,
    draft_specs,
    gather_suggestions,
    intake_dir_for,
    interview_step,
    load_declines,
    parse_draft,
    plan_step,
    record_decline,
    refuse_if_round_running,
    render_draft,
    repo_evidence,
    submit_draft,
    write_draft_text,
)


# ---- fixtures ---------------------------------------------------------------


def run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A real checkout, because `path_suggest` reads one with real git."""
    root = tmp_path / "repo"
    (root / "autoloop").mkdir(parents=True)
    (root / "docs").mkdir()
    run_git(root, "init", "-q", "-b", "work")
    run_git(root, "config", "user.email", "t@e.com")
    run_git(root, "config", "user.name", "T")
    (root / "autoloop" / "dashboard.py").write_text("def collect():\n    return {}\n")
    (root / "docs" / "AUTOLOOP.md").write_text("# loop\n")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-qm", "init")
    return root


@pytest.fixture()
def intake_dir(tmp_path: Path) -> Path:
    return tmp_path / "outside" / "intake"


class StubAsk:
    """A model that says exactly what a test tells it to, and records the ask.

    Deliberately a callable object rather than a lambda: several tests assert
    on the PROMPT (that it carries the evidence, that it was never called at
    all), and that is the half of the interview a stub can still get wrong.
    """

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else ""


def answer_required(path: Path, *, first: str = "the page renders a book",
                    second: str = "no infinite scroll") -> None:
    """Answer the two `?!` questions in place, the way an operator would."""
    text = path.read_text(encoding="utf-8")
    answers = [first, second]
    out = []
    for line in text.splitlines():
        if line.startswith("?!") and line.rstrip().endswith("->") and answers:
            line = f"{line} {answers.pop(0)}"
        out.append(line)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


#: The idea `ready_draft` starts from. It NAMES A TRACKED FILE on purpose:
#: `path_suggest` proposes a scope only from what the text points at, so an
#: idea naming nothing produces a draft with no `approved_paths` — a real and
#: ordinary state, pinned separately by
#: `test_a_ready_draft_whose_scope_could_not_be_proposed_says_so`. A helper
#: that hit it silently would make every submit test fail for a reason none of
#: them is about.
READY_IDEA = "Add a book reader page in autoloop/dashboard.py."


def ready_draft(intake_dir: Path, repo: Path, slug: str = "reader") -> Path:
    path = create_draft(intake_dir, slug, READY_IDEA)
    interview_step(path, ask=StubAsk("? does it remember your place?"), repo=repo)
    answer_required(path)
    interview_step(path, ask=StubAsk(""), repo=repo)
    return path


# ---- the file format --------------------------------------------------------


def test_a_draft_round_trips_through_parse_and_render():
    """Every pass re-renders the file. A round trip that lost an answer or
    reflowed one would silently eat the operator's words on the NEXT pass, and
    nothing downstream could reconstruct them."""
    draft = IntakeDraft(
        slug="demo",
        idea="Add a book reader page.\n\nIt should feel like a book.",
        questions=(
            IntakeQuestion(REQUIRED_QUESTIONS[0], "the page renders", required=True),
            IntakeQuestion(REQUIRED_QUESTIONS[1], "", required=True),
            IntakeQuestion("does it remember your place?", "yes\nacross devices"),
        ),
        evidence=(Evidence("book_service.py — named in the description", "git ls-files"),),
        assumptions=("does it paginate? — left blank",),
        tasks=(
            DraftTask(
                id="demo",
                title="Add a book reader page",
                priority=7,
                depends_on=("other",),
                approved_paths=("autoloop/dashboard.py  # named",),
                description="build the page\nand the tests",
            ),
        ),
    )
    assert parse_draft(render_draft(draft), "demo") == draft


def test_a_hand_mangled_file_keeps_every_answer(intake_dir, repo):
    """The operator will delete the arrow, answer on the next line, indent, and
    paste. Losing an answer is the one failure this parser must not have."""
    path = create_draft(intake_dir, "mangled", "Add a book reader page.")
    interview_step(path, ask=StubAsk("? does it paginate?"), repo=repo)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            f"?! {REQUIRED_QUESTIONS[0]} ->",
            f"?! {REQUIRED_QUESTIONS[0]} ->\n  a reader can open a book\n  and close it",
        ).replace(
            "? does it paginate? ->", "? does it paginate? -> yes -> by chapter"
        ),
        encoding="utf-8",
    )
    draft = parse_draft(path.read_text(encoding="utf-8"), "mangled")
    by_text = {q.text: q.answer for q in draft.questions}
    assert by_text[REQUIRED_QUESTIONS[0]] == "a reader can open a book\nand close it"
    # The FIRST arrow separates; an answer may contain more of them.
    assert by_text["does it paginate?"] == "yes -> by chapter"


def test_a_file_with_no_marker_is_read_as_all_idea(intake_dir):
    """A `.md` an operator wrote by hand has no marker in it. Reading anything
    but "this is all idea" would throw their text away."""
    draft = parse_draft("Add a book reader page.\nWith pages.", "x")
    assert draft.idea == "Add a book reader page.\nWith pages."
    assert draft.questions == ()
    assert INTAKE_MARKER in render_draft(draft), "and a render restores the marker"


# ---- three entry points, one path -------------------------------------------


@contextlib.contextmanager
def serving(repo, monkeypatch, intake_dir=None, inbox_dir=None):
    import autoloop.dashboard as dash

    if intake_dir is not None:
        monkeypatch.setattr(dash, "_intake_dir", lambda _repo: intake_dir)
    if inbox_dir is not None:
        monkeypatch.setattr(dash, "_inbox_dir", lambda _repo: inbox_dir)
    monkeypatch.setattr(dash.Handler, "repo", repo)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dash.Handler)
    srv.daemon_threads = True
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def post(base, path, payload):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Autoloop": "1"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as resp:
        return resp.status, json.loads(resp.read() or b"{}")


def test_three_entry_points_produce_a_byte_identical_draft(
    tmp_path, repo, intake_dir, monkeypatch
):
    """The falsifiable reading of "one path, not three implementations".

    Asserting that each route CALLS `create_draft` proves nothing — a second
    implementation could call it too and then decorate the result. Byte
    equality cannot be satisfied by two formats.
    """
    idea = "Add a book reader page."
    typed = create_draft(intake_dir, "typed", idea)

    dropped_source = tmp_path / "notes.md"
    dropped_source.write_text(idea, encoding="utf-8")
    dropped = create_draft_from_file(intake_dir, dropped_source, "dropped")

    with serving(repo, monkeypatch, intake_dir=intake_dir) as base:
        status, body = post(base, "/api/intake", {"id": "posted", "idea": idea})
    assert status == 200, body
    posted = intake_dir / "posted.md"

    def body_of(path: Path) -> str:
        # The slug is the only thing that legitimately differs, and it appears
        # only in the file NAME — never inside the bytes.
        return path.read_text(encoding="utf-8")

    assert body_of(typed) == body_of(dropped) == body_of(posted)


def test_a_dropped_file_must_look_like_an_idea(tmp_path, intake_dir):
    """`.md`/`.txt` only. Pointed at a `.py` this would adopt source code as an
    idea, and the operator would find out three passes later."""
    source = tmp_path / "service.py"
    source.write_text("print('hi')\n", encoding="utf-8")
    with pytest.raises(IntakeError, match=r"not an idea file"):
        create_draft_from_file(intake_dir, source)


def test_a_slug_cannot_address_a_file_outside_the_intake_directory(intake_dir):
    """The slug arrives from an HTTP body on the dashboard route."""
    for bad in ("../escape", "/etc/passwd", "a/b", ".hidden", "", "Upper"):
        with pytest.raises(IntakeError):
            draft_path(intake_dir, bad)


def test_starting_a_draft_twice_refuses_instead_of_clobbering(intake_dir):
    create_draft(intake_dir, "reader", "Add a book reader page.")
    before = (intake_dir / "reader.md").read_text(encoding="utf-8")
    with pytest.raises(IntakeError, match=r"already exists"):
        create_draft(intake_dir, "reader", "something else entirely")
    assert (intake_dir / "reader.md").read_text(encoding="utf-8") == before


def test_an_empty_idea_creates_nothing(intake_dir):
    with pytest.raises(IntakeError):
        create_draft(intake_dir, "blank", "   \n\n ")
    assert not (intake_dir / "blank.md").exists()


# ---- the draft-emission gate (the fail-open one) ----------------------------


def test_a_silent_provider_never_makes_a_draft_look_ready(intake_dir, repo):
    """THE fail-open case. If "nothing left to ask" meant "the model returned
    no questions", a provider that is down, throttled or terse would declare
    the interview finished and emit a draft nobody answered."""
    path = create_draft(intake_dir, "reader", "Add a book reader page.")
    result = interview_step(path, ask=StubAsk(""), repo=repo)

    assert not result.ready
    assert result.blockers, "silence is not evidence of completeness"
    assert "NOT a finished interview" in result.provider_note
    assert "## Draft" not in path.read_text(encoding="utf-8")
    with pytest.raises(IntakeError):
        draft_specs(parse_draft(path.read_text(encoding="utf-8"), "reader"))


def test_a_failing_provider_still_asks_what_only_the_operator_can_answer(
    intake_dir, repo
):
    """A transport fault degrades the pass and cannot advance the draft. The
    two questions that gate readiness are constants here for exactly this
    reason: an unreachable model cannot fabricate either of them."""

    def boom(_prompt):
        raise IntakeError("codex_cli did not answer: timed out")

    path = create_draft(intake_dir, "reader", "Add a book reader page.")
    result = interview_step(path, ask=boom, repo=repo)

    assert tuple(result.added_questions) == REQUIRED_QUESTIONS
    assert "could not reach the model" in result.provider_note
    assert not result.ready


def test_deleting_a_required_question_blocks_rather_than_clears(intake_dir, repo):
    """Readiness is POSITIVE evidence, never the absence of a question. An
    operator (or a bad edit) removing the `?!` line must not thereby satisfy
    the gate."""
    path = ready_draft(intake_dir, repo)
    assert draft_blockers(parse_draft(path.read_text(encoding="utf-8"), "reader")) == ()

    stripped = "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.startswith(f"?! {REQUIRED_QUESTIONS[0]}")
    )
    blockers = draft_blockers(parse_draft(stripped, "reader"))
    assert blockers and "not in the file" in blockers[0]


def test_a_blank_required_answer_blocks_and_a_blank_optional_one_is_assumed(
    intake_dir, repo
):
    """The two classes of blank are different, and collapsing them is how the
    provable claim gets fabricated."""
    path = create_draft(intake_dir, "reader", "Add a book reader page.")
    interview_step(path, ask=StubAsk("? does it remember your place?"), repo=repo)

    blocked = interview_step(path, ask=StubAsk(""), repo=repo)
    assert not blocked.ready
    assert any("only you can answer" in b for b in blocked.blockers)

    answer_required(path)
    done = interview_step(path, ask=StubAsk(""), repo=repo)
    assert done.ready
    assert any("does it remember your place?" in a for a in done.assumptions)
    assert "## Assumptions" in path.read_text(encoding="utf-8"), (
        "proceeding on a blank has to SAY what it assumed, in the artifact"
    )


# ---- echo -------------------------------------------------------------------


def test_a_provider_that_hands_back_its_own_prompt_adds_nothing(intake_dir, repo):
    """The prompt carries the idea, the open questions and the evidence, so a
    model with nothing to add will return some of it. Reading that as output is
    an interview that never converges, and an evidence line promoted to a
    second, uncited claim."""
    path = create_draft(
        intake_dir, "reader", "Add a book reader page in autoloop/dashboard.py."
    )
    first = interview_step(path, ask=StubAsk("? does it remember your place?"), repo=repo)
    assert "does it remember your place?" in first.added_questions
    assert first.added_evidence, "the prompt will carry this; the reply must not"

    echo = "\n".join(
        [
            "? does it remember your place?",
            "? Does it remember your place???",
            f"? {REQUIRED_QUESTIONS[0]}",
            "? Add a book reader page in autoloop/dashboard.py.",
            f"? {first.added_evidence[0]}",
        ]
    )
    second = interview_step(path, ask=StubAsk(echo), repo=repo)
    assert second.added_questions == ()
    assert "echoes" in second.provider_note


def test_a_reply_contributes_questions_and_never_evidence(intake_dir, repo):
    """Evidence is what the system READ, with the source named. A model saying
    a file exists is not a reading of that file."""
    path = create_draft(
        intake_dir, "reader", "Add a book reader page in autoloop/dashboard.py."
    )
    interview_step(
        path,
        ask=StubAsk("? does it paginate?\nautoloop/nonexistent.py holds the reader"),
        repo=repo,
    )
    draft = parse_draft(path.read_text(encoding="utf-8"), "reader")
    assert draft.evidence, "there is real evidence to compare against"
    assert all(item.source == "git ls-files" for item in draft.evidence)
    assert not any("nonexistent" in item.text for item in draft.evidence)


def test_questions_are_batched_three_to_five(intake_dir, repo):
    """Dripping one per pass turns an afternoon into a week; twenty at once is
    not answered at all."""
    path = create_draft(intake_dir, "reader", "Add a book reader page.")
    reply = "\n".join(f"? question number {i}?" for i in range(20))
    result = interview_step(path, ask=StubAsk(reply), repo=repo)
    assert len(result.added_questions) == MAX_QUESTIONS_PER_PASS
    assert tuple(result.added_questions[:2]) == REQUIRED_QUESTIONS


# ---- evidence ---------------------------------------------------------------


def test_evidence_says_when_it_read_nothing_at_all(tmp_path):
    """`path_suggest.tracked_files` returns `[]` for a git that errored, a git
    that is missing and a directory that is not a checkout. An empty Evidence
    section rendered from that reads as "nothing relevant exists", which is a
    fabricated negative."""
    not_a_checkout = tmp_path / "plain"
    not_a_checkout.mkdir()
    found, note = repo_evidence(not_a_checkout, "Add a book reader page.")
    assert found == ()
    assert "NOTHING WAS READ" in note


def test_evidence_distinguishes_no_matches_from_no_read(repo):
    found, note = repo_evidence(repo, "something entirely unrelated to this repo")
    assert found == ()
    assert "NOTHING WAS READ" not in note
    assert "tracked files" in note and "none was named" in note


def test_every_evidence_line_names_a_source(intake_dir, repo):
    path = create_draft(intake_dir, "reader", "Change autoloop/dashboard.py.")
    interview_step(path, ask=StubAsk(""), repo=repo)
    draft = parse_draft(path.read_text(encoding="utf-8"), "reader")
    assert draft.evidence
    assert all(item.source for item in draft.evidence)
    assert any("autoloop/dashboard.py" in item.text for item in draft.evidence)


# ---- the authoring-time guard ----------------------------------------------


def write_lock(state_dir: Path, *, pid: int, corrupt: bool = False) -> None:
    from autoloop.lock import LOCK_FILENAME

    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / LOCK_FILENAME
    if corrupt:
        path.write_text("{not json", encoding="utf-8")
        return
    import socket
    from datetime import datetime, timezone

    path.write_text(
        json.dumps(
            {
                "pid": pid,
                "hostname": socket.gethostname(),
                "started_at": datetime.now(timezone.utc).isoformat(),
                "run_id": "r1",
                "state_dir": str(state_dir),
            }
        ),
        encoding="utf-8",
    )


def test_asking_is_refused_while_a_round_is_running(tmp_path):
    import os

    state_dir = tmp_path / "state"
    write_lock(state_dir, pid=os.getpid())
    with pytest.raises(IntakeError, match=r"ask_user"):
        refuse_if_round_running(state_dir, "the intake interview")


def test_the_guard_fails_closed_on_a_lock_it_cannot_read(tmp_path):
    """A corrupt lock means "cannot determine". `LoopLock.is_live` answers that
    permissively because it is answering a different question — may I TAKE this
    lock, where the permissive answer is recoverable. Here the permissive
    answer is a question reaching a model mid-round."""
    state_dir = tmp_path / "state"
    write_lock(state_dir, pid=0, corrupt=True)
    with pytest.raises(IntakeError, match=r"corrupt"):
        refuse_if_round_running(state_dir, "the intake interview")


def test_the_guard_allows_authoring_when_no_loop_holds_the_lock(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    refuse_if_round_running(state_dir, "the intake interview")  # no raise


def test_writing_and_submitting_stay_safe_mid_round(tmp_path, repo, intake_dir):
    """Deliberately NOT guarded. `add-task` is documented safe at any moment
    because it touches nothing inside the checkout, and intake's file writes
    and its submit have exactly that property — taking it away would be a
    regression dressed as caution."""
    import os

    state_dir = tmp_path / "state"
    write_lock(state_dir, pid=os.getpid())
    path = ready_draft(intake_dir, repo)
    write_draft_text(intake_dir, "reader", path.read_text(encoding="utf-8"))
    inbox = TaskInbox(tmp_path / "outside" / "inbox")
    assert submit_draft(path, inbox), "submitting mid-round is allowed"


# ---- nothing reaches the registry without a submit --------------------------


def test_the_exchange_queues_nothing_until_submit(intake_dir, repo, tmp_path):
    inbox_dir = tmp_path / "outside" / "inbox"
    inbox = TaskInbox(inbox_dir)
    path = create_draft(intake_dir, "reader", READY_IDEA)
    interview_step(path, ask=StubAsk("? does it paginate?"), repo=repo)
    answer_required(path)
    interview_step(path, ask=StubAsk(""), repo=repo)
    assert inbox.pending() == [], "the whole exchange files nothing"

    filed = submit_draft(path, inbox)
    assert [task_id for task_id, _ in filed] == ["reader"]
    queued = json.loads(inbox.pending()[0].read_text(encoding="utf-8"))
    assert queued["kind"] == "task"
    assert queued["id"] == "reader"
    assert "the page renders a book" in queued["description"], (
        "the artifact IS the description — no translation step"
    )


def test_a_draft_with_no_authorized_scope_is_refused_at_submit(intake_dir, repo):
    """`TaskRegistry.add_many` accepts an empty scope and the orchestrator then
    refuses to dispatch the task forever, so filing one is a trap. Refused in
    the same terms `dashboard._submit_task` refuses it."""
    path = ready_draft(intake_dir, repo)
    text = path.read_text(encoding="utf-8")
    head, _, _ = text.partition("approved_paths:")
    path.write_text(head + "approved_paths:\n", encoding="utf-8")
    with pytest.raises(IntakeError, match=r"no approved paths"):
        draft_specs(parse_draft(path.read_text(encoding="utf-8"), "reader"))


def test_suggested_paths_are_mechanical_and_carry_their_reason(intake_dir, repo):
    """`path_suggest`'s line exactly: a suggestion is not an authorization. The
    field is FILLED; submitting it is the confirmation."""
    path = create_draft(intake_dir, "reader", "Change autoloop/dashboard.py.")
    interview_step(path, ask=StubAsk(""), repo=repo)
    answer_required(path)
    interview_step(path, ask=StubAsk(""), repo=repo)
    draft = parse_draft(path.read_text(encoding="utf-8"), "reader")
    assert draft.tasks[0].approved_paths
    assert all("  # " in entry for entry in draft.tasks[0].approved_paths), (
        "each proposed path carries the reason it was proposed, in the same "
        "`path  # reason` shape the dashboard's Detect-paths button writes"
    )
    spec = draft_specs(draft)[0]
    assert "autoloop/dashboard.py" in spec["approved_paths"], (
        "the reason is stripped again; a path is what the registry validates"
    )


def test_a_ready_draft_whose_scope_could_not_be_proposed_says_so(intake_dir, repo):
    """The ordinary case for an idea that names no file. The draft is still
    emitted — the operator needs somewhere to type the paths — but the empty
    list must not read as "nothing needed", and submitting it is refused."""
    path = create_draft(intake_dir, "vague", "Make the whole thing nicer.")
    interview_step(path, ask=StubAsk(""), repo=repo)
    answer_required(path)
    result = interview_step(path, ask=StubAsk(""), repo=repo)

    assert result.ready, "readiness is about the ANSWERS, not about the scope"
    text = path.read_text(encoding="utf-8")
    assert "## Draft" in text
    assert "nothing was detected" in text
    draft = parse_draft(text, "vague")
    assert draft.tasks[0].approved_paths == ()
    with pytest.raises(IntakeError, match=r"no approved paths"):
        draft_specs(draft)


def test_a_split_queues_all_or_nothing(intake_dir, repo, tmp_path):
    """Half a split is worse than none: the operator would have to work out
    which half landed."""
    path = ready_draft(intake_dir, repo)
    text = path.read_text(encoding="utf-8")
    text += "\n### task: \ntitle: nameless\napproved_paths:\n  - autoloop/dashboard.py\n"
    path.write_text(text, encoding="utf-8")
    inbox = TaskInbox(tmp_path / "outside" / "inbox")
    with pytest.raises(Exception):
        submit_draft(path, inbox)
    assert inbox.pending() == [], "the good half must not land alone"


# ---- an abandoned exchange leaves nothing behind ----------------------------


def test_a_drain_ignores_everything_intake_writes(tmp_path, repo, intake_dir):
    """The intake directory is a SIBLING of the inbox, never inside it.
    `TaskInbox.drain` globs `*.json` in its own directory and MOVES anything it
    cannot parse into `rejected/` — which would eat the decline ledger and
    report a spurious problem line for it."""
    workers_root = tmp_path / "outside" / "workers"
    inbox_dir = tmp_path / "outside" / "inbox"
    assert intake_dir_for(workers_root, tmp_path) != inbox_dir
    assert intake_dir_for(workers_root, tmp_path).parent == inbox_dir.parent

    real_intake = intake_dir_for(workers_root, tmp_path)
    create_draft(real_intake, "reader", "Add a book reader page.")
    record_decline(real_intake, "audit_finding:x:y", "abc")

    inbox = TaskInbox(inbox_dir)
    inbox.directory.mkdir(parents=True, exist_ok=True)
    specs, problems = inbox.drain()
    assert (specs, problems) == ([], [])
    assert (real_intake / "declined.json").exists()


def test_abandoning_a_draft_is_deleting_one_file(intake_dir, repo):
    """No task, no blocker, no registry row, no state — one file, and the
    operator can remove it."""
    path = create_draft(intake_dir, "reader", "Add a book reader page.")
    interview_step(path, ask=StubAsk("? does it paginate?"), repo=repo)
    assert sorted(p.name for p in intake_dir.iterdir()) == ["reader.md"]
    path.unlink()
    assert list(intake_dir.iterdir()) == []


# ---- phase 1: suggestions ---------------------------------------------------


def audit_report(repo: Path, name: str, findings: list[tuple[str, str]]) -> None:
    body = "# Repository audit\n\n" + "".join(
        f"#### {qid} — {headline}\n- severity **low**\n" for qid, headline in findings
    )
    (repo / "docs" / name).write_text(body, encoding="utf-8")


def test_every_suggestion_cites_the_artifact_it_came_from(repo, tmp_path, intake_dir):
    """A suggestion that cannot name a file, a finding id, a measurement or a
    task id must not be offered — a system that suggests work will keep
    suggesting work whether or not any is needed."""
    audit_report(repo, "AUDIT_2026-08-05.md", [("db:db-01", "Fix the baseline")])
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text(
        json.dumps({"tasks": [{"id": "t1", "title": "Do a thing", "status": "pending"}]}),
        encoding="utf-8",
    )
    blockers_dir = tmp_path / "blockers"
    blockers_dir.mkdir()
    (blockers_dir / "blk-t1-001.json").write_text(
        json.dumps({"id": "blk-t1-001", "code": "wedged", "question": "what now?"}),
        encoding="utf-8",
    )

    offer = gather_suggestions(
        repo,
        report_glob="docs/AUDIT_*.md",
        tasks_file=tasks_file,
        blockers_dir=blockers_dir,
        intake_dir=intake_dir,
    )
    assert len(offer.suggestions) == 3, "two or three, never a list"
    assert {s.source for s in offer.suggestions} == {
        "audit_finding", "ready_task", "open_blocker"
    }
    for item in offer.suggestions:
        assert item.cite and item.fingerprint and item.headline


def test_an_actioned_finding_is_not_offered(repo, tmp_path, intake_dir):
    audit_report(repo, "AUDIT_2026-08-05.md", [("db:db-01", "Fix the baseline")])
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text(
        json.dumps({"tasks": [{"id": "au-1", "description": "closes db:db-01",
                               "status": "completed"}]}),
        encoding="utf-8",
    )
    offer = gather_suggestions(
        repo, report_glob="docs/AUDIT_*.md", tasks_file=tasks_file,
        blockers_dir=tmp_path / "none", intake_dir=intake_dir,
    )
    assert [s for s in offer.suggestions if s.source == "audit_finding"] == []


def test_an_absent_source_is_reported_not_silently_empty(repo, tmp_path, intake_dir):
    """An empty offer with no explanation is indistinguishable from "there is
    nothing to do", and one of those two is a lie."""
    offer = gather_suggestions(
        repo,
        report_glob="docs/AUDIT_*.md",
        tasks_file=tmp_path / "missing.json",
        blockers_dir=tmp_path / "missing",
        intake_dir=intake_dir,
    )
    assert offer.suggestions == ()
    joined = " ".join(offer.sources)
    assert "NOT READ" in joined
    assert "not the same as none existing" in joined


def test_declining_is_free_and_sticks_until_the_evidence_changes(
    repo, tmp_path, intake_dir
):
    audit_report(repo, "AUDIT_2026-08-05.md", [("db:db-01", "Fix the baseline")])
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text(json.dumps({"tasks": []}), encoding="utf-8")
    kwargs = dict(
        report_glob="docs/AUDIT_*.md", tasks_file=tasks_file,
        blockers_dir=tmp_path / "none", intake_dir=intake_dir,
    )
    first = gather_suggestions(repo, **kwargs).suggestions[0]
    record_decline(intake_dir, first.key, first.fingerprint)
    assert load_declines(intake_dir)[first.key] == first.fingerprint

    again = gather_suggestions(repo, **kwargs)
    assert again.suggestions == ()
    assert again.declined == 1

    audit_report(repo, "AUDIT_2026-08-05.md", [("db:db-01", "Fix the baseline, again")])
    fresh = gather_suggestions(repo, **kwargs)
    assert [s.key for s in fresh.suggestions] == [first.key], (
        "new evidence re-opens the offer; unchanged evidence never does"
    )


# ---- phase 3: one level, through the existing verb --------------------------


PLAN_REPLY = json.dumps(
    {
        "version": PROTOCOL_VERSION,
        "decision": "plan",
        "reason": "two halves",
        "tasks": [
            {"id": "reader-a", "title": "The page", "description": "autoloop/dashboard.py",
             "approved_paths": ["/etc/passwd", "everything/"]},
            {"id": "reader-b", "title": "The tests", "description": "autoloop/dashboard.py"},
        ],
    }
)


def test_planning_produces_one_level_and_takes_its_scope_from_the_repo(
    intake_dir, repo
):
    """The plan reply's own `approved_paths` are DISCARDED. A task that arrived
    carrying its own permission slip is the circularity `path_suggest`'s
    docstring and `docs/SECURITY.md` #2 both exist to prevent."""
    path = ready_draft(intake_dir, repo)
    result = plan_step(path, ask=StubAsk(PLAN_REPLY), repo=repo)

    assert list(result.tasks) == ["reader-a", "reader-b"]
    draft = parse_draft(path.read_text(encoding="utf-8"), "reader")
    every_path = [p for t in draft.tasks for p in t.approved_paths]
    assert every_path, "scope is proposed, mechanically"
    assert not any("/etc/passwd" in p or "everything/" in p for p in every_path)


def test_planning_refuses_a_second_level(intake_dir, repo):
    """Lazy, not eager. A task that turns out to be two gets split when it says
    so — by a reviewer with that task's evidence — not pre-split into four that
    were never needed."""
    path = ready_draft(intake_dir, repo)
    plan_step(path, ask=StubAsk(PLAN_REPLY), repo=repo)
    with pytest.raises(IntakeError, match=r"second level"):
        plan_step(path, ask=StubAsk(PLAN_REPLY), repo=repo)


def test_planning_refuses_an_unready_draft_and_writes_nothing(intake_dir, repo):
    path = create_draft(intake_dir, "reader", "Add a book reader page.")
    before = path.read_text(encoding="utf-8")
    asker = StubAsk(PLAN_REPLY)
    with pytest.raises(IntakeError, match=r"not ready"):
        plan_step(path, ask=asker, repo=repo)
    assert asker.prompts == [], "no model is asked about a draft that is not ready"
    assert path.read_text(encoding="utf-8") == before


def test_a_reply_that_is_not_a_plan_changes_nothing(intake_dir, repo):
    path = ready_draft(intake_dir, repo)
    before = path.read_text(encoding="utf-8")
    reply = json.dumps({"decision": "stop", "reason": "no"})
    with pytest.raises(IntakeError):
        plan_step(path, ask=StubAsk(reply), repo=repo)
    assert path.read_text(encoding="utf-8") == before


# ---- the dashboard entry point ---------------------------------------------


def test_the_dashboard_reads_and_writes_the_same_file_the_cli_does(
    tmp_path, repo, intake_dir, monkeypatch
):
    inbox_dir = tmp_path / "outside" / "inbox"
    path = ready_draft(intake_dir, repo)
    with serving(repo, monkeypatch, intake_dir=intake_dir, inbox_dir=inbox_dir) as base:
        status, view = get(base, "/api/intake?id=reader")
        assert status == 200 and view["ready"] is True, view
        assert view["text"] == path.read_text(encoding="utf-8")

        edited = view["text"].replace("Add a book reader page in", "Add a READER page in")
        assert post(base, "/api/intake/edit", {"id": "reader", "text": edited})[0] == 200
        assert path.read_text(encoding="utf-8") == edited, "verbatim, not tidied"

        assert TaskInbox(inbox_dir).pending() == []
        status, body = post(base, "/api/intake/submit", {"id": "reader"})
        assert status == 200, body
        assert body["queued"] == ["reader"]
    queued = json.loads(TaskInbox(inbox_dir).pending()[0].read_text(encoding="utf-8"))
    assert "Add a READER page in" in queued["description"]


def test_the_dashboard_refuses_a_slug_that_escapes_and_an_unready_submit(
    tmp_path, repo, intake_dir, monkeypatch
):
    inbox_dir = tmp_path / "outside" / "inbox"
    create_draft(intake_dir, "raw", "Add a book reader page.")
    with serving(repo, monkeypatch, intake_dir=intake_dir, inbox_dir=inbox_dir) as base:
        escape, _ = post(base, "/api/intake", {"id": "../evil", "idea": "x"})
        unready, body = post(base, "/api/intake/submit", {"id": "raw"})
    assert escape == 400
    assert unready == 400 and "not finished" in body["error"]
    assert TaskInbox(inbox_dir).pending() == []
    assert not (intake_dir.parent / "evil.md").exists()


def test_the_dashboard_ask_route_refuses_mid_round(tmp_path, monkeypatch):
    """`/api/intake/ask` is the one intake route that talks to a model, so it
    is the one that must refuse while a round is live."""
    import os

    import autoloop.dashboard as dash

    repo = tmp_path / "repo"
    (repo / ".autoloop").mkdir(parents=True)
    state_dir = tmp_path / "state"
    workers_root = tmp_path / "outside" / "workers"
    (repo / ".autoloop" / "config.toml").write_text(
        "[conversation]\n"
        'provider = "codex_cli"\n'
        "[paths]\n"
        f'state_dir = "{state_dir}"\n'
        f'workers_root = "{workers_root}"\n',
        encoding="utf-8",
    )
    intake_dir = workers_root.parent / "intake"
    create_draft(intake_dir, "reader", "Add a book reader page.")
    write_lock(state_dir, pid=os.getpid())

    monkeypatch.setattr(dash.Handler, "repo", repo)
    with serving(repo, monkeypatch) as base:
        status, body = post(base, "/api/intake/ask", {"id": "reader"})
    assert status == 400
    assert "ask_user" in body["error"]


# ---- the CLI entry point ----------------------------------------------------


def write_config(tmp_path: Path, state_dir: Path, workers_root: Path) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(
        "[conversation]\n"
        'provider = "codex_cli"\n'
        "[paths]\n"
        f'state_dir = "{state_dir}"\n'
        f'workers_root = "{workers_root}"\n',
        encoding="utf-8",
    )
    return config


def test_the_cli_walks_idea_to_queued_task_and_only_submit_queues(
    tmp_path, repo, monkeypatch, capsys
):
    state_dir = tmp_path / "state"
    workers_root = tmp_path / "outside" / "workers"
    config = write_config(tmp_path, state_dir, workers_root)
    inbox_dir = workers_root.parent / "inbox"
    intake_dir = workers_root.parent / "intake"
    monkeypatch.chdir(repo)

    assert cli.main([
        "intake", "new", "--config", str(config), "--id", "reader",
        "--text", "Add a book reader page touching autoloop/dashboard.py.",
    ]) == 0
    assert cli.main([
        "intake", "ask", "--config", str(config), "--id", "reader", "--no-model",
    ]) == 0
    assert TaskInbox(inbox_dir).pending() == []

    # Not ready: submitting must refuse, and refuse loudly.
    assert cli.main([
        "intake", "submit", "--config", str(config), "--id", "reader",
    ]) == 1
    assert TaskInbox(inbox_dir).pending() == []

    answer_required(intake_dir / "reader.md")
    assert cli.main([
        "intake", "ask", "--config", str(config), "--id", "reader", "--no-model",
    ]) == 0
    capsys.readouterr()
    assert cli.main([
        "intake", "submit", "--config", str(config), "--id", "reader", "--dry-run",
    ]) == 0
    assert "dry run" in capsys.readouterr().out
    assert TaskInbox(inbox_dir).pending() == []

    assert cli.main([
        "intake", "submit", "--config", str(config), "--id", "reader",
    ]) == 0
    assert len(TaskInbox(inbox_dir).pending()) == 1


def test_the_cli_ask_refuses_while_a_round_is_running(tmp_path, repo, monkeypatch):
    import os

    state_dir = tmp_path / "state"
    workers_root = tmp_path / "outside" / "workers"
    config = write_config(tmp_path, state_dir, workers_root)
    create_draft(workers_root.parent / "intake", "reader", "Add a book reader page.")
    write_lock(state_dir, pid=os.getpid())
    monkeypatch.chdir(repo)

    assert cli.main([
        "intake", "ask", "--config", str(config), "--id", "reader", "--no-model",
    ]) == 1


def test_intake_is_reachable_from_the_parser_and_says_what_it_is():
    help_text = cli.build_parser().format_help()
    assert "intake" in help_text
    args = cli.build_parser().parse_args(["intake", "list"])
    assert args.intake_cmd == "list"
    assert args.func is cli._cmd_intake
