"""Persistent state: round-trips, atomic writes, corruption handling,
archive/reset."""

import json

import pytest

from autoloop.errors import StateCorruptError, StateError
from autoloop.state import LastResponse, LoopState, PendingRequest, Phase, StateStore


def make_state() -> LoopState:
    state = LoopState.new("https://chatgpt.com/c/abc")
    state.phase = Phase.AWAITING.value
    state.iteration = 7
    state.parse_retries = 1
    state.pending_request = PendingRequest(
        request_id="alr-x-0007",
        payload="hello",
        submitted=True,
        prompt="full prompt text",
        head_sha="a" * 40,
        base_sha="b" * 40,
        report_sha256="c" * 64,
        timestamp="2026-07-29T00:00:00+00:00",
    )
    state.last_response = LastResponse(
        request_id="alr-x-0006",
        raw="raw",
        received_at="t",
        head_sha="a" * 40,
        base_sha="b" * 40,
        report_sha256="c" * 64,
    )
    state.current_task = {"task_id": "t1", "title": "Task one", "decision": "implement"}
    state.reviewed_commit = "a" * 40
    state.last_decision = "implement"
    state.last_validation = "ruff clean"
    return state


def test_load_missing_returns_none(tmp_path):
    assert StateStore(tmp_path / "state.json").load() is None


def test_roundtrip(tmp_path):
    store = StateStore(tmp_path / "state.json")
    state = make_state()
    store.save(state)
    loaded = store.load()
    assert loaded is not None
    assert loaded.to_dict() == state.to_dict()
    assert loaded.pending_request.request_id == "alr-x-0007"
    assert loaded.last_response.raw == "raw"


def test_save_is_atomic_no_tmp_left_behind(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.save(make_state())
    leftovers = [p.name for p in tmp_path.iterdir()]
    assert leftovers == ["state.json"]


def test_save_updates_updated_at(tmp_path):
    store = StateStore(tmp_path / "state.json")
    state = make_state()
    state.updated_at = "1970-01-01T00:00:00+00:00"
    store.save(state)
    assert state.updated_at != "1970-01-01T00:00:00+00:00"


def test_corrupt_file_raises_and_is_kept(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(StateCorruptError):
        StateStore(path).load()
    assert path.read_text(encoding="utf-8") == "{ this is not json"


def test_non_object_json_raises(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(StateCorruptError):
        StateStore(path).load()


def test_schema_version_mismatch_raises(tmp_path):
    path = tmp_path / "state.json"
    data = make_state().to_dict()
    data["schema_version"] = 99
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateError):
        StateStore(path).load()


def test_unexpected_shape_raises_corrupt(tmp_path):
    path = tmp_path / "state.json"
    data = make_state().to_dict()
    data["pending_request"] = {"wrong_key": 1}
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateCorruptError):
        StateStore(path).load()


def test_unknown_top_level_field_raises_corrupt(tmp_path):
    path = tmp_path / "state.json"
    data = make_state().to_dict()
    data["mystery"] = True
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateCorruptError):
        StateStore(path).load()


def test_archive_moves_state_aside(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.save(make_state())
    backup = store.archive()
    assert backup is not None and backup.exists()
    assert store.load() is None
    assert store.archive() is None
