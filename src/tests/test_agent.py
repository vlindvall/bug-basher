import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from investigator.agent import (
    _extract_json_object,
    aggregate_findings,
    build_investigation_prompt,
    clone_repo,
    investigate_repos,
    parse_investigation_response,
    run_investigation_agent,
)
from shared.config import InvestigationConfig
from shared.models import (
    BugReport,
    FileChange,
    InvestigationResult,
    ProposedFix,
    Repository,
    TriageResult,
)
from tests.conftest import make_investigation_response


# --- Prompt building ---


class TestBuildInvestigationPrompt:
    def test_includes_bug_details(self, sample_bug):
        prompt = build_investigation_prompt(sample_bug, "checkout-service")
        assert "BUY-1234" in prompt
        assert "Checkout fails for subscription items" in prompt
        assert "500 error" in prompt

    def test_includes_repo_name(self, sample_bug):
        prompt = build_investigation_prompt(sample_bug, "my-repo")
        assert "my-repo" in prompt

    def test_includes_output_format(self, sample_bug):
        prompt = build_investigation_prompt(sample_bug, "repo")
        assert "root_cause_found" in prompt
        assert "confidence" in prompt
        assert "proposed_fix" in prompt

    def test_includes_components_and_labels(self, sample_bug):
        prompt = build_investigation_prompt(sample_bug, "repo")
        assert "checkout" in prompt
        assert "subscriptions" in prompt
        assert "production" in prompt
        assert "regression" in prompt

    def test_omits_empty_optional_fields(self):
        bug = BugReport(jira_key="BUG-1", summary="A bug")
        prompt = build_investigation_prompt(bug, "repo")
        assert "Components" not in prompt
        assert "Labels" not in prompt
        assert "Description" not in prompt


# --- JSON extraction ---


class TestExtractJsonObject:
    def test_bare_object(self):
        text = '{"key": "value"}'
        assert _extract_json_object(text) == '{"key": "value"}'

    def test_markdown_fenced(self):
        text = '```json\n{"key": "value"}\n```'
        result = _extract_json_object(text)
        assert result == '{"key": "value"}'

    def test_embedded_in_prose(self):
        text = 'Here are the results:\n{"root_cause": "bug"}\nDone.'
        result = _extract_json_object(text)
        assert '"root_cause"' in result

    def test_no_json_returns_none(self):
        text = "No JSON here at all"
        assert _extract_json_object(text) is None


# --- Response parsing ---


class TestParseInvestigationResponse:
    def test_valid_full_response(self):
        raw = make_investigation_response()
        result = parse_investigation_response(raw, "checkout-service")
        assert result is not None
        assert result.root_cause_found is True
        assert result.confidence == 0.85
        assert result.root_cause == "Null pointer in checkout handler"
        assert len(result.evidence) == 2
        assert result.proposed_fix is not None
        assert len(result.proposed_fix.files_changed) == 1

    def test_sets_repo_name(self):
        raw = make_investigation_response()
        result = parse_investigation_response(raw, "my-repo")
        assert result is not None
        assert result.repo == "my-repo"

    def test_no_proposed_fix(self):
        data = {
            "root_cause_found": True,
            "confidence": 0.7,
            "root_cause": "Race condition",
            "evidence": ["logs show timing issue"],
        }
        result = parse_investigation_response(json.dumps(data), "repo")
        assert result is not None
        assert result.proposed_fix is None

    def test_markdown_wrapped(self):
        inner = make_investigation_response()
        raw = f"```json\n{inner}\n```"
        result = parse_investigation_response(raw, "repo")
        assert result is not None
        assert result.root_cause_found is True

    def test_malformed_json(self):
        result = parse_investigation_response("{broken json", "repo")
        assert result is None

    def test_non_object_json(self):
        result = parse_investigation_response("[1, 2, 3]", "repo")
        assert result is None

    def test_no_json_in_text(self):
        result = parse_investigation_response("No JSON here", "repo")
        assert result is None

    def test_invalid_confidence(self):
        raw = json.dumps({"root_cause_found": False, "confidence": 1.5})
        result = parse_investigation_response(raw, "repo")
        assert result is None

    def test_invalid_file_change_items_skipped(self):
        raw = json.dumps({
            "root_cause_found": True,
            "confidence": 0.8,
            "proposed_fix": {
                "description": "fix",
                "files_changed": [
                    {"path": "good.py", "diff": "+fix"},
                    "not a dict",
                    {"no_path": True},
                ],
            },
        })
        result = parse_investigation_response(raw, "repo")
        assert result is not None
        assert result.proposed_fix is not None
        assert len(result.proposed_fix.files_changed) == 1
        assert result.proposed_fix.files_changed[0].path == "good.py"


# --- Clone repo ---


class TestCloneRepo:
    async def test_successful_clone(self, investigation_config, tmp_path):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0

        async def fake_exec(*args, **kwargs):
            # Create the destination directory to simulate git clone
            dest = Path(args[6])  # 7th arg is dest path
            dest.mkdir(parents=True, exist_ok=True)
            return mock_proc

        with patch("investigator.agent.asyncio.create_subprocess_exec", side_effect=fake_exec):
            with patch("investigator.agent.tempfile.mkdtemp", return_value=str(tmp_path)):
                result = await clone_repo("example-org/checkout-service", investigation_config)

        assert result == tmp_path / "checkout-service"

    async def test_includes_token_in_url(self, investigation_config):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0

        with patch("investigator.agent.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            with patch("investigator.agent.tempfile.mkdtemp", return_value="/tmp/test"):
                try:
                    await clone_repo("example-org/repo", investigation_config)
                except Exception:
                    pass

        args = mock_exec.call_args[0]
        url_arg = args[5]  # 6th arg is the URL
        assert "x-access-token:test-gh-token@github.com" in url_arg

    async def test_uses_ssh_url_when_protocol_is_ssh(self, investigation_config):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0
        investigation_config.clone_protocol = "ssh"

        with patch("investigator.agent.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            with patch("investigator.agent.tempfile.mkdtemp", return_value="/tmp/test"):
                try:
                    await clone_repo("example-org/repo", investigation_config)
                except Exception:
                    pass

        args = mock_exec.call_args[0]
        url_arg = args[5]  # 6th arg is the URL
        assert url_arg == "git@github.com:example-org/repo.git"

    async def test_ssh_protocol_takes_precedence_over_token(self, investigation_config):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0
        investigation_config.clone_protocol = "ssh"
        investigation_config.github_token = "should-not-be-used"

        with patch("investigator.agent.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            with patch("investigator.agent.tempfile.mkdtemp", return_value="/tmp/test"):
                try:
                    await clone_repo("example-org/repo", investigation_config)
                except Exception:
                    pass

        args = mock_exec.call_args[0]
        url_arg = args[5]  # 6th arg is the URL
        assert url_arg == "git@github.com:example-org/repo.git"

    async def test_clone_failure_raises(self, investigation_config):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"fatal: repository not found")
        mock_proc.returncode = 128

        with patch("investigator.agent.asyncio.create_subprocess_exec", return_value=mock_proc):
            with patch("investigator.agent.tempfile.mkdtemp", return_value="/tmp/test"):
                with pytest.raises(RuntimeError, match="Clone failed"):
                    await clone_repo("example-org/missing-repo", investigation_config)

    async def test_timeout_raises(self, investigation_config):
        with patch(
            "investigator.agent.asyncio.wait_for",
            side_effect=asyncio.TimeoutError,
        ):
            with patch("investigator.agent.tempfile.mkdtemp", return_value="/tmp/test"):
                with patch("investigator.agent.shutil.rmtree"):
                    with pytest.raises(RuntimeError, match="timed out"):
                        await clone_repo("example-org/slow-repo", investigation_config)

    async def test_git_not_found_raises(self, investigation_config):
        with patch(
            "investigator.agent.asyncio.wait_for",
            side_effect=FileNotFoundError,
        ):
            with patch("investigator.agent.tempfile.mkdtemp", return_value="/tmp/test"):
                with patch("investigator.agent.shutil.rmtree"):
                    with pytest.raises(RuntimeError, match="git not found"):
                        await clone_repo("example-org/repo", investigation_config)


# --- Agent invocation ---


class TestRunInvestigationAgent:
    async def test_successful_run(self, sample_bug, investigation_config, tmp_path):
        response = make_investigation_response()
        envelope = json.dumps({"result": response})
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (envelope.encode(), b"")
        mock_proc.returncode = 0

        with patch("investigator.agent.asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await run_investigation_agent(
                sample_bug, "checkout-service", tmp_path, investigation_config
            )

        assert result is not None
        assert result.repo == "checkout-service"
        assert result.confidence == 0.85

    async def test_passes_correct_cli_flags(self, sample_bug, investigation_config, tmp_path):
        envelope = json.dumps({"result": "{}"})
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (envelope.encode(), b"")
        mock_proc.returncode = 0

        with patch("investigator.agent.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await run_investigation_agent(
                sample_bug, "repo", tmp_path, investigation_config
            )

        args = mock_exec.call_args[0]
        assert args[0] == "claude"
        assert "-p" in args
        assert "--output-format" in args
        assert "json" in args
        assert "--model" in args
        assert "claude-sonnet-4-6" in args
        assert "--allowedTools" in args
        assert "--add-dir" in args
        assert str(tmp_path) in args
        assert "--max-budget-usd" in args

    async def test_codex_passes_correct_cli_flags(self, sample_bug, tmp_path):
        response = make_investigation_response()
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0

        config = InvestigationConfig(
            provider="codex",
            model="gpt-5-codex",
            agent_timeout_seconds=60.0,
        )

        async def fake_exec(*args, **kwargs):
            output_idx = args.index("--output-last-message") + 1
            Path(args[output_idx]).write_text(response)
            return mock_proc

        with patch("investigator.agent.asyncio.create_subprocess_exec", side_effect=fake_exec) as mock_exec:
            result = await run_investigation_agent(sample_bug, "repo", tmp_path, config)

        args = mock_exec.call_args[0]
        assert args[0] == "codex"
        assert args[1] == "exec"
        assert "--cd" in args
        assert str(tmp_path) in args
        assert "--model" in args
        assert "gpt-5-codex" in args
        assert "--output-last-message" in args
        assert result is not None
        assert result.repo == "repo"

    async def test_cli_not_found_returns_none(self, sample_bug, investigation_config, tmp_path):
        with patch(
            "investigator.agent.asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError,
        ):
            result = await run_investigation_agent(
                sample_bug, "repo", tmp_path, investigation_config
            )

        assert result is None

    async def test_nonzero_exit_returns_none(self, sample_bug, investigation_config, tmp_path):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"error")
        mock_proc.returncode = 1

        with patch("investigator.agent.asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await run_investigation_agent(
                sample_bug, "repo", tmp_path, investigation_config
            )

        assert result is None

    async def test_timeout_returns_none(self, sample_bug, investigation_config, tmp_path):
        mock_proc = AsyncMock()

        async def slow_communicate():
            raise asyncio.TimeoutError

        mock_proc.communicate = slow_communicate

        with patch("investigator.agent.asyncio.create_subprocess_exec", return_value=mock_proc):
            with patch(
                "investigator.agent.asyncio.wait_for",
                side_effect=asyncio.TimeoutError,
            ):
                result = await run_investigation_agent(
                    sample_bug, "repo", tmp_path, investigation_config
                )

        assert result is None


# --- Aggregation ---


class TestAggregateFindings:
    def _make_result(
        self,
        confidence: float = 0.85,
        has_fix: bool = True,
        repo: str = "checkout-service",
    ) -> InvestigationResult:
        proposed_fix = None
        if has_fix:
            proposed_fix = ProposedFix(
                description="Fix the bug",
                files_changed=[FileChange(path="src/fix.py", diff="+fix")],
            )
        return InvestigationResult(
            repo=repo,
            root_cause_found=True,
            confidence=confidence,
            root_cause="Found it",
            proposed_fix=proposed_fix,
        )

    def test_high_confidence_with_fix_pr(self, sample_bug, investigation_config):
        results = [self._make_result(confidence=0.9, has_fix=True)]
        findings = aggregate_findings(sample_bug, results, investigation_config)
        assert findings.action is not None
        assert findings.action.action_type == "pr"
        assert findings.action.has_fix is True

    def test_high_confidence_no_fix_comment_root_cause(self, sample_bug, investigation_config):
        results = [self._make_result(confidence=0.9, has_fix=False)]
        findings = aggregate_findings(sample_bug, results, investigation_config)
        assert findings.action is not None
        assert findings.action.action_type == "comment_root_cause"

    def test_medium_confidence_comment_uncertain(self, sample_bug, investigation_config):
        results = [self._make_result(confidence=0.6)]
        findings = aggregate_findings(sample_bug, results, investigation_config)
        assert findings.action is not None
        assert findings.action.action_type == "comment_uncertain"

    def test_low_confidence_comment_summary(self, sample_bug, investigation_config):
        results = [self._make_result(confidence=0.3)]
        findings = aggregate_findings(sample_bug, results, investigation_config)
        assert findings.action is not None
        assert findings.action.action_type == "comment_summary"

    def test_empty_results_comment_summary(self, sample_bug, investigation_config):
        findings = aggregate_findings(sample_bug, [], investigation_config)
        assert findings.action is not None
        assert findings.action.action_type == "comment_summary"
        assert findings.best_result is None

    def test_picks_highest_confidence(self, sample_bug, investigation_config):
        results = [
            self._make_result(confidence=0.6, repo="low"),
            self._make_result(confidence=0.95, repo="high"),
            self._make_result(confidence=0.75, repo="mid"),
        ]
        # Results should be sorted by confidence desc (done by investigate_repos)
        # but aggregate_findings picks the first one
        sorted_results = sorted(results, key=lambda r: r.confidence, reverse=True)
        findings = aggregate_findings(sample_bug, sorted_results, investigation_config)
        assert findings.best_result is not None
        assert findings.best_result.repo == "high"

    def test_fix_with_empty_files_not_counted(self, sample_bug, investigation_config):
        result = InvestigationResult(
            repo="repo",
            confidence=0.9,
            proposed_fix=ProposedFix(description="Fix", files_changed=[]),
        )
        findings = aggregate_findings(sample_bug, [result], investigation_config)
        assert findings.action is not None
        assert findings.action.action_type == "comment_root_cause"
        assert findings.action.has_fix is False

    def test_boundary_at_0_8(self, sample_bug, investigation_config):
        results = [self._make_result(confidence=0.8, has_fix=True)]
        findings = aggregate_findings(sample_bug, results, investigation_config)
        assert findings.action is not None
        assert findings.action.action_type == "pr"

    def test_boundary_just_below_0_8(self, sample_bug, investigation_config):
        results = [self._make_result(confidence=0.79, has_fix=True)]
        findings = aggregate_findings(sample_bug, results, investigation_config)
        assert findings.action is not None
        assert findings.action.action_type == "comment_uncertain"

    def test_boundary_at_0_5(self, sample_bug, investigation_config):
        results = [self._make_result(confidence=0.5)]
        findings = aggregate_findings(sample_bug, results, investigation_config)
        assert findings.action is not None
        assert findings.action.action_type == "comment_uncertain"

    def test_boundary_just_below_0_5(self, sample_bug, investigation_config):
        results = [self._make_result(confidence=0.49)]
        findings = aggregate_findings(sample_bug, results, investigation_config)
        assert findings.action is not None
        assert findings.action.action_type == "comment_summary"


# --- Orchestration ---


class TestInvestigateRepos:
    async def test_investigates_all_triaged_repos(self, sample_bug, sample_repos, investigation_config):
        triage_results = [
            TriageResult(repo="checkout-service", confidence=0.9, reasoning="Match"),
            TriageResult(repo="subscription-engine", confidence=0.7, reasoning="Related"),
        ]

        async def fake_clone(slug, config):
            return Path(f"/tmp/fake/{slug.split('/')[-1]}")

        response = make_investigation_response()
        envelope = json.dumps({"result": response})
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (envelope.encode(), b"")
        mock_proc.returncode = 0

        with patch("investigator.agent.clone_repo", side_effect=fake_clone):
            with patch("investigator.agent.asyncio.create_subprocess_exec", return_value=mock_proc):
                with patch("investigator.agent.shutil.rmtree"):
                    results = await investigate_repos(
                        sample_bug, triage_results, sample_repos, investigation_config
                    )

        assert len(results) == 2

    async def test_skips_repos_without_github_slug(self, sample_bug, investigation_config):
        repos = [Repository(name="no-slug-repo")]
        triage_results = [TriageResult(repo="no-slug-repo", confidence=0.9)]

        results = await investigate_repos(
            sample_bug, triage_results, repos, investigation_config
        )

        assert len(results) == 0

    async def test_respects_max_parallel_agents(self, sample_bug, sample_repos, investigation_config):
        investigation_config.max_parallel_agents = 1
        triage_results = [
            TriageResult(repo="checkout-service", confidence=0.9),
            TriageResult(repo="subscription-engine", confidence=0.7),
        ]

        concurrent_count = 0
        max_concurrent = 0

        async def tracking_clone(slug, config):
            nonlocal concurrent_count, max_concurrent
            concurrent_count += 1
            max_concurrent = max(max_concurrent, concurrent_count)
            await asyncio.sleep(0.01)
            concurrent_count -= 1
            return Path(f"/tmp/fake/{slug.split('/')[-1]}")

        async def fake_agent(bug, repo_name, repo_dir, config):
            return InvestigationResult(repo=repo_name, confidence=0.5)

        with patch("investigator.agent.clone_repo", side_effect=tracking_clone):
            with patch("investigator.agent.run_investigation_agent", side_effect=fake_agent):
                with patch("investigator.agent.shutil.rmtree"):
                    await investigate_repos(
                        sample_bug, triage_results, sample_repos, investigation_config
                    )

        assert max_concurrent <= 1

    async def test_returns_sorted_by_confidence(self, sample_bug, sample_repos, investigation_config):
        triage_results = [
            TriageResult(repo="checkout-service", confidence=0.9),
            TriageResult(repo="subscription-engine", confidence=0.7),
        ]

        call_count = 0

        async def fake_clone(slug, config):
            return Path(f"/tmp/fake/{slug.split('/')[-1]}")

        async def fake_agent(bug, repo_name, repo_dir, config):
            nonlocal call_count
            call_count += 1
            # Return in reverse order of expected
            conf = 0.6 if repo_name == "checkout-service" else 0.9
            return InvestigationResult(repo=repo_name, confidence=conf)

        with patch("investigator.agent.clone_repo", side_effect=fake_clone):
            with patch("investigator.agent.run_investigation_agent", side_effect=fake_agent):
                with patch("investigator.agent.shutil.rmtree"):
                    results = await investigate_repos(
                        sample_bug, triage_results, sample_repos, investigation_config
                    )

        assert len(results) == 2
        assert results[0].confidence >= results[1].confidence

    async def test_only_investigates_top_three_triage_results(self, sample_bug, investigation_config):
        repos = [
            Repository(name="repo-a", github_slug="example-org/repo-a"),
            Repository(name="repo-b", github_slug="example-org/repo-b"),
            Repository(name="repo-c", github_slug="example-org/repo-c"),
            Repository(name="repo-d", github_slug="example-org/repo-d"),
        ]
        triage_results = [
            TriageResult(repo="repo-a", confidence=0.40),
            TriageResult(repo="repo-b", confidence=0.95),
            TriageResult(repo="repo-c", confidence=0.80),
            TriageResult(repo="repo-d", confidence=0.70),
        ]

        investigated_repos: list[str] = []

        async def fake_clone(slug, config):
            return Path(f"/tmp/fake/{slug.split('/')[-1]}")

        async def fake_agent(bug, repo_name, repo_dir, config):
            investigated_repos.append(repo_name)
            return InvestigationResult(repo=repo_name, confidence=0.5)

        with patch("investigator.agent.clone_repo", side_effect=fake_clone):
            with patch("investigator.agent.run_investigation_agent", side_effect=fake_agent):
                with patch("investigator.agent.shutil.rmtree"):
                    await investigate_repos(
                        sample_bug, triage_results, repos, investigation_config
                    )

        assert len(investigated_repos) == 3
        assert set(investigated_repos) == {"repo-b", "repo-c", "repo-d"}
