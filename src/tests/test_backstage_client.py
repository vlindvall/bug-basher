import json

import httpx
import pytest
import respx

from shared.backstage_client import BackstageClient, BackstageClientError, BackstageDataSourceError
from shared.config import BackstageConfig
from tests.conftest import make_entity


@pytest.fixture
def config(backstage_config: BackstageConfig) -> BackstageConfig:
    return backstage_config


async def test_filters_to_production_only(config: BackstageConfig) -> None:
    entities = [
        make_entity(name="prod-svc", lifecycle="production"),
        make_entity(name="deprecated-svc", lifecycle="deprecated"),
        make_entity(name="experimental-svc", lifecycle="experimental"),
    ]
    with respx.mock:
        respx.get(f"{config.base_url}/entities").mock(
            return_value=httpx.Response(200, json=entities)
        )
        async with BackstageClient(config) as client:
            repos = await client.get_repositories()

    assert len(repos) == 1
    assert repos[0].name == "prod-svc"


async def test_transforms_entity_to_repository(config: BackstageConfig) -> None:
    entity = make_entity(
        name="checkout",
        slug="example-org/checkout",
        description="Checkout service",
        component_type="service",
        lifecycle="production",
        owner="group:team",
        system="buy-system",
        tags=["python", "fastapi"],
    )
    with respx.mock:
        respx.get(f"{config.base_url}/entities").mock(
            return_value=httpx.Response(200, json=[entity])
        )
        async with BackstageClient(config) as client:
            repos = await client.get_repositories()

    repo = repos[0]
    assert repo.name == "checkout"
    assert repo.github_slug == "example-org/checkout"
    assert repo.description == "Checkout service"
    assert repo.component_type == "service"
    assert repo.lifecycle == "production"
    assert repo.owner == "group:team"
    assert repo.system == "buy-system"
    assert repo.tags == ["python", "fastapi"]


async def test_handles_missing_optional_fields(config: BackstageConfig) -> None:
    entity = make_entity(
        name="minimal",
        slug=None,
        description=None,
        system=None,
        lifecycle="production",
    )
    with respx.mock:
        respx.get(f"{config.base_url}/entities").mock(
            return_value=httpx.Response(200, json=[entity])
        )
        async with BackstageClient(config) as client:
            repos = await client.get_repositories()

    repo = repos[0]
    assert repo.name == "minimal"
    assert repo.github_slug is None
    assert repo.description is None
    assert repo.system is None


async def test_caches_results(config: BackstageConfig) -> None:
    entities = [make_entity(lifecycle="production")]
    with respx.mock:
        route = respx.get(f"{config.base_url}/entities").mock(
            return_value=httpx.Response(200, json=entities)
        )
        async with BackstageClient(config) as client:
            first = await client.get_repositories()
            second = await client.get_repositories()

    assert first == second
    assert route.call_count == 1


async def test_cache_expires_with_zero_ttl(config: BackstageConfig) -> None:
    config.cache_ttl_seconds = 0
    entities = [make_entity(lifecycle="production")]
    with respx.mock:
        route = respx.get(f"{config.base_url}/entities").mock(
            return_value=httpx.Response(200, json=entities)
        )
        async with BackstageClient(config) as client:
            await client.get_repositories()
            await client.get_repositories()

    assert route.call_count == 2


async def test_bypass_cache(config: BackstageConfig) -> None:
    entities = [make_entity(lifecycle="production")]
    with respx.mock:
        route = respx.get(f"{config.base_url}/entities").mock(
            return_value=httpx.Response(200, json=entities)
        )
        async with BackstageClient(config) as client:
            await client.get_repositories()
            await client.get_repositories(bypass_cache=True)

    assert route.call_count == 2


async def test_api_500_raises_error(config: BackstageConfig) -> None:
    with respx.mock:
        respx.get(f"{config.base_url}/entities").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        async with BackstageClient(config) as client:
            with pytest.raises(BackstageClientError) as exc_info:
                await client.get_repositories()

    assert exc_info.value.status_code == 500


async def test_api_401_raises_error(config: BackstageConfig) -> None:
    with respx.mock:
        respx.get(f"{config.base_url}/entities").mock(
            return_value=httpx.Response(401, text="Unauthorized")
        )
        async with BackstageClient(config) as client:
            with pytest.raises(BackstageClientError) as exc_info:
                await client.get_repositories()

    assert exc_info.value.status_code == 401


async def test_sends_correct_filter_params(config: BackstageConfig) -> None:
    with respx.mock:
        route = respx.get(f"{config.base_url}/entities").mock(
            return_value=httpx.Response(200, json=[])
        )
        async with BackstageClient(config) as client:
            await client.get_repositories()

    request = route.calls[0].request
    params = httpx.QueryParams(request.url.params).multi_items()
    assert ("filter", "kind=Component") in params
    assert ("filter", "spec.owner=group:team") in params


async def test_sends_auth_header(config: BackstageConfig) -> None:
    with respx.mock:
        route = respx.get(f"{config.base_url}/entities").mock(
            return_value=httpx.Response(200, json=[])
        )
        async with BackstageClient(config) as client:
            await client.get_repositories()

    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer test-token"


async def test_outside_context_manager_raises() -> None:
    client = BackstageClient()
    with pytest.raises(RuntimeError, match="context manager"):
        await client.get_repositories()


async def test_case_insensitive_lifecycle_filter(config: BackstageConfig) -> None:
    entities = [
        make_entity(name="upper", lifecycle="Production"),
        make_entity(name="lower", lifecycle="production"),
        make_entity(name="mixed", lifecycle="PRODUCTION"),
    ]
    with respx.mock:
        respx.get(f"{config.base_url}/entities").mock(
            return_value=httpx.Response(200, json=entities)
        )
        async with BackstageClient(config) as client:
            repos = await client.get_repositories()

    assert len(repos) == 3
    names = {r.name for r in repos}
    assert names == {"upper", "lower", "mixed"}


async def test_loads_repositories_from_local_file(tmp_path) -> None:
    local_file = tmp_path / "repos.json"
    entities = [
        make_entity(name="prod-svc", lifecycle="production"),
        make_entity(name="non-prod", lifecycle="experimental"),
    ]
    local_file.write_text(json.dumps(entities))

    config = BackstageConfig(
        use_local_file=True,
        local_file_path=str(local_file),
    )

    async with BackstageClient(config) as client:
        repos = await client.get_repositories()

    assert len(repos) == 1
    assert repos[0].name == "prod-svc"


async def test_local_file_missing_raises_error(tmp_path) -> None:
    config = BackstageConfig(
        use_local_file=True,
        local_file_path=str(tmp_path / "missing.json"),
    )

    async with BackstageClient(config) as client:
        with pytest.raises(BackstageDataSourceError, match="Backstage local file not found"):
            await client.get_repositories()


async def test_local_file_invalid_json_raises_error(tmp_path) -> None:
    local_file = tmp_path / "invalid.json"
    local_file.write_text("{not valid json")

    config = BackstageConfig(
        use_local_file=True,
        local_file_path=str(local_file),
    )

    async with BackstageClient(config) as client:
        with pytest.raises(BackstageDataSourceError, match="Invalid JSON in Backstage local file"):
            await client.get_repositories()
