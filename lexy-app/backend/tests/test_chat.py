"""
Chat tests — session lifecycle, message send/receive, auth enforcement.

All tests use the real DB (via the `client` fixture from conftest) so that
session and message persistence is verified end-to-end.

The LLM call (llm_service.evaluate_and_reply) is mocked in every test so
no API key is required and tests stay fast and free.
"""
from unittest.mock import AsyncMock, patch

from httpx import AsyncClient
from ._email_helper import make_test_email

REGISTER = "/api/v1/auth/register"
LOGIN    = "/api/v1/auth/login"
SESSIONS = "/api/v1/chat/sessions"

_MOCK_EVAL = {
    "reply": "Sehr gut! Wie war dein Tag?",
    "language_detected": "de",
    "corrections": [
        {"original": "Ich bin gut", "corrected": "Mir geht es gut", "explanation": "Use 'Mir geht es gut' to express wellbeing."}
    ],
    "word_matches": [],
}


def make_email() -> str:
    return make_test_email()


async def _register_and_login(client: AsyncClient, email: str) -> dict:
    r = await client.post(REGISTER, json={"email": email, "password": "password123"})
    assert r.status_code == 201, f"Register failed {r.status_code}: {r.json()}"
    r = await client.post(LOGIN, json={"email": email, "password": "password123"})
    assert r.status_code == 200, f"Login failed {r.status_code}: {r.json()}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------


async def test_create_session_returns_201_with_session_id(client: AsyncClient):
    headers = await _register_and_login(client, make_email())
    resp = await client.post(SESSIONS, json={"session_type": "free"}, headers=headers)

    assert resp.status_code == 201
    data = resp.json()
    assert "session_id" in data
    assert data["session_type"] == "free"
    assert len(data["session_id"]) == 36  # UUID


async def test_list_sessions_returns_created_session(client: AsyncClient):
    headers = await _register_and_login(client, make_email())
    create_resp = await client.post(SESSIONS, json={}, headers=headers)
    session_id = create_resp.json()["session_id"]

    list_resp = await client.get(SESSIONS, headers=headers)
    assert list_resp.status_code == 200
    ids = [s["session_id"] for s in list_resp.json()]
    assert session_id in ids


async def test_get_session_by_id(client: AsyncClient):
    headers = await _register_and_login(client, make_email())
    session_id = (await client.post(SESSIONS, json={}, headers=headers)).json()["session_id"]

    resp = await client.get(f"{SESSIONS}/{session_id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["session_id"] == session_id


async def test_get_session_belonging_to_other_user_returns_404(client: AsyncClient):
    headers_a = await _register_and_login(client, make_email())
    headers_b = await _register_and_login(client, make_email())

    session_id = (await client.post(SESSIONS, json={}, headers=headers_a)).json()["session_id"]

    resp = await client.get(f"{SESSIONS}/{session_id}", headers=headers_b)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Sending messages — happy path
# ---------------------------------------------------------------------------


async def test_send_message_returns_201_with_both_messages(client: AsyncClient):
    headers = await _register_and_login(client, make_email())
    session_id = (await client.post(SESSIONS, json={}, headers=headers)).json()["session_id"]

    with patch(
        "backend.services.llm_service.evaluate_and_reply",
        new_callable=AsyncMock,
        return_value=_MOCK_EVAL,
    ):
        resp = await client.post(
            f"{SESSIONS}/{session_id}/messages",
            json={"content": "Ich bin gut."},
            headers=headers,
        )

    assert resp.status_code == 201
    data = resp.json()
    assert "user_message" in data
    assert "assistant_message" in data


async def test_user_message_shape(client: AsyncClient):
    headers = await _register_and_login(client, make_email())
    session_id = (await client.post(SESSIONS, json={}, headers=headers)).json()["session_id"]

    with patch(
        "backend.services.llm_service.evaluate_and_reply",
        new_callable=AsyncMock,
        return_value=_MOCK_EVAL,
    ):
        data = (await client.post(
            f"{SESSIONS}/{session_id}/messages",
            json={"content": "Hallo!"},
            headers=headers,
        )).json()

    user_msg = data["user_message"]
    assert user_msg["role"] == "user"
    assert user_msg["content"] == "Hallo!"
    assert user_msg["session_id"] == session_id
    assert "message_id" in user_msg
    assert "created_at" in user_msg


async def test_assistant_message_carries_evaluation_data(client: AsyncClient):
    headers = await _register_and_login(client, make_email())
    session_id = (await client.post(SESSIONS, json={}, headers=headers)).json()["session_id"]

    with patch(
        "backend.services.llm_service.evaluate_and_reply",
        new_callable=AsyncMock,
        return_value=_MOCK_EVAL,
    ):
        data = (await client.post(
            f"{SESSIONS}/{session_id}/messages",
            json={"content": "Ich bin gut."},
            headers=headers,
        )).json()

    asst = data["assistant_message"]
    assert asst["role"] == "assistant"
    assert asst["content"] == _MOCK_EVAL["reply"]
    assert asst["language_detected"] == "de"
    assert len(asst["corrections"]) == 1
    correction = asst["corrections"][0]
    assert "original" in correction
    assert "corrected" in correction
    assert "explanation" in correction


# ---------------------------------------------------------------------------
# Message history
# ---------------------------------------------------------------------------


async def test_get_messages_returns_both_messages_in_order(client: AsyncClient):
    headers = await _register_and_login(client, make_email())
    session_id = (await client.post(SESSIONS, json={}, headers=headers)).json()["session_id"]

    with patch(
        "backend.services.llm_service.evaluate_and_reply",
        new_callable=AsyncMock,
        return_value=_MOCK_EVAL,
    ):
        await client.post(
            f"{SESSIONS}/{session_id}/messages",
            json={"content": "Guten Morgen!"},
            headers=headers,
        )

    msgs = (await client.get(f"{SESSIONS}/{session_id}/messages", headers=headers)).json()
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"


# ---------------------------------------------------------------------------
# Auth enforcement
# ---------------------------------------------------------------------------


async def test_send_message_requires_auth(client: AsyncClient):
    headers = await _register_and_login(client, make_email())
    session_id = (await client.post(SESSIONS, json={}, headers=headers)).json()["session_id"]

    resp = await client.post(
        f"{SESSIONS}/{session_id}/messages",
        json={"content": "Hallo"},
    )
    assert resp.status_code == 403


async def test_missing_content_returns_422(client: AsyncClient):
    headers = await _register_and_login(client, make_email())
    session_id = (await client.post(SESSIONS, json={}, headers=headers)).json()["session_id"]

    resp = await client.post(f"{SESSIONS}/{session_id}/messages", json={}, headers=headers)
    assert resp.status_code == 422
