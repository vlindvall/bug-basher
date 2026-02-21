# Bug Basher

Automated bug investigation system that receives JIRA webhook notifications, identifies relevant repositories via the Backstage catalog, uses an LLM to triage and investigate root causes, and either opens a PR with a fix or comments findings on the JIRA ticket.

## Architecture

```
JIRA Webhook → API Gateway → Lambda (webhook-receiver) → SQS → ECS Fargate (investigator)
```

See `PLAN.md` for the full implementation plan.

## Prerequisites

- Python 3.12+
- [Poetry](https://python-poetry.org/docs/#installation) (2.x)
- Docker (for the investigator container)
- AWS CLI + CDK CLI (for infrastructure deployment)

## Local Setup

1. **Clone the repository:**

   ```bash
   git clone <repo-url>
   cd bug-basher
   ```

2. **Install dependencies:**

   ```bash
   poetry install
   ```

3. **Verify everything works:**

   ```bash
   poetry run pytest -v
   ```

## Common Commands

```bash
# Run all tests
poetry run pytest

# Run a specific test file
poetry run pytest src/tests/test_backstage_client.py -v

# Run a single test by name
poetry run pytest src/tests/test_backstage_client.py::test_caches_results -v

# Lint
poetry run ruff check src/

# Format
poetry run ruff format src/
```

## LLM Provider Selection

Both `triage` and `investigate` support `claude` and `codex` providers.

```bash
# Triage with Codex CLI
poetry run python -m shared.cli triage BUY-1234 --provider codex

# Investigate with Codex CLI
poetry run python -m shared.cli investigate BUY-1234 --provider codex
```

Optional environment variables:

- `LLM_PROVIDER` (`claude` or `codex`)
- `TRIAGE_MODEL` (optional model override for triage)
- `INVESTIGATION_MODEL` (optional model override for investigation agent)
- `GITHUB_DEFAULT_ORG` (default GitHub org used by `GitHubConfig`)
- `TEAM` (team identifier used for repo filtering; replaces hardcoded team names)
- `DEFAULT_GITHUB_SLUGS` (comma-separated fallback slug filters, e.g. `example-org/commerce-core,example-org/checkout`)
- `BACKSTAGE_OWNER_GROUP` (optional explicit Backstage owner group; defaults to `TEAM`)
- `BACKSTAGE_DEFAULT_GITHUB_SLUGS` (legacy alias for `DEFAULT_GITHUB_SLUGS`)

## Backstage Offline Mode

CLI config now defaults to reading repositories from a local JSON file.

- `BACKSTAGE_LOCAL_FILE_PATH=backstage_fixture.json`
- `BACKSTAGE_USE_LOCAL_FILE=false` to force Backstage API mode

## Project Structure

```
src/
  shared/                   # Common modules (config, API clients, models)
    backstage_client.py     # Async Backstage Catalog API client
    config.py               # Configuration dataclasses with defaults
    models.py               # Pydantic models (API responses + domain models)
  webhook_receiver/         # Lambda handler + JIRA webhook validation
  investigator/             # ECS task: triage, agent orchestration, PR creation
  tests/                    # pytest tests + fixtures
cdk/                        # AWS CDK infrastructure
  stacks/bug_basher_stack.py
```

## Running the Backstage Client Locally

You can test the Backstage catalog client against the real API:

```python
import asyncio
from shared.backstage_client import BackstageClient

async def main():
    async with BackstageClient() as client:
        repos = await client.get_repositories()
        for r in repos:
            print(f"{r.name} -> {r.github_slug}")

asyncio.run(main())
```

Run it with:

```bash
poetry run python -c "
import asyncio
from shared.backstage_client import BackstageClient

async def main():
    async with BackstageClient() as client:
        repos = await client.get_repositories()
        for r in repos:
            print(f'{r.name} -> {r.github_slug}')

asyncio.run(main())
"
```

## Infrastructure Deployment

```bash
# Synthesize CloudFormation template
cd cdk && cdk synth

# Deploy the stack
cd cdk && cdk deploy

# Build the investigator Docker image
docker build -t bug-basher-investigator -f src/investigator/Dockerfile .
```

## Configuration

All secrets and configuration are stored in AWS SSM Parameter Store under the `/bug-basher/` prefix. See `PLAN.md` for the full list of parameters.

Key parameters:

| Parameter | Description |
|---|---|
| `/bug-basher/backstage/api-url` | Backstage API base URL |
| `/bug-basher/backstage/api-token` | Backstage auth token |
| `/bug-basher/jira/api-token` | JIRA API token |
| `/bug-basher/anthropic/api-key` | Anthropic API key |
| `/bug-basher/github/token` | GitHub token |
| `/bug-basher/slack/webhook-url` | Slack incoming webhook URL |
