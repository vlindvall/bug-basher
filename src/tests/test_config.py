from shared.config import BackstageConfig, GitHubConfig, JiraConfig


def test_backstage_config_from_env(monkeypatch):
    monkeypatch.setenv("BACKSTAGE_BASE_URL", "https://backstage.example/api/catalog")
    monkeypatch.setenv("BACKSTAGE_TOKEN", "backstage-token")
    monkeypatch.setenv("TEAM", "checkout")
    monkeypatch.setenv("BACKSTAGE_OWNER_GROUP", "group/checkouts")
    monkeypatch.setenv("DEFAULT_GITHUB_SLUGS", "example-org/legacy-commerce,example-org/orders")
    monkeypatch.setenv("BACKSTAGE_USE_LOCAL_FILE", "true")
    monkeypatch.setenv("BACKSTAGE_LOCAL_FILE_PATH", "custom_repos.json")
    monkeypatch.setenv("BACKSTAGE_CACHE_TTL_SECONDS", "42.5")

    config = BackstageConfig.from_env()
    assert config.base_url == "https://backstage.example/api/catalog"
    assert config.token == "backstage-token"
    assert config.team == "checkout"
    assert config.owner_group == "group/checkouts"
    assert config.default_github_slugs == [
        "example-org/legacy-commerce",
        "example-org/orders",
    ]
    assert config.use_local_file is True
    assert config.local_file_path == "custom_repos.json"
    assert config.cache_ttl_seconds == 42.5


def test_backstage_config_defaults_to_local_file_mode(monkeypatch):
    monkeypatch.delenv("BACKSTAGE_USE_LOCAL_FILE", raising=False)
    monkeypatch.delenv("TEAM", raising=False)
    monkeypatch.delenv("BACKSTAGE_OWNER_GROUP", raising=False)
    monkeypatch.delenv("DEFAULT_GITHUB_SLUGS", raising=False)
    monkeypatch.delenv("BACKSTAGE_DEFAULT_GITHUB_SLUGS", raising=False)
    config = BackstageConfig.from_env()
    assert config.use_local_file is True
    assert config.team == "team"
    assert config.owner_group == "team"


def test_backstage_config_can_disable_local_file_mode(monkeypatch):
    monkeypatch.setenv("BACKSTAGE_USE_LOCAL_FILE", "false")
    config = BackstageConfig.from_env()
    assert config.use_local_file is False


def test_jira_config_from_env(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "dev@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "jira-token")

    config = JiraConfig.from_env()
    assert config.base_url == "https://example.atlassian.net"
    assert config.email == "dev@example.com"
    assert config.api_token == "jira-token"


def test_github_config_from_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    monkeypatch.setenv("GITHUB_DEFAULT_ORG", "acme-org")

    config = GitHubConfig.from_env()
    assert config.token == "gh-token"
    assert config.default_org == "acme-org"
