"""Source-control server tests: fully offline via httpx mocking."""

import pytest
import respx
from httpx import Response

from ops_copilot.mcp.servers import source_control as sc


def call(tool, *args, **kwargs):
    """FastMCP wraps functions in FunctionTool; .fn is the plain callable."""
    return tool.fn(*args, **kwargs)


@pytest.fixture(autouse=True)
def clean_cache():
    sc._cache.clear()
    yield
    sc._cache.clear()


@pytest.fixture()
def github_mock():
    with respx.mock(base_url=sc.GITHUB_API) as mock:
        yield mock


class TestRepoTools:
    @respx.mock
    async def test_repo_overview(self, github_mock):
        github_mock.get("/repos/acme/widgets").mock(
            return_value=Response(
                200,
                json={
                    "full_name": "acme/widgets",
                    "description": "test repo",
                    "stargazers_count": 42,
                    "open_issues_count": 7,
                    "language": "Python",
                    "default_branch": "main",
                    "pushed_at": "2026-01-01T00:00:00Z",
                },
            )
        )
        data = call(sc.repo_overview, "acme/widgets")
        assert data["stars"] == 42
        assert data["default_branch"] == "main"

    async def test_bad_repo_name(self):
        with pytest.raises(ValueError, match="owner/name"):
            call(sc.repo_overview, "not-a-repo")

    @respx.mock
    async def test_issue_thread_masks_pii(self, github_mock):
        github_mock.get("/repos/acme/widgets/issues/9").mock(
            return_value=Response(
                200,
                json={
                    "title": "Login broken",
                    "state": "open",
                    "user": {"login": "reporter1"},
                    "labels": [],
                    "body": "email me at victim@example.com for details",
                },
            )
        )
        github_mock.get("/repos/acme/widgets/issues/9/comments").mock(
            return_value=Response(
                200, json=[{"user": {"login": "dev2"}, "body": "call 415-555-0100 to verify"}]
            )
        )
        result = call(sc.issue_thread, "acme/widgets", 9)
        assert "victim@example.com" not in result["body"]
        assert all("415-555-0100" not in c["body"] for c in result["comments"])

    async def test_draft_comment_rejects_empty(self):
        with pytest.raises(ValueError, match="empty"):
            call(sc.draft_pr_comment, "acme/widgets", 5, "   ")

    @respx.mock
    async def test_rate_limit_surfaces_useful_error(self, github_mock):
        github_mock.get("/repos/limited/repo").mock(
            return_value=Response(
                403,
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0"},
                json={"message": "rate limited"},
            )
        )
        with pytest.raises(ValueError, match=r"(?i)rate limit"):
            call(sc.repo_overview, "limited/repo")

    @respx.mock
    async def test_cache_hit_avoids_second_request(self, github_mock):
        route = github_mock.get("/repos/cached/repo").mock(
            return_value=Response(
                200,
                json={
                    "full_name": "cached/repo",
                    "description": None,
                    "stargazers_count": 1,
                    "open_issues_count": 0,
                    "language": None,
                    "default_branch": "main",
                    "pushed_at": "",
                },
            )
        )
        call(sc.repo_overview, "cached/repo")
        call(sc.repo_overview, "cached/repo")
        assert route.call_count == 1

    def test_post_comment_fails_closed_without_secret(self, monkeypatch):
        monkeypatch.delenv("OPS_APPROVAL_SECRET", raising=False)
        with pytest.raises(Exception, match="OPS_APPROVAL_SECRET"):
            call(sc.post_comment, "acme/widgets", 3, "hello", approval_token="x")
