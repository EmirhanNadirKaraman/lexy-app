"""
Settings / preferences tests.

Unit tests (no DB) — test pure functions:
  apply_defaults:
    - empty raw → all defaults present
    - known key overrides default
    - unknown key in raw is silently dropped
    - partial override keeps other defaults
    - DEFAULTS dict is not mutated

Integration tests (real DB):
  - get_preferences for a new user returns defaults
  - update_preferences single field changes only that field
  - update_preferences multiple fields changes all of them
  - update_preferences unknown key is silently ignored
  - get then update then get: second get reflects the change

HTTP tests (FastAPI client):
  - GET /api/v1/settings/preferences returns 200 with all 7 keys
  - GET requires auth → 403
  - PUT returns 200 with all 7 keys
  - PUT requires auth → 403
  - PUT invalid hex color → 422
  - PUT reps below minimum → 422
  - PUT reps above maximum → 422
  - PUT empty body changes nothing
  - PUT only changes the specified field
"""

import pytest

from backend.services.settings_service import DEFAULTS, apply_defaults, get_preferences, update_preferences
from ._email_helper import cleanup_pattern, make_test_email


# ---------------------------------------------------------------------------
# Unit tests — pure function, no DB
# ---------------------------------------------------------------------------

class TestApplyDefaults:
    def test_empty_raw_returns_all_defaults(self):
        result = apply_defaults({})
        assert result == DEFAULTS

    def test_known_key_overrides_default(self):
        result = apply_defaults({"known_word_color": "#000000"})
        assert result["known_word_color"] == "#000000"

    def test_other_defaults_still_present_after_override(self):
        result = apply_defaults({"known_word_color": "#000000"})
        for key in DEFAULTS:
            assert key in result

    def test_unknown_key_is_dropped(self):
        result = apply_defaults({"nonexistent_key": "some_value"})
        assert "nonexistent_key" not in result

    def test_partial_override_keeps_other_defaults(self):
        result = apply_defaults({"passive_reps_for_known": 7})
        assert result["passive_reps_for_known"] == 7
        assert result["active_reps_for_known"] == DEFAULTS["active_reps_for_known"]

    def test_defaults_dict_not_mutated(self):
        original_defaults = dict(DEFAULTS)
        apply_defaults({"liked_channels": ["ch1"]})
        assert DEFAULTS == original_defaults

    def test_all_keys_overridden(self):
        # Channel arrays moved to user_channel_preference in T1.4 (mig 027),
        # so apply_defaults drops them; only JSONB-resident keys round-trip.
        overrides = {
            "channel_names":          {"channel1": "Sports Channel"},
            "passive_reps_for_known": 10,
            "active_reps_for_known":  8,
            "known_word_color":       "#111111",
            "learning_word_color":    "#222222",
            "unknown_word_color":     "#333333",
            "reminders_enabled":      False,
        }
        result = apply_defaults(overrides)
        # apply_defaults returns all default keys with provided overrides applied
        assert set(result.keys()) == set(DEFAULTS.keys())
        for key, value in overrides.items():
            assert result[key] == value


# ---------------------------------------------------------------------------
# Integration tests — real DB
# ---------------------------------------------------------------------------

async def _create_user(pool) -> str:
    """Insert a fresh test user, return user_id string."""
    row = await pool.fetchrow(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING user_id",
        make_test_email(),
    )
    return str(row["user_id"])


async def test_get_preferences_new_user_returns_defaults(db_pool):
    """A fresh user sees DEFAULTS plus empty derived lists for relational fields."""
    user_id = await _create_user(db_pool)
    result = await get_preferences(db_pool, user_id)
    expected = {
        **DEFAULTS,
        # Category prefs (user_video_category) — derived per get_preferences.
        "liked_categories": [], "disliked_categories": [],
        "liked_genres":     [], "disliked_genres":     [],
        # Channel prefs (user_channel_preference, T1.4) — derived too.
        "followed_channels": [], "liked_channels": [], "disliked_channels": [],
    }
    assert result == expected


async def test_update_preferences_single_field(db_pool):
    user_id = await _create_user(db_pool)
    result = await update_preferences(db_pool, user_id, {"known_word_color": "#1a237e"})
    assert result["known_word_color"] == "#1a237e"
    # All other fields unchanged
    assert result["learning_word_color"] == DEFAULTS["learning_word_color"]
    assert result["passive_reps_for_known"] == DEFAULTS["passive_reps_for_known"]


async def test_update_preferences_multiple_fields(db_pool):
    user_id = await _create_user(db_pool)
    result = await update_preferences(db_pool, user_id, {
        "liked_channels": ["channel1"],
        "passive_reps_for_known": 7,
    })
    assert result["liked_channels"] == ["channel1"]
    assert result["passive_reps_for_known"] == 7
    assert result["active_reps_for_known"] == DEFAULTS["active_reps_for_known"]


async def test_update_preferences_unknown_key_ignored(db_pool):
    user_id = await _create_user(db_pool)
    result = await update_preferences(db_pool, user_id, {
        "nonexistent_key": "should_be_dropped",
        "known_word_color": "#abcdef",
    })
    assert "nonexistent_key" not in result
    assert result["known_word_color"] == "#abcdef"


async def test_update_preferences_roundtrip(db_pool):
    user_id = await _create_user(db_pool)

    await update_preferences(db_pool, user_id, {"liked_channels": ["chan1", "chan2"]})
    fetched = await get_preferences(db_pool, user_id)

    assert fetched["liked_channels"] == ["chan1", "chan2"]


async def test_update_preserves_previous_updates(db_pool):
    user_id = await _create_user(db_pool)

    await update_preferences(db_pool, user_id, {"known_word_color": "#111111"})
    await update_preferences(db_pool, user_id, {"learning_word_color": "#222222"})

    fetched = await get_preferences(db_pool, user_id)
    assert fetched["known_word_color"] == "#111111"
    assert fetched["learning_word_color"] == "#222222"


# ---------------------------------------------------------------------------
# HTTP tests — FastAPI client
# ---------------------------------------------------------------------------

async def _auth_token(client) -> str:
    email = make_test_email()
    await client.post("/api/v1/auth/register", json={"email": email, "password": "password123"})
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": "password123"})
    return resp.json()["access_token"]


# Derive from settings_service.DEFAULTS (the source of truth) plus the four
# derived keys that get_preferences always returns alongside the JSON blob:
#   liked_categories / disliked_categories — from user_video_category table
#   liked_genres     / disliked_genres     — frontend-facing aliases for above
# Anything new added to DEFAULTS will flow through automatically. Adding a new
# *derived* key (i.e. one not in DEFAULTS) requires updating this union.
ALL_PREFERENCE_KEYS = set(DEFAULTS) | {
    "liked_categories", "disliked_categories",
    "liked_genres",     "disliked_genres",
    # Channel-preference fields moved to user_channel_preference in T1.4
    # but are still surfaced in the response dict for API compatibility.
    "followed_channels", "liked_channels", "disliked_channels",
}


async def test_get_returns_200_with_all_keys(client):
    token = await _auth_token(client)
    resp = await client.get(
        "/api/v1/settings/preferences",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert ALL_PREFERENCE_KEYS == set(body.keys())


async def test_get_requires_auth(client):
    resp = await client.get("/api/v1/settings/preferences")
    assert resp.status_code == 403


async def test_put_returns_200_with_all_keys(client):
    token = await _auth_token(client)
    resp = await client.put(
        "/api/v1/settings/preferences",
        json={"known_word_color": "#1a237e"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert ALL_PREFERENCE_KEYS == set(body.keys())
    assert body["known_word_color"] == "#1a237e"


async def test_put_requires_auth(client):
    resp = await client.put(
        "/api/v1/settings/preferences",
        json={"known_word_color": "#000000"},
    )
    assert resp.status_code == 403


async def test_put_invalid_hex_color_returns_422(client):
    token = await _auth_token(client)
    resp = await client.put(
        "/api/v1/settings/preferences",
        json={"known_word_color": "#zzzzzz"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


async def test_put_short_hex_color_returns_422(client):
    token = await _auth_token(client)
    resp = await client.put(
        "/api/v1/settings/preferences",
        json={"known_word_color": "#fff"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


async def test_put_reps_below_min_returns_422(client):
    token = await _auth_token(client)
    resp = await client.put(
        "/api/v1/settings/preferences",
        json={"passive_reps_for_known": 0},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


async def test_put_reps_above_max_returns_422(client):
    token = await _auth_token(client)
    resp = await client.put(
        "/api/v1/settings/preferences",
        json={"active_reps_for_known": 21},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


async def test_put_empty_body_changes_nothing(client):
    token = await _auth_token(client)

    get_resp = await client.get(
        "/api/v1/settings/preferences",
        headers={"Authorization": f"Bearer {token}"},
    )
    original = get_resp.json()

    put_resp = await client.put(
        "/api/v1/settings/preferences",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert put_resp.status_code == 200
    assert put_resp.json() == original


async def test_put_only_changes_specified_field(client):
    token = await _auth_token(client)

    resp = await client.put(
        "/api/v1/settings/preferences",
        json={"passive_reps_for_known": 9},
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.json()
    assert body["passive_reps_for_known"] == 9
    assert body["active_reps_for_known"] == DEFAULTS["active_reps_for_known"]
    assert body["known_word_color"] == DEFAULTS["known_word_color"]


# ---------------------------------------------------------------------------
# theme_mode tristate (T1.3) + dark_mode legacy compatibility
# ---------------------------------------------------------------------------

async def test_new_user_theme_mode_defaults_to_system(client):
    """T1.3: brand-new users get theme_mode='system' so iOS follows OS dark mode."""
    token = await _auth_token(client)
    resp = await client.get(
        "/api/v1/settings/preferences",
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.json()
    assert body["theme_mode"] == "system"
    # Legacy mirror: system resolves to "not dark" by storage default.
    assert body["dark_mode"] is False


@pytest.mark.parametrize("mode", ["system", "light", "dark"])
async def test_put_theme_mode_persists(client, mode):
    token = await _auth_token(client)
    put = await client.put(
        "/api/v1/settings/preferences",
        json={"theme_mode": mode},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert put.status_code == 200
    assert put.json()["theme_mode"] == mode

    get = await client.get(
        "/api/v1/settings/preferences",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert get.json()["theme_mode"] == mode


async def test_put_invalid_theme_mode_returns_422(client):
    token = await _auth_token(client)
    resp = await client.put(
        "/api/v1/settings/preferences",
        json={"theme_mode": "midnight"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


async def test_setting_theme_mode_mirrors_dark_mode_for_legacy_clients(client):
    """T1.3 compat: theme_mode is source of truth, dark_mode mirrors it on the wire."""
    token = await _auth_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    dark = await client.put("/api/v1/settings/preferences",
                            json={"theme_mode": "dark"}, headers=headers)
    assert dark.json()["dark_mode"] is True

    light = await client.put("/api/v1/settings/preferences",
                             json={"theme_mode": "light"}, headers=headers)
    assert light.json()["dark_mode"] is False

    system = await client.put("/api/v1/settings/preferences",
                              json={"theme_mode": "system"}, headers=headers)
    # system resolves to "not dark" for legacy boolean readers — they can't
    # follow prefers-color-scheme anyway.
    assert system.json()["dark_mode"] is False


async def test_legacy_dark_mode_true_derives_theme_mode_dark(client):
    """A legacy client sending only dark_mode=True must end up with theme_mode='dark'."""
    token = await _auth_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.put("/api/v1/settings/preferences",
                            json={"dark_mode": True}, headers=headers)
    body = resp.json()
    assert body["dark_mode"] is True
    assert body["theme_mode"] == "dark"


async def test_legacy_dark_mode_false_derives_theme_mode_light(client):
    """dark_mode=False is an explicit user choice — must NOT silently upgrade to system."""
    token = await _auth_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.put("/api/v1/settings/preferences",
                            json={"dark_mode": False}, headers=headers)
    body = resp.json()
    assert body["dark_mode"] is False
    assert body["theme_mode"] == "light"


async def test_existing_user_with_stored_dark_mode_only_reads_with_derived_theme(db_pool):
    """
    Backfill simulation: an existing row in users.settings JSONB that only has
    `dark_mode=True` (saved before T1.3) must come back with theme_mode='dark'.
    """
    import json as _json
    user_id = await _create_user(db_pool)
    # Plant a legacy settings blob directly (bypasses update_preferences).
    await db_pool.execute(
        "UPDATE users SET settings = $1::jsonb WHERE user_id = $2::uuid",
        _json.dumps({"dark_mode": True}),
        user_id,
    )
    prefs = await get_preferences(db_pool, user_id)
    assert prefs["theme_mode"] == "dark"
    assert prefs["dark_mode"] is True


# ---------------------------------------------------------------------------
# T1.4 — channel preferences moved to user_channel_preference
# ---------------------------------------------------------------------------

async def test_new_user_channel_prefs_are_empty_lists(client):
    token = await _auth_token(client)
    resp = await client.get("/api/v1/settings/preferences",
                            headers={"Authorization": f"Bearer {token}"})
    body = resp.json()
    assert body["followed_channels"] == []
    assert body["liked_channels"] == []
    assert body["disliked_channels"] == []


async def test_put_followed_channels_persists_relationally(client, db_pool):
    token = await _auth_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    put = await client.put("/api/v1/settings/preferences",
                           json={"followed_channels": ["UC_x", "UC_y"]},
                           headers=headers)
    assert put.json()["followed_channels"] == ["UC_x", "UC_y"]
    get = await client.get("/api/v1/settings/preferences", headers=headers)
    assert get.json()["followed_channels"] == ["UC_x", "UC_y"]

    # Belt-and-braces: rows exist in the relational table, not JSONB.
    rows = await db_pool.fetch(
        """
        SELECT u.user_id FROM users u
        WHERE u.email LIKE $1
        ORDER BY u.created_at DESC LIMIT 1
        """,
        cleanup_pattern(),
    )
    uid = rows[0]["user_id"]
    channel_rows = await db_pool.fetch(
        """
        SELECT youtube_channel_id, preference_kind
          FROM user_channel_preference
         WHERE user_id = $1 AND preference_kind = 'followed'
         ORDER BY youtube_channel_id
        """,
        uid,
    )
    assert [(r["youtube_channel_id"], r["preference_kind"]) for r in channel_rows] == [
        ("UC_x", "followed"), ("UC_y", "followed"),
    ]
    # JSONB blob does NOT contain followed_channels anymore.
    settings_row = await db_pool.fetchrow(
        "SELECT settings FROM users WHERE user_id = $1", uid,
    )
    import json as _json
    settings_raw = settings_row["settings"]
    if isinstance(settings_raw, str):
        settings_raw = _json.loads(settings_raw)
    assert "followed_channels" not in settings_raw


async def test_put_liked_channels_persists_relationally(client):
    token = await _auth_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    put = await client.put("/api/v1/settings/preferences",
                           json={"liked_channels": ["UC_a"]}, headers=headers)
    assert put.json()["liked_channels"] == ["UC_a"]


async def test_put_disliked_channels_persists_relationally(client):
    token = await _auth_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    put = await client.put("/api/v1/settings/preferences",
                           json={"disliked_channels": ["UC_b"]}, headers=headers)
    assert put.json()["disliked_channels"] == ["UC_b"]


async def test_put_replaces_existing_channel_list(client):
    """PUT is full-replace per kind, not a merge — mirrors pre-T1.4 semantics."""
    token = await _auth_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    await client.put("/api/v1/settings/preferences",
                     json={"followed_channels": ["A", "B"]}, headers=headers)
    await client.put("/api/v1/settings/preferences",
                     json={"followed_channels": ["C"]}, headers=headers)
    body = (await client.get("/api/v1/settings/preferences", headers=headers)).json()
    assert body["followed_channels"] == ["C"]


async def test_channel_action_follow_then_dislike_clears_followed(client):
    """Dislike removes both followed AND liked (mutual exclusion + dislike-override)."""
    token = await _auth_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"channel_id": "UC_q", "channel_name": "Quack"}

    await client.put("/api/v1/settings/channel-preference",
                     json={**payload, "action": "follow"}, headers=headers)
    await client.put("/api/v1/settings/channel-preference",
                     json={**payload, "action": "like"}, headers=headers)
    body = (await client.put("/api/v1/settings/channel-preference",
                             json={**payload, "action": "dislike"}, headers=headers)).json()

    assert body["followed_channels"] == []
    assert body["liked_channels"] == []
    assert body["disliked_channels"] == ["UC_q"]
    # channel_names cache still has the display name.
    assert body["channel_names"]["UC_q"] == "Quack"


async def test_channel_action_clear_removes_all_three(client):
    token = await _auth_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"channel_id": "UC_r", "channel_name": "Rover"}

    await client.put("/api/v1/settings/channel-preference",
                     json={**payload, "action": "follow"}, headers=headers)
    body = (await client.put("/api/v1/settings/channel-preference",
                             json={**payload, "action": "clear"}, headers=headers)).json()

    assert body["followed_channels"] == []
    assert body["liked_channels"] == []
    assert body["disliked_channels"] == []
    # Display-name cache evicted because no remaining presence.
    assert "UC_r" not in body["channel_names"]


async def test_channel_action_follow_and_like_coexist(client):
    """followed + liked are NOT mutually exclusive (only liked/disliked are)."""
    token = await _auth_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"channel_id": "UC_z", "channel_name": "Zed"}

    await client.put("/api/v1/settings/channel-preference",
                     json={**payload, "action": "follow"}, headers=headers)
    body = (await client.put("/api/v1/settings/channel-preference",
                             json={**payload, "action": "like"}, headers=headers)).json()

    assert body["followed_channels"] == ["UC_z"]
    assert body["liked_channels"] == ["UC_z"]


async def test_scalar_settings_still_persist_in_jsonb(client, db_pool):
    """T1.4 must not break the JSONB path for color / theme / reps scalars."""
    token = await _auth_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    await client.put("/api/v1/settings/preferences",
                     json={"known_word_color": "#123456", "passive_reps_for_known": 9},
                     headers=headers)

    # Raw JSONB row contains the scalars (regression guard).
    rows = await db_pool.fetch(
        "SELECT user_id, settings FROM users WHERE email LIKE $1 ORDER BY created_at DESC LIMIT 1",
        cleanup_pattern(),
    )
    import json as _json
    raw = rows[0]["settings"]
    if isinstance(raw, str):
        raw = _json.loads(raw)
    assert raw["known_word_color"] == "#123456"
    assert raw["passive_reps_for_known"] == 9


async def test_channel_action_preserves_out_of_band_jsonb_keys(db_pool):
    """Audit #8: channel preference actions used to full-replace users.settings
    with a DEFAULTS-filtered dict, silently dropping any out-of-band keys (e.g.
    is_admin planted via SQL). The fix preserves the raw JSONB blob and only
    rewrites `channel_names`.
    """
    import json as _json
    from backend.services.settings_service import channel_preference_action
    user_id = await _create_user(db_pool)
    await db_pool.execute(
        "UPDATE users SET settings = $1::jsonb WHERE user_id = $2::uuid",
        _json.dumps({"is_admin": True, "custom_key": "keep-me",
                     "known_word_color": "#abcdef"}),
        user_id,
    )

    # Each of the four action paths must preserve out-of-band keys.
    for action in ("follow", "like", "dislike", "clear"):
        await channel_preference_action(
            db_pool, user_id, "UC_audit8", "Audit-8 Channel", action,
        )
        raw = (await db_pool.fetchrow(
            "SELECT settings FROM users WHERE user_id = $1::uuid", user_id,
        ))["settings"]
        if isinstance(raw, str):
            raw = _json.loads(raw)
        assert raw.get("is_admin") is True, f"is_admin lost after action={action}"
        assert raw.get("custom_key") == "keep-me", f"custom_key lost after action={action}"
        # Scalar pref planted in JSONB also survives.
        assert raw.get("known_word_color") == "#abcdef", (
            f"known_word_color lost after action={action}"
        )


async def test_channel_action_updates_channel_names_cache(db_pool):
    """Sanity check the cache write still works alongside the new preservation
    logic — follow planting + clear-when-empty eviction both behave correctly."""
    import json as _json
    from backend.services.settings_service import channel_preference_action
    user_id = await _create_user(db_pool)

    # Plant out-of-band keys to make sure they coexist with channel_names writes.
    await db_pool.execute(
        "UPDATE users SET settings = $1::jsonb WHERE user_id = $2::uuid",
        _json.dumps({"is_admin": True}), user_id,
    )

    await channel_preference_action(db_pool, user_id, "UC_cache", "Cache TV", "follow")
    raw = (await db_pool.fetchrow(
        "SELECT settings FROM users WHERE user_id = $1::uuid", user_id,
    ))["settings"]
    if isinstance(raw, str):
        raw = _json.loads(raw)
    assert raw["channel_names"]["UC_cache"] == "Cache TV"
    assert raw["is_admin"] is True  # not nuked

    # Clear removes the channel's presence + evicts its name (no remaining state).
    await channel_preference_action(db_pool, user_id, "UC_cache", "Cache TV", "clear")
    raw = (await db_pool.fetchrow(
        "SELECT settings FROM users WHERE user_id = $1::uuid", user_id,
    ))["settings"]
    if isinstance(raw, str):
        raw = _json.loads(raw)
    assert "UC_cache" not in raw.get("channel_names", {})
    assert raw["is_admin"] is True  # still preserved across the clear path too


async def test_legacy_jsonb_channel_arrays_backfilled_by_migration(db_pool):
    """
    The migration 027 backfill is exercised once at alembic upgrade head.
    This test simulates a user inserted with the legacy shape and verifies
    that the relational rows + read path align — i.e. if a JSONB blob with
    channel arrays were ever planted manually, _fetch_channel_prefs would
    still surface them ONCE the corresponding rows are present.
    """
    import json as _json
    user_id = await _create_user(db_pool)
    # Plant relational rows directly (what the migration does for existing users).
    for cid in ("X1", "X2"):
        await db_pool.execute(
            """
            INSERT INTO user_channel_preference (user_id, youtube_channel_id, preference_kind)
            VALUES ($1::uuid, $2, 'followed') ON CONFLICT DO NOTHING
            """,
            user_id, cid,
        )
    # And a legacy JSONB blob — the read path should IGNORE it (relational wins).
    await db_pool.execute(
        "UPDATE users SET settings = $1::jsonb WHERE user_id = $2::uuid",
        _json.dumps({"followed_channels": ["IGNORED_LEGACY"]}),
        user_id,
    )
    prefs = await get_preferences(db_pool, user_id)
    assert prefs["followed_channels"] == ["X1", "X2"], (
        "relational table must be source of truth; stale JSONB array must be ignored"
    )
