"""Shared helper for the register+login dance every HTTP-level test starts with.

See docs/TESTS.md. Fifteen test files each carried a byte-identical (or
cosmetically-varied) local helper that did exactly this: POST /register, POST
/login, build the bearer header, then look up the user_id. That is one behaviour
maintained in fifteen places — a change to the auth routes meant fifteen edits,
and drift between copies was invisible until a test failed for an unrelated
reason.

Migration pattern follows `_word_helper.py`: files keep their existing local
helper NAME and signature and rewrite its BODY to delegate here, so **no call
site changes**. That keeps the diff small and reviewable, and means a test's
existing `_register(...)` / `_register_and_login(...)` / `_register_and_get_user(...)`
calls behave exactly as before.

Deliberately NOT migrated (their differences are real, not cosmetic):
  * `test_e2e_learning_loop.py` — asserts the login status code, so it fails
    loudly at setup rather than at the first confusing assertion.
  * `test_chat.py` — asserts BOTH the 201 register and 200 login, and returns
    headers only (no db_pool, no user_id).
  * `test_chat_language.py` — generates its own email internally rather than
    taking one, so callers don't thread an email through.
  * `test_words.py::_registered_token` — returns the raw JWT string, not headers.
  * `test_phrases_seed_admin.py` — returns the user_id as a **UUID object**, not
    a str; downstream SQL there relies on the UUID type.
"""
from httpx import AsyncClient

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"

# Every migrated caller used this exact password. Kept as one constant so a
# future password-policy change (e.g. a longer minimum) is a one-line edit.
PASSWORD = "password123"


async def register_and_login(
    client: AsyncClient,
    db_pool,
    email: str,
) -> tuple[dict, str]:
    """Register + log in `email`, returning `(auth_headers, user_id_str)`.

    The user_id is returned as a **str**, matching what every migrated caller
    expected — several interpolate it into SQL or compare it to a str.

    No status assertions: the migrated helpers had none, and adding them here
    would change when a failing test reports its error. Callers that DO want
    that guarantee keep their own helper (see the module docstring).
    """
    await client.post(REGISTER, json={"email": email, "password": PASSWORD})
    r = await client.post(LOGIN, json={"email": email, "password": PASSWORD})
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    uid = str(await db_pool.fetchval("SELECT user_id FROM users WHERE email = $1", email))
    return headers, uid
