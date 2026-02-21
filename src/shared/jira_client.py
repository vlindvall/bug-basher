import base64
import logging
from types import TracebackType

import httpx

from shared.config import JiraConfig
from shared.models import BugReport

logger = logging.getLogger(__name__)


class JiraClientError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"JIRA API error {status_code}: {detail}")


class JiraClient:
    def __init__(self, config: JiraConfig | None = None) -> None:
        self._config = config or JiraConfig()
        self._client: httpx.AsyncClient | None = None

    def _auth_header(self) -> str:
        credentials = f"{self._config.email}:{self._config.api_token}"
        encoded = base64.b64encode(credentials.encode()).decode()
        return f"Basic {encoded}"

    async def __aenter__(self) -> "JiraClient":
        self._client = httpx.AsyncClient(
            base_url=self._config.base_url,
            headers={
                "Authorization": self._auth_header(),
                "Accept": "application/json",
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
                "JiraClient must be used as an async context manager"
            )
        return self._client

    async def get_issue(self, issue_key: str) -> BugReport:
        client = self._ensure_open()
        response = await client.get(
            f"/rest/api/3/issue/{issue_key}",
            params={"fields": "summary,description,labels,priority,reporter,components,created"},
        )
        if response.status_code != 200:
            raise JiraClientError(
                status_code=response.status_code,
                detail=response.text,
            )

        data = response.json()
        fields = data["fields"]

        return BugReport(
            jira_key=data["key"],
            summary=fields.get("summary", ""),
            description=_extract_description(fields.get("description")),
            labels=fields.get("labels", []),
            priority=_extract_priority(fields.get("priority")),
            reporter=_extract_reporter(fields.get("reporter")),
            components=[c["name"] for c in fields.get("components", [])],
            created=fields.get("created"),
            url=f"{self._config.base_url}/browse/{data['key']}",
        )

    async def add_comment(self, issue_key: str, body: dict) -> None:
        """Post a comment in ADF format to a JIRA issue."""
        client = self._ensure_open()
        response = await client.post(
            f"/rest/api/3/issue/{issue_key}/comment",
            json={"body": body},
            headers={"Content-Type": "application/json"},
        )
        if response.status_code not in (200, 201):
            raise JiraClientError(
                status_code=response.status_code,
                detail=response.text,
            )


def _extract_description(description: object) -> str:
    """Extract plain text from Atlassian Document Format (ADF) or plain string."""
    if description is None:
        return ""
    if isinstance(description, str):
        return description
    if isinstance(description, dict):
        return _adf_to_text(description)
    return ""


def _adf_to_text(node: dict) -> str:
    """Recursively extract text from an ADF document."""
    if node.get("type") == "text":
        return node.get("text", "")
    parts: list[str] = []
    for child in node.get("content", []):
        parts.append(_adf_to_text(child))
    return " ".join(parts).strip()


def _extract_priority(priority: object) -> str:
    if isinstance(priority, dict):
        return priority.get("name", "P3")
    return "P3"


def _extract_reporter(reporter: object) -> str | None:
    if isinstance(reporter, dict):
        return reporter.get("displayName") or reporter.get("emailAddress")
    return None


def build_adf_document(sections: list[tuple[str, str]]) -> dict:
    """Convert (heading, body_text) tuples to an ADF document.

    Each tuple produces a heading node followed by a paragraph node.
    """
    content: list[dict] = []
    for heading, body_text in sections:
        content.append(
            {
                "type": "heading",
                "attrs": {"level": 3},
                "content": [{"type": "text", "text": heading}],
            }
        )
        if body_text:
            content.append(
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": body_text}],
                }
            )
    return {"type": "doc", "version": 1, "content": content}
