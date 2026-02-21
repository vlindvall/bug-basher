import logging
from types import TracebackType

import httpx

from shared.config import SlackConfig

logger = logging.getLogger(__name__)


class SlackClientError(Exception):
    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"Slack API error: {detail}")


class SlackClient:
    def __init__(self, config: SlackConfig | None = None) -> None:
        self._config = config or SlackConfig()
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "SlackClient":
        self._client = httpx.AsyncClient(
            base_url="https://slack.com/api",
            headers={
                "Authorization": f"Bearer {self._config.bot_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            timeout=30.0,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _ensure_open(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError(
                "SlackClient must be used as an async context manager"
            )
        return self._client

    async def post_message(
        self,
        channel: str,
        text: str,
        blocks: list[dict] | None = None,
    ) -> dict:
        client = self._ensure_open()
        body: dict = {"channel": channel, "text": text}
        if blocks is not None:
            body["blocks"] = blocks

        response = await client.post("/chat.postMessage", json=body)
        if response.status_code != 200:
            raise SlackClientError(
                f"HTTP {response.status_code}: {response.text}"
            )

        data = response.json()
        if not data.get("ok"):
            raise SlackClientError(data.get("error", "unknown error"))

        return data
