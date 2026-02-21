import base64

import httpx
import pytest
import respx

from shared.github_client import GitHubClient, GitHubClientError


class TestGitHubClient:
    @respx.mock
    async def test_get_default_branch(self, github_config):
        respx.get("https://api.github.com/repos/org/repo").mock(
            return_value=httpx.Response(200, json={"default_branch": "main"})
        )
        async with GitHubClient(github_config) as client:
            branch = await client.get_default_branch("org", "repo")
        assert branch == "main"

    @respx.mock
    async def test_get_branch_sha(self, github_config):
        respx.get("https://api.github.com/repos/org/repo/git/ref/heads/main").mock(
            return_value=httpx.Response(
                200, json={"object": {"sha": "abc123"}}
            )
        )
        async with GitHubClient(github_config) as client:
            sha = await client.get_branch_sha("org", "repo", "main")
        assert sha == "abc123"

    @respx.mock
    async def test_create_branch(self, github_config):
        route = respx.post("https://api.github.com/repos/org/repo/git/refs").mock(
            return_value=httpx.Response(201, json={})
        )
        async with GitHubClient(github_config) as client:
            await client.create_branch("org", "repo", "feature", "abc123")

        request = route.calls.last.request
        import json

        body = json.loads(request.content)
        assert body["ref"] == "refs/heads/feature"
        assert body["sha"] == "abc123"

    @respx.mock
    async def test_get_file_content(self, github_config):
        content = base64.b64encode(b"hello world").decode()
        respx.get("https://api.github.com/repos/org/repo/contents/src/main.py").mock(
            return_value=httpx.Response(
                200, json={"content": content, "sha": "file-sha-123"}
            )
        )
        async with GitHubClient(github_config) as client:
            text, sha = await client.get_file_content(
                "org", "repo", "src/main.py", "main"
            )
        assert text == "hello world"
        assert sha == "file-sha-123"

    @respx.mock
    async def test_get_file_content_404(self, github_config):
        respx.get("https://api.github.com/repos/org/repo/contents/missing.py").mock(
            return_value=httpx.Response(404, json={"message": "Not Found"})
        )
        async with GitHubClient(github_config) as client:
            with pytest.raises(GitHubClientError) as exc_info:
                await client.get_file_content("org", "repo", "missing.py", "main")
            assert exc_info.value.status_code == 404

    @respx.mock
    async def test_update_file_existing(self, github_config):
        route = respx.put("https://api.github.com/repos/org/repo/contents/src/main.py").mock(
            return_value=httpx.Response(200, json={})
        )
        async with GitHubClient(github_config) as client:
            await client.update_file(
                "org", "repo", "src/main.py", "new content", "fix: update", "feature", sha="old-sha"
            )

        import json

        body = json.loads(route.calls.last.request.content)
        assert body["sha"] == "old-sha"
        assert body["branch"] == "feature"
        decoded = base64.b64decode(body["content"]).decode()
        assert decoded == "new content"

    @respx.mock
    async def test_update_file_new(self, github_config):
        route = respx.put("https://api.github.com/repos/org/repo/contents/new.py").mock(
            return_value=httpx.Response(201, json={})
        )
        async with GitHubClient(github_config) as client:
            await client.update_file(
                "org", "repo", "new.py", "content", "add: new file", "feature"
            )

        import json

        body = json.loads(route.calls.last.request.content)
        assert "sha" not in body

    @respx.mock
    async def test_create_pull_request(self, github_config):
        pr_response = {"html_url": "https://github.com/org/repo/pull/1", "number": 1}
        respx.post("https://api.github.com/repos/org/repo/pulls").mock(
            return_value=httpx.Response(201, json=pr_response)
        )
        async with GitHubClient(github_config) as client:
            result = await client.create_pull_request(
                "org", "repo", "title", "body", "feature", "main"
            )
        assert result["html_url"] == "https://github.com/org/repo/pull/1"

    @respx.mock
    async def test_sends_auth_header(self, github_config):
        route = respx.get("https://api.github.com/repos/org/repo").mock(
            return_value=httpx.Response(200, json={"default_branch": "main"})
        )
        async with GitHubClient(github_config) as client:
            await client.get_default_branch("org", "repo")

        request = route.calls.last.request
        assert request.headers["authorization"] == "Bearer test-gh-token"

    async def test_outside_context_manager_raises(self, github_config):
        client = GitHubClient(github_config)
        with pytest.raises(RuntimeError, match="context manager"):
            await client.get_default_branch("org", "repo")

    @respx.mock
    async def test_api_error_raises(self, github_config):
        respx.get("https://api.github.com/repos/org/repo").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        async with GitHubClient(github_config) as client:
            with pytest.raises(GitHubClientError) as exc_info:
                await client.get_default_branch("org", "repo")
            assert exc_info.value.status_code == 500
