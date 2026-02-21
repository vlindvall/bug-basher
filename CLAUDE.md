# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Bug Basher is an automated bug investigation system that:
1. Receives JIRA webhook notifications for team bugs
2. Identifies relevant repositories via the Backstage catalog
3. Uses Claude to triage and investigate root causes
4. Opens a PR with a fix (high confidence) or comments findings on the JIRA ticket (low confidence)

See `PLAN.md` for the full architecture and implementation plan.

## Architecture

```
JIRA Webhook → API Gateway → Lambda (webhook-receiver) → SQS → ECS Fargate (investigator)
```

- **Webhook Receiver** (Lambda, Python 3.12): Validates JIRA webhooks, filters for team bugs, enqueues to SQS
- **Investigator** (ECS Fargate, Python 3.12 in Docker): Fetches bug details, queries Backstage for repos, triages with Claude Haiku, spawns parallel investigation agents (Claude Sonnet), creates PRs or comments findings
- **Infrastructure**: AWS CDK stack (`cdk/stacks/bug_basher_stack.py`)

## Build Commands

```bash
# Install dependencies
poetry install

# Run tests
poetry run pytest

# Run a single test
poetry run pytest src/tests/test_webhook_receiver.py::test_name -v

# CDK commands (from cdk/ directory)
cd cdk && cdk synth
cd cdk && cdk deploy

# Build investigator Docker image
docker build -t bug-basher-investigator -f src/investigator/Dockerfile .
```

## Project Structure

```
cdk/                        # AWS CDK infrastructure
  stacks/bug_basher_stack.py
src/
  shared/                   # Common modules (config, API clients, models)
  webhook_receiver/         # Lambda handler + JIRA webhook validation
  investigator/             # ECS task: triage, agent orchestration, PR creation, reporting
  tests/                    # pytest tests + fixtures
```

## Key Design Decisions

- **ECS over Lambda for investigation**: Cloning repos and running agents can exceed Lambda's 15-min timeout. ECS Fargate allows longer runtime, more memory, and ephemeral storage.
- **Haiku for triage, Sonnet for investigation**: Haiku ranks repos by relevance (cheap, fast); Sonnet does deep code investigation (more capable).
- **Confidence-based actions**: >= 0.8 confidence with fix → PR; lower confidence → JIRA comment with findings.
- **Secrets in SSM Parameters**: All API tokens/keys stored in SSM (see PLAN.md for full list under `/bug-basher/` prefix).

## Tech Stack

- Python 3.12, Poetry for dependency management
- Pydantic for data models
- AWS CDK for infrastructure
- External APIs: JIRA, Backstage Catalog, GitHub, Slack webhooks, Anthropic Claude
