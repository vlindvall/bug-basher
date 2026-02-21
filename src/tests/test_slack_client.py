import httpx
import pytest
import respx

from shared.slack_client import SlackClient, SlackClientError


class TestSlackClient:
    @respx.mock
    async def test_post_text_only(self, slack_config):
        respx.post("https://slack.com/api/chat.postMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "ts": "1234"})
        )
        async with SlackClient(slack_config) as client:
            result = await client.post_message("#channel", "hello")
        assert result["ok"] is True

    @respx.mock
    async def test_post_with_blocks(self, slack_config):
        route = respx.post("https://slack.com/api/chat.postMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]
        async with SlackClient(slack_config) as client:
            await client.post_message("#channel", "fallback", blocks=blocks)

        import json

        body = json.loads(route.calls.last.request.content)
        assert body["blocks"] == blocks

    @respx.mock
    async def test_sends_auth_header(self, slack_config):
        route = respx.post("https://slack.com/api/chat.postMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        async with SlackClient(slack_config) as client:
            await client.post_message("#channel", "hello")

        request = route.calls.last.request
        assert request.headers["authorization"] == "Bearer xoxb-test-token"

    @respx.mock
    async def test_http_error_raises(self, slack_config):
        respx.post("https://slack.com/api/chat.postMessage").mock(
            return_value=httpx.Response(500, text="Server Error")
        )
        async with SlackClient(slack_config) as client:
            with pytest.raises(SlackClientError, match="HTTP 500"):
                await client.post_message("#channel", "hello")

    @respx.mock
    async def test_ok_false_raises(self, slack_config):
        respx.post("https://slack.com/api/chat.postMessage").mock(
            return_value=httpx.Response(
                200, json={"ok": False, "error": "channel_not_found"}
            )
        )
        async with SlackClient(slack_config) as client:
            with pytest.raises(SlackClientError, match="channel_not_found"):
                await client.post_message("#channel", "hello")

    async def test_outside_context_manager_raises(self, slack_config):
        client = SlackClient(slack_config)
        with pytest.raises(RuntimeError, match="context manager"):
            await client.post_message("#channel", "hello")

    @respx.mock
    async def test_user_id_as_channel(self, slack_config):
        route = respx.post("https://slack.com/api/chat.postMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        async with SlackClient(slack_config) as client:
            await client.post_message("U12345678", "DM message")

        import json

        body = json.loads(route.calls.last.request.content)
        assert body["channel"] == "U12345678"
