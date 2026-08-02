"""Suggesting a scope, without ever authorizing one.

`approved_paths` is what a write-capable agent may write, and
`docs/SECURITY.md` finding #2 exists because the executor's own report must
never define its own scope. So the tests that matter most here are the ones
proving this only ever PROPOSES: the endpoint queues nothing, and a suggestion
the operator does not submit has authorized nothing.

The rest is false-positive control. A detector that offers a plausible wrong
file is worse than one that offers nothing, because it looks considered.
"""

import contextlib
import json
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from autoloop.path_suggest import MAX_SUGGESTIONS, suggest


@pytest.fixture
def repo(tmp_path):
    """A small real git repo — `suggest` shells out to git, so a fake tree
    would exercise none of the code that matters."""
    root = tmp_path / "repo"
    (root / "autoloop" / "tests").mkdir(parents=True)
    (root / "lexy-app" / "backend" / "routers").mkdir(parents=True)
    (root / "docs").mkdir()

    (root / "autoloop" / "validation.py").write_text(
        "def run_validation_commands(commands):\n    return True\n", encoding="utf-8")
    (root / "autoloop" / "tasks.py").write_text(
        "class TaskRegistry:\n    pass\n\n\ndef report(x):\n    return x\n", encoding="utf-8")
    (root / "autoloop" / "tests" / "test_tasks.py").write_text("# t\n", encoding="utf-8")
    (root / "lexy-app" / "backend" / "routers" / "notifications.py").write_text(
        "# router\n", encoding="utf-8")
    (root / "docs" / "SUMMARY.md").write_text("# s\n", encoding="utf-8")
    # An ambiguous basename: two files share it.
    (root / "autoloop" / "models.py").write_text("# a\n", encoding="utf-8")
    (root / "lexy-app" / "backend" / "models.py").write_text("# b\n", encoding="utf-8")

    for argv in (("init", "-q", "-b", "main"), ("config", "user.email", "t@e.com"),
                 ("config", "user.name", "T"), ("config", "commit.gpgsign", "false"),
                 ("add", "-A"), ("commit", "-qm", "base")):
        subprocess.run(["git", *argv], cwd=root, check=True, capture_output=True)
    return root


def paths(found):
    return [s.path for s in found]


# --- what it should find ------------------------------------------------------


def test_an_explicit_path_is_found(repo):
    found = suggest("Fix autoloop/validation.py so it reports the failing test.", repo)
    assert "autoloop/validation.py" in paths(found)
    assert found[0].reason == "named in the description"


def test_a_directory_gets_its_trailing_slash(repo):
    """The slash is what makes it a prefix rather than an exact file, so the
    detector adds it instead of leaving the operator to remember."""
    found = suggest("Include autoloop/tests in the default test surface.", repo)
    assert "autoloop/tests/" in paths(found)


def test_a_bare_filename_that_resolves_uniquely(repo):
    found = suggest("Extract SQL from notifications.py into a service.", repo)
    assert "lexy-app/backend/routers/notifications.py" in paths(found)


def test_an_identifier_resolves_to_where_it_is_DEFINED(repo):
    """A definition, not a mention: the defining file is what a task about
    that symbol will edit."""
    found = suggest("Make run_validation_commands name the failing test.", repo)
    assert paths(found) == ["autoloop/validation.py"]
    assert "defines run_validation_commands" in found[0].reason


def test_camel_case_identifiers_too(repo):
    found = suggest("TaskRegistry should refuse a duplicate id.", repo)
    assert paths(found) == ["autoloop/tasks.py"]


def test_a_file_the_task_will_CREATE_is_offered(repo):
    """Its parent directory exists, so the path is plausible — and a task that
    only adds a file would otherwise get no suggestion at all."""
    found = suggest("Add autoloop/tests/test_new_thing.py covering the gap.", repo)
    assert "autoloop/tests/test_new_thing.py" in paths(found)
    assert "new file" in [s.reason for s in found if s.path.endswith("test_new_thing.py")][0]


# --- what it must NOT find (the trust surface) --------------------------------


def test_prose_that_collides_with_a_function_name_is_ignored(repo):
    """`report` is defined in tasks.py AND is an ordinary English word. The
    first version matched it and produced one confident, wrong suggestion —
    the shape rule (snake_case or CamelCase only) is what stops it, rather
    than a blocklist that would need a new entry per collision."""
    found = suggest("Make the loop report progress faster and improve things.", repo)
    assert paths(found) == []


def test_an_ambiguous_basename_is_dropped_not_guessed(repo):
    """Two files named models.py. Offering one would look considered and be
    wrong half the time."""
    found = suggest("Update models.py to add the new column.", repo)
    assert paths(found) == []


def test_a_path_that_does_not_exist_is_not_invented(repo):
    found = suggest("Rewrite frobnicator/nonexistent.py entirely.", repo)
    assert paths(found) == []


def test_the_list_is_bounded(repo):
    """A form pre-filled with forty paths is accepted rather than read, which
    would defeat the confirmation this exists to preserve."""
    text = " ".join(["autoloop/validation.py autoloop/tasks.py docs/SUMMARY.md"] * 40)
    assert len(suggest(text, repo)) <= MAX_SUGGESTIONS


def test_scanning_leaves_the_repo_byte_identical(repo):
    """It runs against a checkout the loop may be mid-round in. A plain git
    read can rewrite .git/index, and a dirty checkout makes the escape
    detector refuse the next write-capable task."""
    def snapshot():
        return {p: p.stat().st_mtime_ns for p in sorted(repo.rglob("*")) if p.is_file()}

    before = snapshot()
    suggest("Fix autoloop/validation.py and run_validation_commands.", repo)
    assert snapshot() == before


# --- the endpoint proposes; it never authorizes -------------------------------


@contextlib.contextmanager
def serving(repo_path, inbox_dir, monkeypatch):
    import autoloop.dashboard as dash

    monkeypatch.setattr(dash, "_inbox_dir", lambda _r: inbox_dir)
    monkeypatch.setattr(dash.Handler, "repo", repo_path)
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


def post(base, path, payload, headers=None):
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 **({"X-Autoloop": "1"} if headers is None else headers)},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_suggesting_queues_nothing(repo, tmp_path, monkeypatch):
    """THE property. A suggestion is not an authorization: the operator's
    submit is still the only thing that can queue a scope."""
    from autoloop.inbox import TaskInbox

    inbox_dir = tmp_path / "outside" / "inbox"
    with serving(repo, inbox_dir, monkeypatch) as base:
        status, body = post(base, "/api/suggest-paths", {
            "title": "Fix validation", "description": "run_validation_commands is quiet",
        })

    assert status == 200
    assert body["suggestions"][0]["path"] == "autoloop/validation.py"
    assert TaskInbox(inbox_dir).pending() == [], "suggesting must queue nothing"


def test_every_suggestion_carries_its_reason(repo, tmp_path, monkeypatch):
    """The reason is what makes the list checkable rather than trusted."""
    with serving(repo, tmp_path / "inbox", monkeypatch) as base:
        _, body = post(base, "/api/suggest-paths",
                       {"title": "t", "description": "fix autoloop/validation.py"})
    assert all(s.get("reason") for s in body["suggestions"])


def test_the_endpoint_keeps_the_same_guards(repo, tmp_path, monkeypatch):
    with serving(repo, tmp_path / "inbox", monkeypatch) as base:
        no_header, _ = post(base, "/api/suggest-paths", {"title": "t"}, headers={})
        cross, _ = post(base, "/api/suggest-paths", {"title": "t"},
                        headers={"X-Autoloop": "1", "Origin": "http://evil.example"})
        empty, _ = post(base, "/api/suggest-paths", {"title": "", "description": ""})
    assert (no_header, cross, empty) == (403, 403, 400)


def test_the_page_strips_the_reason_comments_before_submitting():
    """The detector appends '  # reason' per line for the operator to read.
    A path is what the registry validates, so those must not be sent."""
    from autoloop.dashboard import PAGE

    assert 's.split("#")[0].trim()' in PAGE
    assert 'id="ntdetect"' in PAGE
