import os
from dataclasses import dataclass, field
from pathlib import Path

_DOTENV_LOADED = False


def load_dotenv() -> None:
    """Load .env into os.environ without overwriting existing variables."""
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return

    env_path = Path(__file__).resolve().parents[2] / ".env"
    if env_path.is_file():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if key and key not in os.environ:
                os.environ[key] = value

    _DOTENV_LOADED = True


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class BackstageConfig:
    base_url: str = "https://backstage.example.com/api/catalog"
    token: str = ""
    team: str = "team"
    owner_group: str = "team"
    default_github_slugs: list[str] = field(default_factory=list)
    cache_ttl_seconds: float = 3600.0
    use_local_file: bool = False
    local_file_path: str = "backstage_fixture.json"

    @classmethod
    def from_env(cls) -> "BackstageConfig":
        load_dotenv()
        config = cls()
        config.base_url = os.environ.get("BACKSTAGE_BASE_URL", config.base_url)
        config.token = os.environ.get("BACKSTAGE_TOKEN", config.token)
        config.team = os.environ.get("TEAM", config.team)
        config.owner_group = os.environ.get("BACKSTAGE_OWNER_GROUP", config.team)
        slug_csv = os.environ.get(
            "DEFAULT_GITHUB_SLUGS",
            os.environ.get("BACKSTAGE_DEFAULT_GITHUB_SLUGS", ""),
        )
        if slug_csv.strip():
            config.default_github_slugs = [
                s.strip() for s in slug_csv.split(",") if s.strip()
            ]
        # Default env-driven behavior to local file mode for easier offline use.
        config.use_local_file = _env_bool("BACKSTAGE_USE_LOCAL_FILE", True)
        config.local_file_path = os.environ.get(
            "BACKSTAGE_LOCAL_FILE_PATH",
            config.local_file_path,
        )
        ttl = os.environ.get("BACKSTAGE_CACHE_TTL_SECONDS")
        if ttl:
            try:
                config.cache_ttl_seconds = float(ttl)
            except ValueError:
                pass
        return config


@dataclass
class JiraConfig:
    base_url: str = "https://jira.example.com"
    email: str = ""
    api_token: str = ""

    @classmethod
    def from_env(cls) -> "JiraConfig":
        load_dotenv()
        config = cls()
        config.base_url = os.environ.get("JIRA_BASE_URL", config.base_url)
        config.email = os.environ.get("JIRA_EMAIL", config.email)
        config.api_token = os.environ.get("JIRA_API_TOKEN", config.api_token)
        return config


@dataclass
class TriageConfig:
    provider: str = "codex"
    anthropic_api_key: str = ""
    model: str | None = None
    max_repos: int = 5
    min_confidence: float = 0.3
    use_subprocess: bool = False


@dataclass
class InvestigationConfig:
    provider: str = "codex"
    model: str | None = None
    max_budget_usd: float = 0.50
    clone_depth: int = 100
    clone_timeout_seconds: float = 120.0
    agent_timeout_seconds: float = 300.0
    max_parallel_agents: int = 3
    high_confidence_threshold: float = 0.8
    uncertain_confidence_threshold: float = 0.5
    github_token: str = ""


@dataclass
class GitHubConfig:
    token: str = ""
    default_org: str = "example-org"

    @classmethod
    def from_env(cls) -> "GitHubConfig":
        load_dotenv()
        config = cls()
        config.token = os.environ.get("GITHUB_TOKEN", config.token)
        config.default_org = os.environ.get("GITHUB_DEFAULT_ORG", config.default_org)
        return config


@dataclass
class SlackConfig:
    bot_token: str = ""
    channel: str = ""
