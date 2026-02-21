import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from investigator.triage import (
    HaikuTriageClient,
    SubprocessTriageClient,
    build_triage_prompt,
    create_triage_client,
    parse_triage_response,
)
from shared.config import TriageConfig
from shared.models import BugReport, Repository


# --- Prompt building ---


class TestBuildTriagePrompt:
    def test_includes_bug_details(self, sample_bug, sample_repos):
        prompt = build_triage_prompt(sample_bug, sample_repos)
        assert "BUY-1234" in prompt
        assert "Checkout fails for subscription items" in prompt
        assert "500 error" in prompt
        assert "P1" in prompt

    def test_includes_repo_details(self, sample_bug, sample_repos):
        prompt = build_triage_prompt(sample_bug, sample_repos)
        assert "checkout-service" in prompt
        assert "Handles checkout flow" in prompt
        assert "example-org/checkout-service" in prompt

    def test_empty_repos(self, sample_bug):
        prompt = build_triage_prompt(sample_bug, [])
        assert "## Repositories" in prompt
        assert "BUY-1234" in prompt

    def test_empty_components(self, sample_repos):
        bug = BugReport(jira_key="BUG-1", summary="A bug")
        prompt = build_triage_prompt(bug, sample_repos)
        assert "Components" not in prompt

    def test_repo_with_no_optional_fields(self, sample_bug):
        repo = Repository(name="bare-repo")
        prompt = build_triage_prompt(sample_bug, [repo])
        assert "bare-repo" in prompt


# --- Response parsing ---


class TestParseTriageResponse:
    def test_valid_json(self, triage_config):
        raw = json.dumps([
            {"repo": "checkout-service", "confidence": 0.9, "reasoning": "Direct match"},
            {"repo": "product-catalog", "confidence": 0.4, "reasoning": "Possible"},
        ])
        results = parse_triage_response(raw, triage_config)
        assert len(results) == 2
        assert results[0].repo == "checkout-service"
        assert results[0].confidence == 0.9

    def test_markdown_wrapped(self, triage_config):
        raw = '```json\n[{"repo": "svc", "confidence": 0.8}]\n```'
        results = parse_triage_response(raw, triage_config)
        assert len(results) == 1
        assert results[0].repo == "svc"

    def test_markdown_without_lang_tag(self, triage_config):
        raw = '```\n[{"repo": "svc", "confidence": 0.7}]\n```'
        results = parse_triage_response(raw, triage_config)
        assert len(results) == 1

    def test_malformed_json(self, triage_config):
        raw = "[{broken json"
        results = parse_triage_response(raw, triage_config)
        assert results == []

    def test_object_not_array(self, triage_config):
        raw = '{"repo": "svc", "confidence": 0.9}'
        results = parse_triage_response(raw, triage_config)
        assert results == []

    def test_empty_array(self, triage_config):
        results = parse_triage_response("[]", triage_config)
        assert results == []

    def test_filters_below_min_confidence(self, triage_config):
        raw = json.dumps([
            {"repo": "high", "confidence": 0.8},
            {"repo": "low", "confidence": 0.1},
        ])
        results = parse_triage_response(raw, triage_config)
        assert len(results) == 1
        assert results[0].repo == "high"

    def test_limits_to_max_repos(self):
        config = TriageConfig(max_repos=2, min_confidence=0.0)
        raw = json.dumps([
            {"repo": f"repo-{i}", "confidence": 0.5}
            for i in range(10)
        ])
        results = parse_triage_response(raw, config)
        assert len(results) == 2

    def test_sorts_descending(self, triage_config):
        raw = json.dumps([
            {"repo": "low", "confidence": 0.4},
            {"repo": "high", "confidence": 0.9},
            {"repo": "mid", "confidence": 0.6},
        ])
        results = parse_triage_response(raw, triage_config)
        assert [r.repo for r in results] == ["high", "mid", "low"]

    def test_skips_invalid_items(self, triage_config):
        raw = json.dumps([
            {"repo": "valid", "confidence": 0.8},
            "not a dict",
            {"missing_confidence": True},
            {"repo": "also-valid", "confidence": 0.5},
        ])
        results = parse_triage_response(raw, triage_config)
        assert len(results) == 2

    def test_rejects_out_of_range_confidence(self, triage_config):
        raw = json.dumps([
            {"repo": "good", "confidence": 0.8},
            {"repo": "too-high", "confidence": 1.5},
            {"repo": "negative", "confidence": -0.1},
        ])
        results = parse_triage_response(raw, triage_config)
        assert len(results) == 1
        assert results[0].repo == "good"

    def test_json_embedded_in_prose(self, triage_config):
        raw = (
            "Here are the results:\n"
            '[{"repo": "checkout-service", "confidence": 0.85, "reasoning": "Likely"}]\n'
            "Let me know if you need more details."
        )
        results = parse_triage_response(raw, triage_config)
        assert len(results) == 1
        assert results[0].repo == "checkout-service"


# --- HaikuTriageClient ---


class TestHaikuTriageClient:
    def test_raises_without_api_key(self):
        config = TriageConfig(provider="claude", anthropic_api_key="")
        with pytest.raises(ValueError, match="anthropic_api_key is required"):
            HaikuTriageClient(config)

    @respx.mock
    async def test_successful_triage(self, triage_config, sample_bug, sample_repos):
        response_body = {
            "content": [{"type": "text", "text": json.dumps([
                {"repo": "checkout-service", "confidence": 0.9, "reasoning": "Direct match"},
            ])}],
        }
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(200, json=response_body)
        )

        client = HaikuTriageClient(triage_config)
        results = await client.triage(sample_bug, sample_repos)
        assert len(results) == 1
        assert results[0].repo == "checkout-service"

    @respx.mock
    async def test_sends_correct_headers_and_model(self, triage_config, sample_bug, sample_repos):
        response_body = {"content": [{"type": "text", "text": "[]"}]}
        route = respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(200, json=response_body)
        )

        client = HaikuTriageClient(triage_config)
        await client.triage(sample_bug, sample_repos)

        request = route.calls.last.request
        assert request.headers["x-api-key"] == "test-key"
        assert request.headers["anthropic-version"] == "2023-06-01"
        body = json.loads(request.content)
        assert body["model"] == "claude-haiku-4-5-20251001"

    @respx.mock
    async def test_api_error_returns_empty(self, triage_config, sample_bug, sample_repos):
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(500, json={"error": "Internal server error"})
        )

        client = HaikuTriageClient(triage_config)
        results = await client.triage(sample_bug, sample_repos)
        assert results == []


# --- SubprocessTriageClient ---


class TestSubprocessTriageClient:
    async def test_successful_triage(self, triage_config, sample_bug, sample_repos):
        envelope = json.dumps({
            "result": json.dumps([
                {"repo": "checkout-service", "confidence": 0.9, "reasoning": "Match"},
            ])
        })
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (envelope.encode(), b"")
        mock_proc.returncode = 0

        config = TriageConfig(provider="claude", use_subprocess=True)
        client = SubprocessTriageClient(config)

        with patch("investigator.triage.asyncio.create_subprocess_exec", return_value=mock_proc):
            results = await client.triage(sample_bug, sample_repos)

        assert len(results) == 1
        assert results[0].repo == "checkout-service"

    async def test_passes_correct_cli_args(self, triage_config, sample_bug, sample_repos):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"[]", b"")
        mock_proc.returncode = 0

        config = TriageConfig(provider="claude", use_subprocess=True)
        client = SubprocessTriageClient(config)

        with patch("investigator.triage.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await client.triage(sample_bug, sample_repos)

        args = mock_exec.call_args[0]
        assert args[0] == "claude"
        assert "-p" in args
        assert "--output-format" in args
        assert "json" in args

    async def test_codex_passes_correct_cli_args(self, sample_bug, sample_repos):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0

        config = TriageConfig(provider="codex")
        client = SubprocessTriageClient(config)

        async def fake_exec(*args, **kwargs):
            output_idx = args.index("--output-last-message") + 1
            Path(args[output_idx]).write_text("[]")
            return mock_proc

        with patch("investigator.triage.asyncio.create_subprocess_exec", side_effect=fake_exec) as mock_exec:
            await client.triage(sample_bug, sample_repos)

        args = mock_exec.call_args[0]
        assert args[0] == "codex"
        assert args[1] == "exec"
        assert "--color" in args
        assert "--output-last-message" in args

    async def test_codex_reads_output_last_message_file(self, sample_bug, sample_repos):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0

        config = TriageConfig(provider="codex")
        client = SubprocessTriageClient(config)

        response = json.dumps([{"repo": "checkout-service", "confidence": 0.9, "reasoning": "Match"}])

        async def fake_exec(*args, **kwargs):
            output_idx = args.index("--output-last-message") + 1
            Path(args[output_idx]).write_text(response)
            return mock_proc

        with patch("investigator.triage.asyncio.create_subprocess_exec", side_effect=fake_exec):
            results = await client.triage(sample_bug, sample_repos)

        assert len(results) == 1
        assert results[0].repo == "checkout-service"

    async def test_cli_not_found_returns_empty(self, sample_bug, sample_repos):
        config = TriageConfig(provider="claude", use_subprocess=True)
        client = SubprocessTriageClient(config)

        with patch(
            "investigator.triage.asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError,
        ):
            results = await client.triage(sample_bug, sample_repos)

        assert results == []

    async def test_nonzero_exit_returns_empty(self, sample_bug, sample_repos):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"error occurred")
        mock_proc.returncode = 1

        config = TriageConfig(provider="claude", use_subprocess=True)
        client = SubprocessTriageClient(config)

        with patch("investigator.triage.asyncio.create_subprocess_exec", return_value=mock_proc):
            results = await client.triage(sample_bug, sample_repos)

        assert results == []


# --- Factory ---


class TestCreateTriageClient:
    def test_creates_subprocess_client(self):
        config = TriageConfig(provider="claude", use_subprocess=True)
        client = create_triage_client(config)
        assert isinstance(client, SubprocessTriageClient)

    def test_creates_haiku_client(self):
        config = TriageConfig(provider="claude", anthropic_api_key="key")
        client = create_triage_client(config)
        assert isinstance(client, HaikuTriageClient)

    def test_codex_provider_uses_subprocess_client(self):
        config = TriageConfig(provider="codex")
        client = create_triage_client(config)
        assert isinstance(client, SubprocessTriageClient)
