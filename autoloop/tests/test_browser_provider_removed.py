"""brw-16 (2026-08-25): the browser provider is neither registered nor required
by configuration.

`test_conversation.py` pins the REGISTRY half of that claim. This file pins the
CONFIGURATION half and what falls out of it:

1. a config with no `[browser]` section at all loads;
2. a config that still HAS one loads, unchanged, and is simply not consulted —
   an operator upgrading mid-flight must not have to edit a file first;
3. a `[conversation]` section still naming the retired provider is handled
   EXPLICITLY, never silently;
4. `autoloop/tests/conftest.py` no longer imports `autoloop`, which is what
   makes `validation.select_validation_commands` able to narrow anything at all.
"""

from pathlib import Path

import pytest

from autoloop.config import (
    RETIRED_BROWSER_PROVIDER,
    BrowserConfig,
    ConversationConfig,
    load_config,
)
from autoloop.conversation import available_providers
from autoloop.errors import ConfigError

#: Every key `[browser]` accepts, written out, so "an unused section is ignored
#: rather than rejected" is checked against the WHOLE section and not just the
#: one key that used to be required.
FULL_BROWSER_SECTION = "\n".join(
    [
        "[browser]",
        'conversation_url = "https://chatgpt.com/c/left-over"',
        'project_url = "https://chatgpt.com/g/g-p-left-over/project"',
        'cdp_url = "http://127.0.0.1:9222"',
        "attach_oversized_diff = true",
        "composer_timeout_seconds = 30.0",
        "input_sync_timeout_seconds = 30.0",
        "send_ready_timeout_seconds = 30.0",
        "submit_timeout_seconds = 60.0",
        "response_start_timeout_seconds = 120.0",
        "response_timeout_seconds = 900.0",
        "reconcile_timeout_seconds = 30.0",
        "poll_interval_seconds = 2.0",
        "stability_seconds = 3.0",
        'restart_command = ["python3", "-m", "autoloop.browser.chrome_restart"]',
        "restart_cooldown_seconds = 120.0",
        "rate_limit_backoff_seconds = 60.0",
        "rate_limit_backoff_max_seconds = 600.0",
    ]
)


def write_config(tmp_path, body=""):
    """A config carrying only what is genuinely REQUIRED, plus `body`."""
    path = tmp_path / "config.toml"
    path.write_text(
        (body + "\n" if body else "")
        + f'[paths]\nworkers_root = "{tmp_path / "workers"}"\n',
        encoding="utf-8",
    )
    return path


# ---- 1. no [browser] section --------------------------------------------------


def test_a_config_with_no_browser_section_loads(tmp_path):
    """THE configuration half of the claim. Before brw-16 this raised
    `browser.conversation_url is required`, which is why a live ChatGPT thread
    URL sat in `config.toml` purely to satisfy a validator for a transport
    nothing selected."""
    config = load_config(write_config(tmp_path))

    assert config.browser == BrowserConfig()
    assert config.browser.conversation_url == ""
    assert config.migration_notices == (), "nothing retired was named"


def test_the_default_provider_is_one_that_is_actually_registered(tmp_path):
    """The fail-open this closes: a config with no `[conversation]` section
    would otherwise LOAD cleanly and then die on the first step, because the
    dataclass default still named the unregistered browser seat."""
    config = load_config(write_config(tmp_path))

    assert config.conversation.provider in available_providers()
    assert ConversationConfig().provider in available_providers()
    assert ConversationConfig().fallback_provider == "", "no failover by default"


def test_an_empty_conversation_url_is_not_a_reason_to_refuse(tmp_path):
    """Explicitly empty, rather than absent — the shape a half-migrated config
    has. It must be as acceptable as the section being gone."""
    config = load_config(write_config(tmp_path, '[browser]\nconversation_url = ""'))

    assert config.browser.conversation_url == ""


# ---- 2. a leftover [browser] section ------------------------------------------


def test_a_config_that_still_has_a_full_browser_section_loads(tmp_path):
    """An unused section is IGNORED, not rejected. Refusing here would mean an
    operator has to edit a config file before the upgraded loop will start,
    which is the one thing this change was not allowed to cost."""
    config = load_config(write_config(tmp_path, FULL_BROWSER_SECTION))

    assert config.browser.conversation_url == "https://chatgpt.com/c/left-over"
    assert config.browser.project_url.endswith("/project")
    assert config.browser.restart_command == (
        "python3",
        "-m",
        "autoloop.browser.chrome_restart",
    )
    assert config.browser.attach_oversized_diff is True
    assert config.migration_notices == (), "an unused section is not a retired key"


def test_an_unknown_key_in_that_section_is_still_refused(tmp_path):
    """Ignored is not UNVALIDATED. A section that accepted anything would let a
    typo'd setting read as configured — the property `load_config`'s whole
    strict-by-design docstring rests on, and one this must not trade away for
    compatibility."""
    with pytest.raises(ConfigError, match=r"unknown keys in \[browser\]"):
        load_config(write_config(tmp_path, '[browser]\nconversaton_url = "typo"'))


def test_a_malformed_restart_command_in_that_section_is_still_refused(tmp_path):
    with pytest.raises(ConfigError, match="list of strings"):
        load_config(
            write_config(tmp_path, '[browser]\nrestart_command = "restart.sh"')
        )


# ---- 3. a [conversation] section naming the retired provider ------------------


def test_a_config_still_naming_the_retired_provider_loads_and_says_so(tmp_path):
    """Not a hard refusal, for the reason every other retired key here is not:
    the live `.autoloop/config.toml` is not in this repository, so refusing at
    load would break `status`, `doctor` and every recovery command on an
    unmigrated deployment — taking away the tooling needed to fix it."""
    config = load_config(
        write_config(tmp_path, f'[conversation]\nprovider = "{RETIRED_BROWSER_PROVIDER}"')
    )

    # NOT rewritten: there is no neutral provider value to migrate to.
    assert config.conversation.provider == RETIRED_BROWSER_PROVIDER
    [notice] = config.migration_notices
    assert RETIRED_BROWSER_PROVIDER in notice
    assert "RETIRED" in notice
    assert "codex_cli" in notice, "the notice says what to set instead"


def test_a_retired_fallback_is_read_as_no_failover_and_says_so(tmp_path):
    """Neutralised rather than left as written, because nothing validates a
    fallback name until the handover happens: `_handle_quota_exhausted` does not
    consult the registry, so an exhausted allowance would switch the reviewer
    role to an unregistered transport and only then fail to build it. `""` is
    the documented value for "park instead", a path that already works."""
    config = load_config(
        write_config(
            tmp_path,
            "[conversation]\n"
            'provider = "codex_cli"\n'
            f'fallback_provider = "{RETIRED_BROWSER_PROVIDER}"',
        )
    )

    assert config.conversation.provider == "codex_cli"
    assert config.conversation.fallback_provider == ""
    [notice] = config.migration_notices
    assert RETIRED_BROWSER_PROVIDER in notice
    assert "failover disabled" in notice


def test_naming_it_in_both_keys_produces_both_notices(tmp_path):
    config = load_config(
        write_config(
            tmp_path,
            "[conversation]\n"
            f'provider = "{RETIRED_BROWSER_PROVIDER}"\n'
            f'fallback_provider = "{RETIRED_BROWSER_PROVIDER}"',
        )
    )

    assert len(config.migration_notices) == 2
    assert config.conversation.fallback_provider == ""


def test_a_healthy_conversation_section_produces_no_notice(tmp_path):
    """The bound on the two tests above: the migration must fire on the retired
    NAME, never on the key being present."""
    config = load_config(
        write_config(
            tmp_path,
            "[conversation]\n"
            'provider = "codex_app_server"\n'
            'fallback_provider = "codex_cli"',
        )
    )

    assert config.conversation.fallback_provider == "codex_cli"
    assert config.migration_notices == ()


# ---- 3b. the live-CDP probe is unreachable on a default run -------------------


def test_a_default_run_never_probes_the_cdp_endpoint(tmp_path):
    """The fail-open the removed conftest fixture used to cover, closed at the
    source instead.

    `Orchestrator._attachable_page_targets` dials `browser.cdp_url` — a real
    Chrome on 127.0.0.1:9222 on a developer machine — and it is reached only
    from `_handle_rate_limited`'s browser arm, which asks
    `conversation.transport_is_browser_backed` first. With no browser-backed
    provider registered that arm is unreachable, which is WHY the autouse
    fixture could go. Asserted by making the probe explode: a run that reaches
    it fails loudly here rather than quietly opening a socket on whoever's
    machine runs the suite.
    """
    from autoloop.errors import RateLimitedError
    from autoloop.state import Phase

    from test_orchestrator import build  # noqa: E402 - see conftest sys.path

    # The PRODUCTION default, named through the dataclass rather than spelled
    # here, so this test moves with it. `test_orchestrator.build` defaults to a
    # browser-backed fake of its own (its transport-fault tests need one), which
    # is exactly what must be overridden to ask this question.
    orch, _, _, _, _, _, _ = build(tmp_path, provider=ConversationConfig().provider)
    assert orch._config.conversation.provider == "codex_cli"
    assert orch._transport_is_browser_backed() is False

    def explode():
        raise AssertionError("a default run must never dial the CDP endpoint")

    orch._attachable_page_targets = explode
    orch._sleep = lambda seconds: None
    orch.state.phase = Phase.AWAITING.value

    orch._handle_rate_limited(Phase.AWAITING, RateLimitedError("throttled"))

    assert orch.state.rate_limit_backoffs == 1, "it still backs off, just blindly"
    assert orch.state.phase == Phase.AWAITING.value


# ---- 4. the test-selection edge this change exists to cut ---------------------


def test_the_tests_conftest_does_not_import_autoloop():
    """The saving, pinned where it can regress.

    pytest applies a conftest to its whole directory tree, so one import here is
    an import every test under `autoloop/tests/` has, and
    `validation.select_validation_commands` reads exactly that graph. While this
    file imported `autoloop.orchestrator` — for one autouse fixture stubbing a
    live-CDP probe — a change to almost any module selected the ENTIRE tree (92
    test files for a change to `autoloop/dashboard.py`, measured 2026-08-25).

    Asserted on the SOURCE rather than on the module object on purpose: a
    string-based `monkeypatch.setattr("autoloop.orchestrator...")` would keep
    the runtime dependency while hiding it from the graph, which is the one
    rewrite that must not be mistaken for a fix.
    """
    source = (Path(__file__).resolve().parent / "conftest.py").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    # The docstring names the module it used to import; strip it before looking.
    body = code.split('"""')[-1]
    assert "import autoloop" not in body
    assert "from autoloop" not in body
    assert "autoloop." not in body
