import base64
import logging
from types import TracebackType

import httpx

from shared.config import GitHubConfig

logger = logging.getLogger(__name__)


class GitHubClientError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"GitHub API error {status_code}: {detail}")


class GitHubClient:
    def __init__(self, config: GitHubConfig | None = None) -> None:
        self._config = config or GitHubConfig()
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "GitHubClient":
        self._client = httpx.AsyncClient(
            base_url="https://api.github.com",
            headers={
                "Authorization": f"Bearer {self._config.token}",
                "Accept": "application/vnd.github+json",
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
                "GitHubClient must be used as an async context manager"
            )
        return self._client

    async def get_default_branch(self, owner: str, repo: str) -> str:
        client = self._ensure_open()
        response = await client.get(f"/repos/{owner}/{repo}")
        if response.status_code != 200:
            raise GitHubClientError(response.status_code, response.text)
        return response.json()["default_branch"]

    async def get_branch_sha(self, owner: str, repo: str, branch: str) -> str:
        client = self._ensure_open()
        response = await client.get(
            f"/repos/{owner}/{repo}/git/ref/heads/{branch}"
        )
        if response.status_code != 200:
            raise GitHubClientError(response.status_code, response.text)
        return response.json()["object"]["sha"]

    async def create_branch(
        self, owner: str, repo: str, branch_name: str, from_sha: str
    ) -> None:
        client = self._ensure_open()
        response = await client.post(
            f"/repos/{owner}/{repo}/git/refs",
            json={"ref": f"refs/heads/{branch_name}", "sha": from_sha},
        )
        if response.status_code != 201:
            raise GitHubClientError(response.status_code, response.text)

    async def get_file_content(
        self, owner: str, repo: str, path: str, ref: str
    ) -> tuple[str, str]:
        """Return (decoded_content, blob_sha). Raises GitHubClientError on 404."""
        client = self._ensure_open()
        response = await client.get(
            f"/repos/{owner}/{repo}/contents/{path}",
            params={"ref": ref},
        )
        if response.status_code != 200:
            raise GitHubClientError(response.status_code, response.text)
        data = response.json()
        content = base64.b64decode(data["content"]).decode()
        return content, data["sha"]

    async def update_file(
        self,
        owner: str,
        repo: str,
        path: str,
        content: str,
        message: str,
        branch: str,
        sha: str | None = None,
    ) -> None:
        """Create or update a file. If sha is None, creates a new file."""
        client = self._ensure_open()
        encoded = base64.b64encode(content.encode()).decode()
        body: dict = {
            "message": message,
            "content": encoded,
            "branch": branch,
        }
        if sha is not None:
            body["sha"] = sha
        response = await client.put(
            f"/repos/{owner}/{repo}/contents/{path}",
            json=body,
        )
        if response.status_code not in (200, 201):
            raise GitHubClientError(response.status_code, response.text)

    async def create_pull_request(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> dict:
        client = self._ensure_open()
        response = await client.post(
            f"/repos/{owner}/{repo}/pulls",
            json={
                "title": title,
                "body": body,
                "head": head,
                "base": base,
            },
        )
        if response.status_code != 201:
            raise GitHubClientError(response.status_code, response.text)
        return response.json()
