# Bug Basher - Implementation Plan

Automated bug investigation system that receives JIRA webhook notifications for team bugs, identifies relevant repositories via Backstage, uses Claude to triage and investigate root causes, and either opens a PR with a fix or comments findings on the ticket.

---

## Architecture Overview

```
JIRA Webhook (label: TEAM)
    │
    ▼
API Gateway (POST /webhook/jira)
    │
    ▼
Lambda: webhook-receiver
    ├── Validate JIRA webhook signature
    ├── Parse issue event (filter: issue_created, label match)
    └── Enqueue to SQS
            │
            ▼
        SQS Queue (bug-basher-investigation)
            │
            ▼
        ECS Fargate Task: investigator
            ├── 1. Fetch full bug details from JIRA API
            ├── 2. Query Backstage API for team repos
            ├── 3. Claude Haiku: triage & rank repos by relevance
            ├── 4. Clone top-N repos, spawn investigation agents (parallel)
            ├── 5. Aggregate findings, assess confidence
            └── 6. Action based on confidence:
                 ├── HIGH  → Create branch + PR, comment on JIRA, post to Slack
                 └── LOW   → Comment on JIRA with findings/next steps, post to Slack
```

### Why ECS over Lambda for investigation?

- Lambda has a 15-minute timeout; cloning repos, running agents, and creating PRs can take longer
- ECS Fargate tasks can run for hours, have more memory, and can mount ephemeral storage for repo clones
- Lambda is still used for the webhook receiver (fast, cheap, scales well)

---

## Components

### 1. Infrastructure (CDK)

**Stack: `BugBasherStack`**

| Resource | Purpose |
|---|---|
| API Gateway (HTTP API) | JIRA webhook endpoint |
| Lambda `webhook-receiver` | Validate & enqueue webhook events |
| SQS Queue `bug-basher-investigation` | Decouple webhook from investigation |
| SQS DLQ `bug-basher-investigation-dlq` | Failed investigations |
| ECS Fargate Cluster + Task Definition | Run investigation workload |
| ECR Repository | Container image for investigator |
| SSM Parameters | JIRA API token, Anthropic API key, GitHub token, Slack webhook URL, Backstage API URL |
| IAM Roles | Lambda execution, ECS task role, SQS permissions |
| CloudWatch Log Groups | Lambda + ECS logs |

### 2. Webhook Receiver (Lambda)

**Runtime:** Python 3.12
**Trigger:** API Gateway POST `/webhook/jira`

```
Responsibilities:
1. Verify JIRA webhook signature (shared secret)
2. Parse the event payload
3. Filter:
   - Event type = issue_created (or issue_updated with label added)
   - Issue type = Bug
   - Labels include the configured TEAM label
4. Extract: issue key, summary, description, labels, reporter, priority, components
5. Send message to SQS with extracted fields
6. Return 200 OK to JIRA
```

**Payload sent to SQS:**
```json
{
  "jira_key": "BUY-1234",
  "summary": "Checkout fails for subscription items",
  "description": "Full bug description...",
  "labels": ["team", "checkout"],
  "priority": "P1",
  "reporter": "jane.doe",
  "components": ["checkout-service", "cart-api"],
  "created": "2026-02-21T10:00:00Z",
  "url": "https://jira.example.com/browse/BUY-1234"
}
```

### 3. Investigator (ECS Fargate)

**Runtime:** Python 3.12 in Docker container
**Trigger:** SQS message (via ECS task launched by Lambda or EventBridge Pipe)

#### Step 1: Fetch Bug Details

- Call JIRA REST API to get the full ticket (in case description was truncated)
- Pull comments (may contain additional context from reporters)

#### Step 2: Query Backstage for Repositories

- Call Backstage Catalog API: `GET /api/catalog/entities?filter=spec.owner=group:team&filter=kind=Component`
- Extract for each repo:
  - `metadata.name` — repo/component name
  - `metadata.description` — what the service does
  - `metadata.annotations["github.com/project-slug"]` — GitHub org/repo
  - `spec.type` — service, library, website, etc.
  - `spec.lifecycle` — production, experimental, deprecated
  - `spec.system` — which system it belongs to
- Filter out deprecated/experimental components
- Cache this list (TTL ~1 hour) to avoid hitting Backstage on every invocation

#### Step 3: Triage with Claude Haiku

**Model:** claude-haiku-4-5-20251001
**Purpose:** Rank which repositories are most likely related to the bug

**Input prompt structure:**
```
You are a senior engineer triaging a bug report. Given the bug details and a list
of repositories owned by the team, rank which repositories are most likely to
contain the root cause.

## Bug Report
- Key: {jira_key}
- Summary: {summary}
- Description: {description}
- Components: {components}
- Priority: {priority}

## Available Repositories
{for each repo: name, description, type, system}

## Instructions
Return a JSON array ranked by likelihood, with confidence scores (0-1):
[
  {"repo": "checkout-service", "confidence": 0.92, "reasoning": "Bug mentions checkout failure..."},
  {"repo": "cart-api", "confidence": 0.75, "reasoning": "Cart state could cause..."},
  ...
]

Only include repos with confidence > 0.3. Max 5 repos.
```

**Output:** Ranked list of repos to investigate (max 5, confidence > 0.3)

#### Step 4: Investigation Agents (Parallel)

For each triaged repo (up to top 3-5 based on confidence):

1. **Clone the repository** (shallow clone, last 30 days of history)
2. **Spawn a Claude agent** (Sonnet for balance of cost/capability) with tools:
   - Read files
   - Search/grep codebase
   - View recent git log & diffs
   - (Optional) Query New Relic for error logs around the bug creation time
3. **Agent prompt:**
   ```
   You are investigating a bug in the {repo_name} repository.

   ## Bug Report
   {full bug details}

   ## Your Task
   1. Search the codebase for code related to the bug description
   2. Check recent commits (last 2 weeks) for changes that could have introduced this bug
   3. Look for error handling gaps, race conditions, or logic errors
   4. If you find a likely root cause, propose a fix

   ## Output Format
   {
     "root_cause_found": true/false,
     "confidence": 0.0-1.0,
     "root_cause": "Description of the root cause...",
     "evidence": ["file:line - explanation", ...],
     "recent_suspect_commits": ["abc1234 - commit message", ...],
     "proposed_fix": {
       "description": "What the fix does...",
       "files_changed": [{"path": "...", "diff": "..."}],
     },
     "next_steps": ["If not confident, suggest what to investigate..."]
   }
   ```
4. **Budget:** Set a token/time limit per agent (e.g., 5 minutes, 100k tokens)

#### Step 5: Aggregate Findings

- Collect results from all investigation agents
- Pick the highest-confidence finding
- Decision matrix:

| Confidence | Has Fix? | Action |
|---|---|---|
| >= 0.8 | Yes | Create PR + JIRA comment + Slack |
| >= 0.8 | No | JIRA comment with root cause + Slack |
| 0.5 - 0.8 | Yes/No | JIRA comment with findings (marked as uncertain) + Slack |
| < 0.5 | — | JIRA comment with investigation summary & next steps + Slack |

#### Step 6a: High-Confidence Fix → Create PR

1. Create a new branch: `bug-basher/{jira_key}`
2. Apply the proposed fix
3. Run linting/formatting (if configured for the repo)
4. Commit with message: `fix: {summary} ({jira_key})`
5. Push branch and create PR via GitHub API:
   - Title: `[Bug Basher] {jira_key}: {summary}`
   - Body:
     ```markdown
     ## Bug
     [{jira_key}]({jira_url}): {summary}

     ## Root Cause
     {root_cause_description}

     ## Evidence
     {evidence list}

     ## Changes
     {description of fix}

     ---
     *This PR was automatically generated by Bug Basher.
     Confidence: {confidence}%
     Please review carefully before merging.*
     ```
6. Request review from the team that owns the repo (from Backstage)

#### Step 6b: Comment on JIRA

Always comment on the JIRA ticket with findings:

```markdown
🔍 *Bug Basher Investigation Complete*

**Repositories Investigated:** {list}
**Confidence:** {confidence level}

**Findings:**
{root cause or investigation summary}

**Evidence:**
{evidence list}

{if PR created}
**Pull Request:** [PR #{number}]({pr_url})
{/if}

{if low confidence}
**Suggested Next Steps:**
{next steps list}
{/if}
```

#### Step 6c: Post to Slack

Post a summary to the team's internal channel:
- Bug key + link
- Confidence level
- Whether a PR was created (with link)
- Brief summary of findings

---

## Project Structure

```
bug-basher/
├── PLAN.md                          # This file
├── README.md
├── cdk/
│   ├── app.py
│   ├── cdk.json
│   ├── requirements.txt
│   └── stacks/
│       └── bug_basher_stack.py      # All infra
├── src/
│   ├── shared/
│   │   ├── __init__.py
│   │   ├── config.py                # SSM param loading, env config
│   │   ├── jira_client.py           # JIRA REST API wrapper
│   │   ├── backstage_client.py      # Backstage Catalog API wrapper
│   │   ├── github_client.py         # GitHub API wrapper (PRs, branches)
│   │   ├── slack_client.py          # Slack webhook poster
│   │   └── models.py                # Pydantic models for bug, repo, findings
│   ├── webhook_receiver/
│   │   ├── __init__.py
│   │   ├── handler.py               # Lambda handler
│   │   └── validator.py             # JIRA webhook signature validation
│   ├── investigator/
│   │   ├── __init__.py
│   │   ├── main.py                  # ECS task entrypoint
│   │   ├── triage.py                # Haiku triage logic
│   │   ├── agent.py                 # Investigation agent orchestration
│   │   ├── repo_manager.py          # Clone, branch, commit, push
│   │   ├── pr_creator.py            # PR creation logic
│   │   ├── reporter.py              # JIRA comment + Slack formatting
│   │   └── Dockerfile
│   └── tests/
│       ├── __init__.py
│       ├── test_webhook_receiver.py
│       ├── test_triage.py
│       ├── test_agent.py
│       └── fixtures/
│           └── sample_webhook.json
├── pyproject.toml
└── poetry.lock
```

---

## Implementation Phases

### Phase 1: Foundation (Week 1)
- [ ] Project scaffolding (Poetry, CDK, Docker)
- [ ] Pydantic models for all data structures
- [ ] JIRA client (fetch issue, post comment)
- [ ] Backstage client (fetch team repos)
- [ ] Config module (SSM parameter loading)
- [ ] Webhook receiver Lambda (validate, filter, enqueue)
- [ ] CDK stack: API Gateway, Lambda, SQS, IAM
- [ ] Unit tests for webhook validation and filtering

### Phase 2: Triage & Investigation (Week 2)
- [ ] Haiku triage prompt + response parsing
- [ ] Repo manager (clone, branch management)
- [ ] Investigation agent (Claude Sonnet + code search tools)
- [ ] Agent budget/timeout enforcement
- [ ] Findings aggregation logic
- [ ] Unit tests for triage and agent output parsing

### Phase 3: Actions & Reporting (Week 3)
- [ ] GitHub client (create branch, commit, PR)
- [ ] PR template and creation logic
- [ ] JIRA comment formatting and posting
- [ ] Slack notification posting
- [ ] Confidence-based decision routing
- [ ] ECS Fargate task definition + Dockerfile
- [ ] CDK stack: ECS, ECR, EventBridge Pipe (SQS → ECS)
- [ ] Integration tests

### Phase 4: Hardening & Observability (Week 4)
- [ ] DLQ handling and alerting
- [ ] CloudWatch metrics and dashboards
- [ ] Rate limiting (max concurrent investigations)
- [ ] Feedback loop: track PR merge/close rates
- [ ] Error handling for all external APIs
- [ ] End-to-end testing with real JIRA webhook
- [ ] Runbook documentation

---

## Key Configuration (SSM Parameters)

| Parameter | Description |
|---|---|
| `/bug-basher/jira/api-token` | JIRA API token (SecureString) |
| `/bug-basher/jira/base-url` | JIRA instance URL |
| `/bug-basher/jira/webhook-secret` | Webhook signature secret |
| `/bug-basher/anthropic/api-key` | Anthropic API key (SecureString) |
| `/bug-basher/github/token` | GitHub PAT or App token (SecureString) |
| `/bug-basher/github/org` | GitHub organization name |
| `/bug-basher/backstage/api-url` | Backstage API base URL |
| `/bug-basher/backstage/api-token` | Backstage auth token (SecureString) |
| `/bug-basher/slack/webhook-url` | Slack incoming webhook URL (SecureString) |
| `/bug-basher/config/max-repos` | Max repos to investigate (default: 3) |
| `/bug-basher/config/confidence-threshold` | PR creation threshold (default: 0.8) |

---

## Cost Considerations

| Component | Estimated Cost per Investigation |
|---|---|
| Haiku triage call | ~$0.01 |
| Sonnet investigation (per repo, ~100k tokens) | ~$1.50 |
| ECS Fargate (2 vCPU, 4GB, ~10 min) | ~$0.01 |
| Total per bug (3 repos investigated) | ~$5.00 |

At ~20 bugs/week: **~$400/month** in AI costs + minimal infra costs.

---

## Open Questions

1. **JIRA webhook configuration** — Does the JIRA instance support webhook label filters natively, or do we filter in Lambda?
2. **Backstage auth** — What authentication does the Backstage API require? Service account token?
3. **GitHub auth** — GitHub App (preferred) or PAT? App gives per-repo permissions and higher rate limits.
4. **Slack channel** — Which channel should notifications go to?
5. **Test execution** — Should investigation agents run tests as part of validation, or just propose code changes?
6. **New Relic integration** — Worth giving agents access to recent error logs for additional context?
7. **Approval workflow** — Should high-confidence PRs auto-request review from the Backstage-listed owners, or go to a fixed reviewer group?
