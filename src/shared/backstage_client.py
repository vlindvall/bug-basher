import logging
import json
import time
from types import TracebackType
from pathlib import Path

import httpx

from shared.config import BackstageConfig
from shared.models import BackstageEntity, Repository

logger = logging.getLogger(__name__)


class BackstageClientError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Backstage API error {status_code}: {detail}")


class BackstageDataSourceError(Exception):
    pass


class _CacheEntry:
    def __init__(self, repos: list[Repository], timestamp: float) -> None:
        self.repos = repos
        self.timestamp = timestamp


class BackstageClient:
    def __init__(self, config: BackstageConfig | None = None) -> None:
        self._config = config or BackstageConfig()
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[str, _CacheEntry] = {}

    async def __aenter__(self) -> "BackstageClient":
        if not self._config.use_local_file:
            self._client = httpx.AsyncClient(
                base_url=self._config.base_url,
                headers={"Authorization": f"Bearer {self._config.token}"},
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
                "BackstageClient must be used as an async context manager"
            )
        return self._client

    async def get_repositories(
        self,
        owner_group: str | None = None,
        *,
        bypass_cache: bool = False,
    ) -> list[Repository]:
        owner = owner_group or self._config.owner_group
        now = time.monotonic()

        if not bypass_cache:
            entry = self._cache.get(owner)
            if entry and (now - entry.timestamp) < self._config.cache_ttl_seconds:
                logger.debug("Cache hit for owner=%s", owner)
                return entry.repos

        if self._config.use_local_file:
            entities = self._load_entities_from_file()
        else:
            entities = await self._fetch_entities(owner)
        all_repos = [Repository.from_entity(e) for e in entities]

        production = [
            r for r in all_repos if (r.lifecycle or "").lower() == "production"
        ]
        excluded = len(all_repos) - len(production)
        if excluded:
            logger.info(
                "Filtered %d non-production entities for owner=%s", excluded, owner
            )

        self._cache[owner] = _CacheEntry(production, time.monotonic())
        return production

    def _load_entities_from_file(self) -> list[BackstageEntity]:
        path = Path(self._config.local_file_path).expanduser()
        if not path.is_file():
            raise BackstageDataSourceError(f"Backstage local file not found: {path}")

        try:
            raw = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise BackstageDataSourceError(
                f"Invalid JSON in Backstage local file {path}: {e}"
            ) from e

        if not isinstance(raw, list):
            raise BackstageDataSourceError(
                f"Backstage local file must contain a JSON array: {path}"
            )

        return [BackstageEntity.model_validate(item) for item in raw]

    async def _fetch_entities(self, owner: str) -> list[BackstageEntity]:
        client = self._ensure_open()
        response = await client.get(
            "/entities",
            params=[
                ("filter", "kind=Component"),
                ("filter", f"spec.owner=group:{owner}"),
            ],
        )
        if response.status_code != 200:
            raise BackstageClientError(
                status_code=response.status_code,
                detail=response.text,
            )
        return [BackstageEntity.model_validate(item) for item in response.json()]
