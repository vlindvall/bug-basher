import argparse
import asyncio
import json
import os

from investigator.reporter import report_findings
from shared.backstage_client import BackstageClient
from shared.config import BackstageConfig, GitHubConfig, InvestigationConfig, JiraConfig, SlackConfig, TriageConfig, load_dotenv
from shared.models import BugReport, Repository


def _backstage_config() -> BackstageConfig:
    return BackstageConfig.from_env()


def _resolve_provider(provider: str | None) -> str:
    resolved = (provider or os.environ.get("LLM_PROVIDER", "codex")).strip().lower()
    if resolved not in {"claude", "codex"}:
        raise ValueError(f"Unsupported provider: {resolved}")
    return resolved


def _filter_repositories(repos: list[Repository], config: BackstageConfig) -> list[Repository]:
    team = config.team.strip().lower()
    slug_filters = [s.lower() for s in config.default_github_slugs]

    if not team and not slug_filters:
        return repos

    filtered: list[Repository] = []
    for repo in repos:
        owner = (repo.owner or "").lower()
        slug = (repo.github_slug or "").lower()
        team_match = bool(team) and team in owner
        slug_match = any(token in slug for token in slug_filters)
        if team_match or slug_match:
            filtered.append(repo)

    return filtered


async def repos_command() -> None:
    backstage_config = _backstage_config()
    async with BackstageClient(backstage_config) as client:
        repos = await client.get_repositories()
        filtered = _filter_repositories(repos, backstage_config)
        scope = f"team={backstage_config.team}"
        if backstage_config.default_github_slugs:
            scope += f", slug_filters={','.join(backstage_config.default_github_slugs)}"
        print(f"Found {len(filtered)} repositories ({scope}):\n")
        for r in sorted(filtered, key=lambda r: r.name):
            slug = r.github_slug or "(no slug)"
            print(f"  {r.name:<40} {slug}")


async def _fetch_bug(args: argparse.Namespace) -> BugReport:
    """Build a BugReport from JIRA API or CLI flags."""
    if args.jira_key and not args.summary:
        from shared.jira_client import JiraClient

        jira_config = JiraConfig.from_env()

        async with JiraClient(jira_config) as jira:
            return await jira.get_issue(args.jira_key)

    return BugReport(
        jira_key=args.jira_key or "BUG-0000",
        summary=args.summary,
        description=args.description,
        components=args.components or [],
        priority=args.priority,
    )


async def triage_command(args: argparse.Namespace) -> None:
    from investigator.triage import create_triage_client

    bug = await _fetch_bug(args)
    provider = _resolve_provider(args.provider)

    config = TriageConfig(
        provider=provider,
        use_subprocess=args.local or provider == "codex",
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        model=args.model or os.environ.get("TRIAGE_MODEL"),
    )

    client = create_triage_client(config)

    backstage_config = _backstage_config()
    async with BackstageClient(backstage_config) as backstage:
        repos = await backstage.get_repositories()
        filtered = _filter_repositories(repos, backstage_config)

    print(f"Triaging {bug.jira_key}: {bug.summary}")
    if bug.description:
        print(f"  {bug.description[:120]}{'...' if len(bug.description) > 120 else ''}")
    print(f"Against {len(filtered)} repositories...")
    print()

    results = await client.triage(bug, filtered)

    if not results:
        print("No relevant repositories identified.")
        return

    for r in results:
        print(f"  {r.confidence:.0%}  {r.repo}")
        if r.reasoning:
            print(f"       {r.reasoning}")


async def investigate_command(args: argparse.Namespace) -> None:
    from investigator.agent import aggregate_findings, investigate_repos
    from investigator.triage import create_triage_client

    bug = await _fetch_bug(args)
    provider = _resolve_provider(args.provider)

    triage_config = TriageConfig(
        provider=provider,
        use_subprocess=args.local or provider == "codex",
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        model=args.triage_model or os.environ.get("TRIAGE_MODEL"),
    )
    client = create_triage_client(triage_config)

    backstage_config = _backstage_config()
    async with BackstageClient(backstage_config) as backstage:
        repos = await backstage.get_repositories()
        filtered = _filter_repositories(repos, backstage_config)

    print(f"Triaging {bug.jira_key}: {bug.summary}")
    triage_results = await client.triage(bug, filtered)

    if not triage_results:
        print("No relevant repositories identified.")
        return

    print(f"Triage identified {len(triage_results)} repositories:")
    for r in triage_results:
        print(f"  {r.confidence:.0%}  {r.repo}")
    print()

    inv_config = InvestigationConfig(
        provider=provider,
        model=args.agent_model or os.environ.get("INVESTIGATION_MODEL"),
        max_budget_usd=args.budget,
        github_token=os.environ.get("GITHUB_TOKEN", ""),
    )

    print("Investigating...")
    results = await investigate_repos(bug, triage_results, filtered, inv_config)
    findings = aggregate_findings(bug, results, inv_config)

    print()
    if findings.best_result:
        best = findings.best_result
        print(f"Best result: {best.repo} (confidence: {best.confidence:.0%})")
        if best.root_cause:
            print(f"  Root cause: {best.root_cause}")
        if best.evidence:
            print("  Evidence:")
            for e in best.evidence:
                print(f"    - {e}")
        if best.proposed_fix and best.proposed_fix.files_changed:
            print(f"  Proposed fix: {best.proposed_fix.description}")
            for fc in best.proposed_fix.files_changed:
                print(f"    - {fc.path}")
    else:
        print("No findings.")

    if findings.action:
        print(f"\nRecommended action: {findings.action.action_type}")

    if args.report:
        from shared.github_client import GitHubClient
        from shared.jira_client import JiraClient
        from shared.slack_client import SlackClient

        github_config = GitHubConfig.from_env()
        slack_bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
        slack_channel = os.environ.get("SLACK_CHANNEL", "")

        if args.dry_run:
            from investigator.reporter import format_jira_comment

            dry_run_comment = format_jira_comment(findings, pr_url=None)
            print("\n[DRY RUN] JIRA comment payload:")
            print(json.dumps(dry_run_comment, indent=2))
            print("\n[DRY RUN] Skipping PR creation, JIRA comment post, and Slack notification.")
            return

        jira_config = JiraConfig.from_env()
        github_client: GitHubClient | None = None
        slack_client: SlackClient | None = None

        async with JiraClient(jira_config) as jira:
            if github_config.token:
                gh_ctx = GitHubClient(github_config)
                github_client = await gh_ctx.__aenter__()
            if slack_bot_token and slack_channel:
                sl_ctx = SlackClient(SlackConfig(bot_token=slack_bot_token, channel=slack_channel))
                slack_client = await sl_ctx.__aenter__()

            try:
                pr_url = await report_findings(
                    findings,
                    filtered,
                    jira,
                    github_client=github_client,
                    slack_client=slack_client,
                    slack_channel=slack_channel,
                )
                if pr_url:
                    print(f"\nPR created: {pr_url}")
                else:
                    print("\nFindings reported (no PR created).")
            finally:
                if github_client:
                    await gh_ctx.__aexit__(None, None, None)
                if slack_client:
                    await sl_ctx.__aexit__(None, None, None)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="bug-basher", description="Bug Basher CLI")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("repos", help="List repositories for configured team/slug filters")

    triage_parser = subparsers.add_parser("triage", help="Triage a bug against repositories")
    triage_parser.add_argument("jira_key", nargs="?", default=None, help="JIRA issue key (e.g. BUY-1234) — fetches details from JIRA API")
    triage_parser.add_argument("--summary", default=None, help="Bug summary (used instead of JIRA fetch)")
    triage_parser.add_argument("--description", default="", help="Bug description")
    triage_parser.add_argument("--components", nargs="*", help="Bug components")
    triage_parser.add_argument("--priority", default="P3", help="Bug priority (default: P3)")
    triage_parser.add_argument("--provider", choices=["claude", "codex"], default=None, help="LLM provider (default: LLM_PROVIDER env or codex)")
    triage_parser.add_argument("--model", default=None, help="Optional model override for triage")
    triage_parser.add_argument("--local", action="store_true", help="Force CLI subprocess mode (Claude only; Codex always uses subprocess)")

    investigate_parser = subparsers.add_parser("investigate", help="Investigate a bug end-to-end")
    investigate_parser.add_argument("jira_key", nargs="?", default=None, help="JIRA issue key (e.g. BUY-1234)")
    investigate_parser.add_argument("--summary", default=None, help="Bug summary (used instead of JIRA fetch)")
    investigate_parser.add_argument("--description", default="", help="Bug description")
    investigate_parser.add_argument("--components", nargs="*", help="Bug components")
    investigate_parser.add_argument("--priority", default="P3", help="Bug priority (default: P3)")
    investigate_parser.add_argument("--provider", choices=["claude", "codex"], default=None, help="LLM provider (default: LLM_PROVIDER env or codex)")
    investigate_parser.add_argument("--triage-model", default=None, help="Optional model override for triage")
    investigate_parser.add_argument("--agent-model", default=None, help="Optional model override for investigation agent")
    investigate_parser.add_argument("--local", action="store_true", help="Force CLI subprocess mode for triage (Claude only; Codex always uses subprocess)")
    investigate_parser.add_argument("--budget", type=float, default=0.50, help="Max budget per agent in USD (default: 0.50)")
    investigate_parser.add_argument("--report", action="store_true", help="Report findings: create PR, comment on JIRA, notify Slack")
    investigate_parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="With --report, print JIRA comment payload and skip PR/JIRA/Slack side effects (default: true; use --no-dry-run to execute side effects)",
    )

    args = parser.parse_args()

    if args.command == "triage":
        if not args.jira_key and not args.summary:
            triage_parser.error("provide a JIRA key or --summary")
        asyncio.run(triage_command(args))
    elif args.command == "investigate":
        if not args.jira_key and not args.summary:
            investigate_parser.error("provide a JIRA key or --summary")
        asyncio.run(investigate_command(args))
    else:
        asyncio.run(repos_command())


if __name__ == "__main__":
    main()
