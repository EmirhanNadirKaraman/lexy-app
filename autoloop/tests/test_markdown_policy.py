"""Markdown-only enforcement for the audit executor."""

import pytest

from autoloop.audit.markdown import MarkdownPolicy
from autoloop.errors import AuditError


@pytest.fixture
def policy(tmp_path):
    (tmp_path / "docs").mkdir()
    return MarkdownPolicy(tmp_path)


def test_canonical_markdown_allowed(policy, tmp_path):
    policy.write("docs/TESTS.md", "# updated")
    assert (tmp_path / "docs/TESTS.md").read_text() == "# updated"
    assert policy.written == ["docs/TESTS.md"]


def test_dated_report_allowed_once(policy, tmp_path):
    policy.write("docs/AUDIT_2026-07-29.md", "# report")
    assert (tmp_path / "docs/AUDIT_2026-07-29.md").exists()
    with pytest.raises(AuditError) as excinfo:
        policy.write("docs/AUDIT_2026-07-30.md", "# second new file")
    assert "at most ONE" in str(excinfo.value)


def test_rewriting_the_same_report_is_not_a_second_file(policy):
    policy.write("docs/AUDIT_2026-07-29.md", "# v1")
    with pytest.raises(AuditError):
        policy.write("docs/AUDIT_2026-07-30.md", "# other date")


def test_production_code_refused(policy, tmp_path):
    with pytest.raises(AuditError) as excinfo:
        policy.write("lexy-app/backend/main.py", "print('nope')")
    assert "Markdown only" in str(excinfo.value)
    assert not (tmp_path / "lexy-app").exists()


def test_non_canonical_markdown_refused(policy):
    with pytest.raises(AuditError):
        policy.write("README.md", "# not on the allowlist")


def test_traversal_refused(policy):
    with pytest.raises(AuditError):
        policy.write("../outside.md", "# nope")


def test_absolute_path_refused(policy):
    with pytest.raises(AuditError):
        policy.write("/etc/evil.md", "# nope")
