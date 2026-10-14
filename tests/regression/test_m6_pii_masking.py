"""Regression tests for M6: PII masking must cover samples and titles.

The module docstring claims scraped content is masked, but on the original
code profile_column returns raw samples and issue_thread / pull_request_diff
return raw titles. The fixed behavior routes those through mask_pii.

The mask_pii primitive itself is pinned first (dependency-free), then the
wiring is tested with stubbed I/O: _get is monkeypatched for the GitHub
tools and connect_readonly for profile_column. No network, no database.
"""

from types import SimpleNamespace

from ops_copilot.guardrails import mask_pii
from ops_copilot.mcp.servers import ops_db, source_control

EMAIL = "jane.doe@corp.example"
PHONE = "+1 415-555-1234"


class TestMaskPiiPrimitive:
    def test_email_masked(self):
        out = mask_pii(f"contact {EMAIL} about it")
        assert EMAIL not in out
        assert "[redacted]" in out

    def test_phone_masked(self):
        out = mask_pii(f"call {PHONE} now")
        assert "415-555-1234" not in out
        assert "[redacted]" in out

    def test_clean_text_untouched(self):
        out = mask_pii("nothing sensitive here")
        assert out == "nothing sensitive here"


class _FakeCursor:
    def __init__(self, cols, rows):
        self.description = [SimpleNamespace(name=c) for c in cols]
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, results):
        self._results = list(results)

    def execute(self, sql, params=None):
        cols, rows = self._results.pop(0)
        return _FakeCursor(cols, rows)


def _fake_issue_get(path, params=None):
    if path.endswith("/comments"):
        return []
    return {
        "title": f"Login broken for {EMAIL}",
        "state": "open",
        "user": {"login": "reporter"},
        "labels": [],
        "body": "stack trace attached",
    }


def test_issue_thread_title_is_masked(monkeypatch):
    monkeypatch.setattr(source_control, "_get", _fake_issue_get)
    result = source_control.issue_thread.fn(repo="owner/name", number=1)
    assert EMAIL not in result["title"], f"issue title leaked PII: {result['title']!r}"
    assert "[redacted]" in result["title"]


def _fake_pr_get(path, params=None):
    if path.endswith("/files"):
        return []
    return {
        "title": f"Fix the {PHONE} hotline routing",
        "state": "open",
        "user": {"login": "dev"},
        "base": {"ref": "main"},
        "head": {"ref": "fix-branch"},
    }


def test_pull_request_diff_title_is_masked(monkeypatch):
    monkeypatch.setattr(source_control, "_get", _fake_pr_get)
    result = source_control.pull_request_diff.fn(repo="owner/name", number=2)
    assert "415-555-1234" not in result["title"], f"PR title leaked PII: {result['title']!r}"
    assert "[redacted]" in result["title"]


def test_profile_column_samples_are_masked(monkeypatch):
    stats = (
        ["total", "nulls", "distinct_values", "min_value", "max_value"],
        [(10, 0, 3, "a", "z")],
    )
    samples = (
        ["value"],
        [(EMAIL,), (PHONE,), ("perfectly ordinary note",)],
    )
    monkeypatch.setattr(ops_db, "connect_readonly", lambda: _FakeConn([stats, samples]))
    result = ops_db.profile_column.fn("scratch_notes", "note")
    values = [row["value"] for row in result["sample"]]
    assert EMAIL not in values, f"column sample leaked PII: {values!r}"
    assert not any("415-555-1234" in v for v in values), f"column sample leaked PII: {values!r}"
    assert any("[redacted]" in v for v in values)
