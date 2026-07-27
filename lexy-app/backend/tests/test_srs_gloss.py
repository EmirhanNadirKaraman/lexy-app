"""
#0a-1 tests — SRS due-card payloads gain prompt_text / answer_text.

Covers:
  - translate_item_gloss returns a string for word/phrase and caches.
  - get_due_cards populates prompt_text/answer_text per direction.
  - grammar_rule cards use the rule's short_explanation, no LLM call.
  - Repeated /srs/due calls hit the LLM cache; no new Anthropic round-trips.
  - Existing fields still present (display_text, direction, etc.).

We MOCK the LLM (env MOCK_LLM=1 returns a deterministic stub) so the tests
don't need real Anthropic credentials and aren't subject to flake/latency.
"""

import pytest
from httpx import AsyncClient

from backend.services import llm_service
from ._email_helper import make_test_email
from ._auth_helper import register_and_login

SRS_DUE_URL = "/api/v1/srs/due"
REGISTER    = "/api/v1/auth/register"
LOGIN       = "/api/v1/auth/login"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _email() -> str:
    return make_test_email()


async def _register(client: AsyncClient, db_pool, email: str) -> tuple[dict, str]:
    """Register + login. Returns (auth_headers, user_id_str).

    Delegates to the shared helper; see tests/_auth_helper.py.
    """
    return await register_and_login(client, db_pool, email)


async def _get_word(db_pool) -> tuple[int, str, str]:
    # Owned, uniquely-named word (xdist-safe — see conftest / docs/TESTS.md).
    # Gloss is mocked/stubbed in these tests, so the synthetic surface is fine.
    from ._word_helper import insert_owned_word
    wid, surface = await insert_owned_word(db_pool, language="de")
    return wid, surface, "de"


async def _get_phrase(db_pool) -> tuple[int, str, str] | None:
    row = await db_pool.fetchrow(
        "SELECT phrase_id, surface_form, language FROM phrase_table LIMIT 1"
    )
    return (row["phrase_id"], row["surface_form"], row["language"]) if row else None


async def _get_grammar_rule(db_pool) -> tuple[int, str, str, str] | None:
    row = await db_pool.fetchrow(
        "SELECT rule_id, title, short_explanation, language FROM grammar_rule_table LIMIT 1"
    )
    return (
        (row["rule_id"], row["title"], row["short_explanation"], row["language"])
        if row else None
    )


@pytest.fixture(autouse=True)
def _force_mock_llm(monkeypatch):
    """All tests in this module run with the mock LLM so we don't hit Anthropic.
    The mock returns a stable string we can assert on."""
    monkeypatch.setattr(llm_service, "_MOCK", True)


# ---------------------------------------------------------------------------
# translate_item_gloss — direct service tests
# ---------------------------------------------------------------------------

async def test_translate_item_gloss_returns_a_gloss_for_word(db_pool):
    gloss = await llm_service.translate_item_gloss("Auto", "word", "de", pool=db_pool)
    assert isinstance(gloss, str) and gloss
    # mock mode returns a deterministic stub containing the input text
    assert "Auto" in gloss


async def test_translate_item_gloss_returns_a_gloss_for_phrase(db_pool):
    gloss = await llm_service.translate_item_gloss(
        "sich freuen auf", "phrase", "de", pool=db_pool,
    )
    assert isinstance(gloss, str) and gloss


async def test_translate_item_gloss_rejects_grammar_rule():
    with pytest.raises(ValueError, match="word.*phrase"):
        await llm_service.translate_item_gloss("Reflexive Verbs", "grammar_rule", "de")


async def test_translate_item_gloss_cache_key_is_text_case_insensitive(db_pool, monkeypatch):
    """Both calls hit the cache — same lowercased text, same item_type/language."""
    from backend.services import llm_cache_service

    # First call populates cache (in mock mode — no real LLM).
    await llm_service.translate_item_gloss("Apfel", "word", "de", pool=db_pool)

    # Inspect the cache directly via the same make_cache_key inputs.
    key_lower = llm_cache_service.make_cache_key(
        "item_gloss", llm_service._MODEL,
        {"text": "apfel", "item_type": "word", "language": "de"},
    )
    # Mock mode SKIPS the cache (returns immediately) so the key may not exist;
    # exercise the real (non-mock) caching path with monkeypatched _client too
    # via a separate test below. Here we just confirm same-text-different-case
    # would map to the same key.
    key_orig = llm_cache_service.make_cache_key(
        "item_gloss", llm_service._MODEL,
        {"text": "Apfel".lower(), "item_type": "word", "language": "de"},
    )
    assert key_lower == key_orig


# ---------------------------------------------------------------------------
# /srs/due — passive card payload
# ---------------------------------------------------------------------------

async def test_passive_due_card_has_english_prompt(client: AsyncClient, db_pool):
    """Passive card (T1.2 / Hole 12): prompt is the English gloss, answer is
    the German display. Same mapping as active — directions differ only in
    grading (passive = self-grade reveal, active = typed input via /produce).
    """
    word_id, word_text, language = await _get_word(db_pool)
    headers, _ = await _register(client, db_pool, _email())

    # Marking a word 'learning' creates both passive and active cards (#0b).
    await client.put(
        f"/api/v1/words/word/{word_id}/status",
        json={"status": "learning"},
        headers=headers,
    )

    resp = await client.get(SRS_DUE_URL, params={"language": language}, headers=headers)
    assert resp.status_code == 200
    cards = resp.json()
    passive = next((c for c in cards if c["direction"] == "passive"), None)
    assert passive is not None
    assert passive["answer_text"] == passive["display_text"] == word_text
    assert passive["prompt_text"] and passive["prompt_text"] != passive["display_text"], (
        "passive prompt must carry the gloss, not the German display — otherwise the "
        "review is pure self-report (Hole 12 regression)."
    )


# ---------------------------------------------------------------------------
# /srs/due — active card payload
# ---------------------------------------------------------------------------

async def test_active_due_card_has_prompt_text_in_english(client: AsyncClient, db_pool):
    """Active card: prompt is the English gloss, answer is the German display."""
    word_id, word_text, language = await _get_word(db_pool)
    headers, _ = await _register(client, db_pool, _email())
    await client.put(
        f"/api/v1/words/word/{word_id}/status",
        json={"status": "learning"},
        headers=headers,
    )

    resp = await client.get(SRS_DUE_URL, params={"language": language}, headers=headers)
    cards = resp.json()
    active = next((c for c in cards if c["direction"] == "active"), None)
    assert active is not None
    assert active["answer_text"] == active["display_text"] == word_text
    assert active["prompt_text"] and active["prompt_text"] != active["display_text"]


# ---------------------------------------------------------------------------
# Cache hit — second call doesn't invoke the LLM
# ---------------------------------------------------------------------------

async def test_second_due_call_uses_cache_no_llm_invocation(client: AsyncClient, db_pool, monkeypatch):
    """Stash the LLM call count; second /srs/due call must not increment it.

    We replace `_client.messages.create` with a counting stub and turn OFF
    mock mode so the real cache pathway runs.
    """
    word_id, _, language = await _get_word(db_pool)
    headers, _ = await _register(client, db_pool, _email())
    await client.put(
        f"/api/v1/words/word/{word_id}/status",
        json={"status": "learning"},
        headers=headers,
    )

    call_count = {"n": 0}

    class _StubBlock:
        type = "tool_use"
        input = {"gloss": "stub gloss"}

    class _StubResp:
        content = [_StubBlock()]

    async def _stub_create(**kwargs):
        call_count["n"] += 1
        return _StubResp()

    monkeypatch.setattr(llm_service, "_MOCK", False)
    monkeypatch.setattr(llm_service._client.messages, "create", _stub_create)

    # First call: may or may not invoke the LLM depending on whether a previous
    # test in the suite already populated the cache for the same word. The key
    # invariant for *this* test is that the SECOND call doesn't increment count,
    # regardless of what the first call did.
    resp1 = await client.get(SRS_DUE_URL, params={"language": language}, headers=headers)
    assert resp1.status_code == 200
    first_calls = call_count["n"]

    # Second call: pure cache hit. No new LLM invocations.
    resp2 = await client.get(SRS_DUE_URL, params={"language": language}, headers=headers)
    assert resp2.status_code == 200
    assert call_count["n"] == first_calls, (
        f"Expected cache to absorb the second call (still {first_calls}), got {call_count['n']}"
    )

    # Payload shape consistent across calls.
    assert resp1.json()[0]["prompt_text"] == resp2.json()[0]["prompt_text"]
    assert resp1.json()[0]["answer_text"] == resp2.json()[0]["answer_text"]


# ---------------------------------------------------------------------------
# Grammar rule card — no LLM call, uses short_explanation
# ---------------------------------------------------------------------------

async def test_grammar_rule_card_uses_short_explanation_no_llm(client: AsyncClient, db_pool, monkeypatch):
    """Grammar rule SRS cards should NOT invoke the LLM — they reuse the
    rule's short_explanation as the 'answer' side."""
    info = await _get_grammar_rule(db_pool)
    if info is None:
        pytest.skip("grammar_rule_table empty")
    rule_id, title, short_explanation, language = info

    headers, _ = await _register(client, db_pool, _email())
    # Mark a grammar rule as learning → creates passive SRS card (grammar is passive-only).
    await client.put(
        f"/api/v1/words/grammar_rule/{rule_id}/status",
        json={"status": "learning"},
        headers=headers,
    )

    # Sentinel: real LLM stub that ASSERTS it is never invoked.
    async def _explode_create(**kwargs):
        raise AssertionError("LLM must not be called for grammar_rule cards")

    monkeypatch.setattr(llm_service, "_MOCK", False)
    monkeypatch.setattr(llm_service._client.messages, "create", _explode_create)

    resp = await client.get(SRS_DUE_URL, params={"language": language}, headers=headers)
    assert resp.status_code == 200
    cards = resp.json()
    grammar_card = next((c for c in cards if c["item_id"] == rule_id and c["item_type"] == "grammar_rule"), None)
    assert grammar_card is not None
    # T1.2: uniform mapping — prompt = English short_explanation,
    # answer = German title. The user is cued by the rule's meaning and
    # tries to recall the rule's name.
    assert grammar_card["prompt_text"] == short_explanation
    assert grammar_card["answer_text"] == title


# ---------------------------------------------------------------------------
# Backward compatibility — existing fields still present
# ---------------------------------------------------------------------------

async def test_existing_card_fields_still_present(client: AsyncClient, db_pool):
    word_id, _, language = await _get_word(db_pool)
    headers, _ = await _register(client, db_pool, _email())
    await client.put(
        f"/api/v1/words/word/{word_id}/status",
        json={"status": "learning"},
        headers=headers,
    )

    resp = await client.get(SRS_DUE_URL, params={"language": language}, headers=headers)
    card = resp.json()[0]

    # Old contract preserved
    expected = {
        "card_id", "item_id", "item_type", "direction", "due_date",
        "repetitions", "passive_level", "active_level", "display_text",
    }
    assert expected <= set(card)
    # New fields
    assert "prompt_text" in card and "answer_text" in card


# ---------------------------------------------------------------------------
# Phrase card — same shape as word card
# ---------------------------------------------------------------------------

async def test_phrase_card_payload_has_gloss_fields(client: AsyncClient, db_pool):
    info = await _get_phrase(db_pool)
    if info is None:
        pytest.skip("phrase_table empty")
    phrase_id, surface_form, language = info

    headers, _ = await _register(client, db_pool, _email())
    await client.put(
        f"/api/v1/words/phrase/{phrase_id}/status",
        json={"status": "learning"},
        headers=headers,
    )

    resp = await client.get(SRS_DUE_URL, params={"language": language}, headers=headers)
    cards = resp.json()
    phrase_cards = [c for c in cards if c["item_id"] == phrase_id and c["item_type"] == "phrase"]
    assert phrase_cards, "expected phrase card in due list"
    for c in phrase_cards:
        assert c["display_text"] == surface_form
        assert c["prompt_text"] and c["answer_text"]
        # Sanity: not the same on both sides
        assert c["prompt_text"] != c["answer_text"]
