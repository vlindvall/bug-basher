import httpx
import pytest
import respx

from shared.jira_client import (
    JiraClient,
    JiraClientError,
    _adf_to_text,
    _extract_description,
    build_adf_document,
)

# Import the test helper from conftest
from tests.conftest import make_jira_issue


class TestJiraClient:
    @respx.mock
    async def test_fetches_issue_and_returns_bug_report(self, jira_config):
        issue = make_jira_issue()
        respx.get("https://test.atlassian.net/rest/api/3/issue/BUY-1234").mock(
            return_value=httpx.Response(200, json=issue)
        )

        async with JiraClient(jira_config) as client:
            bug = await client.get_issue("BUY-1234")

        assert bug.jira_key == "BUY-1234"
        assert bug.summary == "Checkout fails for subscription items"
        assert "500 error" in bug.description
        assert bug.priority == "High"
        assert bug.reporter == "Jane Doe"
        assert bug.components == ["checkout"]
        assert bug.labels == ["team"]
        assert bug.url == "https://test.atlassian.net/browse/BUY-1234"

    @respx.mock
    async def test_sends_basic_auth_header(self, jira_config):
        issue = make_jira_issue()
        route = respx.get("https://test.atlassian.net/rest/api/3/issue/BUY-1").mock(
            return_value=httpx.Response(200, json=issue)
        )

        async with JiraClient(jira_config) as client:
            await client.get_issue("BUY-1")

        request = route.calls.last.request
        assert request.headers["authorization"].startswith("Basic ")

    @respx.mock
    async def test_requests_specific_fields(self, jira_config):
        issue = make_jira_issue()
        route = respx.get("https://test.atlassian.net/rest/api/3/issue/BUY-1").mock(
            return_value=httpx.Response(200, json=issue)
        )

        async with JiraClient(jira_config) as client:
            await client.get_issue("BUY-1")

        request = route.calls.last.request
        assert "fields=" in str(request.url)

    @respx.mock
    async def test_404_raises_error(self, jira_config):
        respx.get("https://test.atlassian.net/rest/api/3/issue/NOPE-999").mock(
            return_value=httpx.Response(404, json={"errorMessages": ["Issue does not exist"]})
        )

        async with JiraClient(jira_config) as client:
            with pytest.raises(JiraClientError) as exc_info:
                await client.get_issue("NOPE-999")
            assert exc_info.value.status_code == 404

    @respx.mock
    async def test_401_raises_error(self, jira_config):
        respx.get("https://test.atlassian.net/rest/api/3/issue/BUY-1").mock(
            return_value=httpx.Response(401, text="Unauthorized")
        )

        async with JiraClient(jira_config) as client:
            with pytest.raises(JiraClientError) as exc_info:
                await client.get_issue("BUY-1")
            assert exc_info.value.status_code == 401

    async def test_outside_context_manager_raises(self, jira_config):
        client = JiraClient(jira_config)
        with pytest.raises(RuntimeError, match="context manager"):
            await client.get_issue("BUY-1")

    @respx.mock
    async def test_handles_null_description(self, jira_config):
        issue = make_jira_issue(description=None)
        issue["fields"]["description"] = None
        respx.get("https://test.atlassian.net/rest/api/3/issue/BUY-1").mock(
            return_value=httpx.Response(200, json=issue)
        )

        async with JiraClient(jira_config) as client:
            bug = await client.get_issue("BUY-1")
        assert bug.description == ""

    @respx.mock
    async def test_handles_null_priority(self, jira_config):
        issue = make_jira_issue()
        issue["fields"]["priority"] = None
        respx.get("https://test.atlassian.net/rest/api/3/issue/BUY-1").mock(
            return_value=httpx.Response(200, json=issue)
        )

        async with JiraClient(jira_config) as client:
            bug = await client.get_issue("BUY-1")
        assert bug.priority == "P3"

    @respx.mock
    async def test_handles_null_reporter(self, jira_config):
        issue = make_jira_issue()
        issue["fields"]["reporter"] = None
        respx.get("https://test.atlassian.net/rest/api/3/issue/BUY-1").mock(
            return_value=httpx.Response(200, json=issue)
        )

        async with JiraClient(jira_config) as client:
            bug = await client.get_issue("BUY-1")
        assert bug.reporter is None

    @respx.mock
    async def test_multiple_components(self, jira_config):
        issue = make_jira_issue(components=["checkout", "cart", "payments"])
        respx.get("https://test.atlassian.net/rest/api/3/issue/BUY-1").mock(
            return_value=httpx.Response(200, json=issue)
        )

        async with JiraClient(jira_config) as client:
            bug = await client.get_issue("BUY-1")
        assert bug.components == ["checkout", "cart", "payments"]


class TestAdfToText:
    def test_simple_paragraph(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Hello world"}],
                }
            ],
        }
        assert _adf_to_text(doc) == "Hello world"

    def test_multiple_paragraphs(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "First"}],
                },
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Second"}],
                },
            ],
        }
        result = _adf_to_text(doc)
        assert "First" in result
        assert "Second" in result

    def test_nested_inline_nodes(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Hello "},
                        {"type": "text", "text": "world"},
                    ],
                }
            ],
        }
        assert "Hello" in _adf_to_text(doc)
        assert "world" in _adf_to_text(doc)

    def test_empty_document(self):
        doc = {"type": "doc", "content": []}
        assert _adf_to_text(doc) == ""


class TestExtractDescription:
    def test_none_returns_empty(self):
        assert _extract_description(None) == ""

    def test_string_passes_through(self):
        assert _extract_description("plain text") == "plain text"

    def test_adf_dict_extracted(self):
        doc = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "ADF content"}],
                }
            ],
        }
        assert _extract_description(doc) == "ADF content"

    def test_unexpected_type_returns_empty(self):
        assert _extract_description(42) == ""


class TestAddComment:
    @respx.mock
    async def test_posts_comment_with_adf_body(self, jira_config):
        route = respx.post(
            "https://test.atlassian.net/rest/api/3/issue/BUY-1234/comment"
        ).mock(return_value=httpx.Response(201, json={"id": "10001"}))

        adf = build_adf_document([("Heading", "Body text")])
        async with JiraClient(jira_config) as client:
            await client.add_comment("BUY-1234", adf)

        import json

        body = json.loads(route.calls.last.request.content)
        assert body["body"]["type"] == "doc"

    @respx.mock
    async def test_comment_sends_auth_header(self, jira_config):
        route = respx.post(
            "https://test.atlassian.net/rest/api/3/issue/BUY-1/comment"
        ).mock(return_value=httpx.Response(201, json={}))

        async with JiraClient(jira_config) as client:
            await client.add_comment("BUY-1", build_adf_document([]))

        request = route.calls.last.request
        assert request.headers["authorization"].startswith("Basic ")

    @respx.mock
    async def test_comment_404_raises(self, jira_config):
        respx.post(
            "https://test.atlassian.net/rest/api/3/issue/NOPE-1/comment"
        ).mock(return_value=httpx.Response(404, text="Not found"))

        async with JiraClient(jira_config) as client:
            with pytest.raises(JiraClientError) as exc_info:
                await client.add_comment("NOPE-1", build_adf_document([]))
            assert exc_info.value.status_code == 404

    @respx.mock
    async def test_comment_403_raises(self, jira_config):
        respx.post(
            "https://test.atlassian.net/rest/api/3/issue/BUY-1/comment"
        ).mock(return_value=httpx.Response(403, text="Forbidden"))

        async with JiraClient(jira_config) as client:
            with pytest.raises(JiraClientError) as exc_info:
                await client.add_comment("BUY-1", build_adf_document([]))
            assert exc_info.value.status_code == 403


class TestBuildAdfDocument:
    def test_heading_and_paragraph(self):
        doc = build_adf_document([("Root Cause", "Null pointer")])
        assert doc["type"] == "doc"
        assert doc["version"] == 1
        content = doc["content"]
        assert len(content) == 2
        assert content[0]["type"] == "heading"
        assert content[0]["content"][0]["text"] == "Root Cause"
        assert content[1]["type"] == "paragraph"
        assert content[1]["content"][0]["text"] == "Null pointer"

    def test_empty_sections(self):
        doc = build_adf_document([])
        assert doc["type"] == "doc"
        assert doc["content"] == []
