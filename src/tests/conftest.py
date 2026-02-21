import json

import pytest

from shared.config import BackstageConfig, GitHubConfig, InvestigationConfig, JiraConfig, SlackConfig, TriageConfig
from shared.models import (
    Action,
    AggregatedFindings,
    BugReport,
    FileChange,
    InvestigationResult,
    ProposedFix,
    Repository,
)


@pytest.fixture
def backstage_config() -> BackstageConfig:
    return BackstageConfig(
        base_url="https://backstage.test/api/catalog",
        token="test-token",
    )


@pytest.fixture
def triage_config() -> TriageConfig:
    return TriageConfig(
        provider="claude",
        anthropic_api_key="test-key",
        max_repos=5,
        min_confidence=0.3,
    )


@pytest.fixture
def sample_bug() -> BugReport:
    return BugReport(
        jira_key="BUY-1234",
        summary="Checkout fails for subscription items",
        description="Users see a 500 error when checking out with subscription products",
        components=["checkout", "subscriptions"],
        priority="P1",
        labels=["production", "regression"],
    )


@pytest.fixture
def sample_repos() -> list[Repository]:
    return [
        Repository(
            name="checkout-service",
            description="Handles checkout flow",
            github_slug="example-org/checkout-service",
            component_type="service",
            tags=["checkout", "payments"],
        ),
        Repository(
            name="subscription-engine",
            description="Subscription management",
            github_slug="example-org/subscription-engine",
            component_type="service",
            tags=["subscriptions"],
        ),
        Repository(
            name="product-catalog",
            description="Product data service",
            github_slug="example-org/product-catalog",
            component_type="service",
        ),
    ]


@pytest.fixture
def jira_config() -> JiraConfig:
    return JiraConfig(
        base_url="https://test.atlassian.net",
        email="test@example.com",
        api_token="test-jira-token",
    )


def make_jira_issue(
    key: str = "BUY-1234",
    summary: str = "Checkout fails for subscription items",
    description: dict | None = None,
    labels: list[str] | None = None,
    priority: str = "High",
    reporter: str = "Jane Doe",
    components: list[str] | None = None,
    created: str = "2026-02-21T10:00:00.000+0000",
) -> dict:
    if description is None:
        description = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Users see a 500 error at checkout"}
                    ],
                }
            ],
        }
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "description": description,
            "labels": labels or ["team"],
            "priority": {"name": priority},
            "reporter": {"displayName": reporter},
            "components": [{"name": c} for c in (components or ["checkout"])],
            "created": created,
        },
    }


@pytest.fixture
def investigation_config() -> InvestigationConfig:
    return InvestigationConfig(
        provider="claude",
        model="claude-sonnet-4-6",
        max_budget_usd=0.25,
        clone_depth=50,
        clone_timeout_seconds=30.0,
        agent_timeout_seconds=60.0,
        max_parallel_agents=2,
        github_token="test-gh-token",
    )


def make_investigation_response(
    root_cause_found: bool = True,
    confidence: float = 0.85,
    root_cause: str = "Null pointer in checkout handler",
    evidence: list[str] | None = None,
    recent_suspect_commits: list[str] | None = None,
    proposed_fix: dict | None = None,
    next_steps: list[str] | None = None,
) -> str:
    data = {
        "root_cause_found": root_cause_found,
        "confidence": confidence,
        "root_cause": root_cause,
        "evidence": evidence or ["Stack trace in logs", "Recent commit changed handler"],
        "recent_suspect_commits": recent_suspect_commits or ["abc1234"],
        "proposed_fix": proposed_fix or {
            "description": "Add null check",
            "files_changed": [{"path": "src/checkout.py", "diff": "- old\n+ new"}],
        },
        "next_steps": next_steps or ["Add integration test"],
    }
    return json.dumps(data)


def make_entity(
    name: str = "my-service",
    *,
    lifecycle: str = "production",
    slug: str | None = "example-org/my-service",
    description: str | None = "A service",
    owner: str = "group:team",
    system: str | None = "buy-system",
    component_type: str = "service",
    tags: list[str] | None = None,
) -> dict:
    annotations: dict[str, str] = {}
    if slug is not None:
        annotations["github.com/project-slug"] = slug

    entity: dict = {
        "metadata": {
            "name": name,
            "annotations": annotations,
            "tags": tags or [],
        },
        "spec": {
            "type": component_type,
            "lifecycle": lifecycle,
            "owner": owner,
            "dependsOn": [],
        },
    }
    if description is not None:
        entity["metadata"]["description"] = description
    if system is not None:
        entity["spec"]["system"] = system
    return entity


@pytest.fixture
def github_config() -> GitHubConfig:
    return GitHubConfig(
        token="test-gh-token",
        default_org="example-org",
    )


@pytest.fixture
def slack_config() -> SlackConfig:
    return SlackConfig(
        bot_token="xoxb-test-token",
        channel="#bug-basher",
    )


def make_aggregated_findings(
    *,
    jira_key: str = "BUY-1234",
    summary: str = "Checkout fails for subscription items",
    confidence: float = 0.85,
    root_cause: str = "Null pointer in checkout handler",
    evidence: list[str] | None = None,
    action_type: str = "pr",
    has_fix: bool = True,
    fix_description: str = "Add null check",
    files_changed: list[dict] | None = None,
    next_steps: list[str] | None = None,
) -> AggregatedFindings:
    bug = BugReport(
        jira_key=jira_key,
        summary=summary,
        url=f"https://jira.example.com/browse/{jira_key}",
    )

    proposed_fix = None
    if has_fix:
        fc = files_changed or [{"path": "src/checkout.py", "diff": "fixed content"}]
        proposed_fix = ProposedFix(
            description=fix_description,
            files_changed=[FileChange(**f) for f in fc],
        )

    best_result = InvestigationResult(
        repo="checkout-service",
        root_cause_found=True,
        confidence=confidence,
        root_cause=root_cause,
        evidence=evidence or ["Stack trace in logs", "Recent commit changed handler"],
        proposed_fix=proposed_fix,
        next_steps=next_steps or ["Add integration test"],
    )

    action = Action(
        action_type=action_type,
        confidence=confidence,
        has_fix=has_fix,
    )

    return AggregatedFindings(
        bug=bug,
        results=[best_result],
        best_result=best_result,
        action=action,
    )
