from shared.cli import _filter_repositories
from shared.config import BackstageConfig
from shared.models import Repository


def test_filter_repositories_by_team_owner():
    repos = [
        Repository(name="svc-a", owner="group:team-core"),
        Repository(name="svc-b", owner="group:payments"),
    ]
    config = BackstageConfig(team="team-core", default_github_slugs=[])
    filtered = _filter_repositories(repos, config)
    assert [r.name for r in filtered] == ["svc-a"]


def test_filter_repositories_by_slug_tokens():
    repos = [
        Repository(name="svc-a", github_slug="example-org/checkout"),
        Repository(name="svc-b", github_slug="example-org/payments"),
    ]
    config = BackstageConfig(team="", default_github_slugs=["checkout"])
    filtered = _filter_repositories(repos, config)
    assert [r.name for r in filtered] == ["svc-a"]


def test_filter_repositories_returns_all_when_no_filters():
    repos = [
        Repository(name="svc-a", owner="group:one"),
        Repository(name="svc-b", owner="group:two"),
    ]
    config = BackstageConfig(team="", default_github_slugs=[])
    filtered = _filter_repositories(repos, config)
    assert [r.name for r in filtered] == ["svc-a", "svc-b"]
