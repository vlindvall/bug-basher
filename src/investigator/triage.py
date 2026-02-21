import asyncio
import json
import logging
import os
import re
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

import httpx

from shared.config import TriageConfig
from shared.models import BugReport, Repository, TriageResult

logger = logging.getLogger(__name__)
DEFAULT_CLAUDE_TRIAGE_MODEL = "claude-haiku-4-5-20251001"


def build_triage_prompt(bug: BugReport, repos: list[Repository]) -> str:
    lines = [
        "You are a bug triage assistant. Given a bug report and a list of repositories, "
        "rank which repositories are most likely to contain the root cause.",
        "",
        "## Bug Report",
        f"**Key:** {bug.jira_key}",
        f"**Summary:** {bug.summary}",
    ]
    if bug.description:
        lines.append(f"**Description:** {bug.description}")
    if bug.priority:
        lines.append(f"**Priority:** {bug.priority}")
    if bug.components:
        lines.append(f"**Components:** {', '.join(bug.components)}")
    if bug.labels:
        lines.append(f"**Labels:** {', '.join(bug.labels)}")

    lines.append("")
    lines.append("## Repositories")
    for repo in repos:
        parts = [f"- **{repo.name}**"]
        if repo.description:
            parts.append(f": {repo.description}")
        details = []
        if repo.github_slug:
            details.append(f"slug={repo.github_slug}")
        if repo.component_type:
            details.append(f"type={repo.component_type}")
        if repo.tags:
            details.append(f"tags={','.join(repo.tags)}")
        if details:
            parts.append(f" ({'; '.join(details)})")
        lines.append("".join(parts))

    lines.append("")
    lines.append(
        "Respond with a JSON array of objects, each with "
        '"repo" (repository name), "confidence" (0.0 to 1.0), '
        'and "reasoning" (brief explanation). '
        "Sort by confidence descending. Only include repositories "
        "that might be relevant."
    )
    return "\n".join(lines)


def _extract_json(text: str) -> str | None:
    # Try ```json ... ``` fences first
    match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    # Try bare JSON array
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        return match.group(0).strip()
    return None


def parse_triage_response(
    raw: str, config: TriageConfig
) -> list[TriageResult]:
    extracted = _extract_json(raw)
    if not extracted:
        logger.warning("No JSON found in triage response")
        return []

    try:
        data = json.loads(extracted)
    except json.JSONDecodeError:
        logger.warning("Failed to parse triage JSON: %s", extracted[:200])
        return []

    if not isinstance(data, list):
        logger.warning("Triage response is not a JSON array")
        return []

    results: list[TriageResult] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if "repo" not in item or "confidence" not in item:
            continue
        try:
            result = TriageResult(**item)
            results.append(result)
        except (ValueError, TypeError):
            continue

    results = [r for r in results if r.confidence >= config.min_confidence]
    results.sort(key=lambda r: r.confidence, reverse=True)
    return results[: config.max_repos]


class TriageClient(ABC):
    @abstractmethod
    async def triage(
        self, bug: BugReport, repos: list[Repository]
    ) -> list[TriageResult]:
        pass


class HaikuTriageClient(TriageClient):
    def __init__(self, config: TriageConfig) -> None:
        if _normalize_provider(config.provider) != "claude":
            raise ValueError("HaikuTriageClient only supports provider='claude'")
        if not config.anthropic_api_key:
            raise ValueError("anthropic_api_key is required for HaikuTriageClient")
        self.config = config

    async def triage(
        self, bug: BugReport, repos: list[Repository]
    ) -> list[TriageResult]:
        prompt = build_triage_prompt(bug, repos)
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": self.config.anthropic_api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": self.config.model or DEFAULT_CLAUDE_TRIAGE_MODEL,
                        "max_tokens": 1024,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    timeout=30.0,
                )
                response.raise_for_status()
            except httpx.HTTPError as e:
                logger.error("Anthropic API error: %s", e)
                return []

        body = response.json()
        raw = body["content"][0]["text"]
        return parse_triage_response(raw, self.config)


class SubprocessTriageClient(TriageClient):
    def __init__(self, config: TriageConfig) -> None:
        _normalize_provider(config.provider)
        self.config = config

    async def triage(
        self, bug: BugReport, repos: list[Repository]
    ) -> list[TriageResult]:
        prompt = build_triage_prompt(bug, repos)
        provider = _normalize_provider(self.config.provider)
        output_path: Path | None = None
        try:
            raw_output = ""
            if provider == "codex":
                fd, path = tempfile.mkstemp(prefix="bug-basher-triage-", suffix=".txt")
                os.close(fd)
                output_path = Path(path)
            cmd = _build_subprocess_command(prompt, self.config, output_path=output_path)
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                logger.error("%s CLI exited with %d: %s", provider, proc.returncode, stderr.decode())
                return []

            if provider == "codex" and output_path is not None:
                raw_output = output_path.read_text() if output_path.is_file() else ""
            if not raw_output:
                raw_output = stdout.decode()

            if provider == "claude":
                # Claude CLI JSON output wraps result in {"result": "..."}
                try:
                    envelope = json.loads(raw_output)
                    if isinstance(envelope, dict) and "result" in envelope:
                        raw_output = envelope["result"]
                except json.JSONDecodeError:
                    pass  # Use raw output as-is

            return parse_triage_response(raw_output, self.config)
        except FileNotFoundError:
            logger.error("%s CLI not found", provider)
            return []
        finally:
            if output_path is not None and output_path.exists():
                output_path.unlink(missing_ok=True)


def create_triage_client(config: TriageConfig) -> TriageClient:
    provider = _normalize_provider(config.provider)
    if provider == "codex":
        return SubprocessTriageClient(config)
    if config.use_subprocess:
        return SubprocessTriageClient(config)
    return HaikuTriageClient(config)


def _normalize_provider(provider: str) -> str:
    normalized = provider.lower()
    if normalized not in {"claude", "codex"}:
        raise ValueError(f"Unsupported provider: {provider}")
    return normalized


def _build_subprocess_command(
    prompt: str,
    config: TriageConfig,
    *,
    output_path: Path | None = None,
) -> list[str]:
    provider = _normalize_provider(config.provider)
    if provider == "claude":
        cmd = ["claude", "-p", prompt, "--output-format", "json"]
        if config.model:
            cmd.extend(["--model", config.model])
        return cmd

    cmd = ["codex", "exec", prompt, "--color", "never"]
    if config.model:
        cmd.extend(["--model", config.model])
    if output_path is not None:
        cmd.extend(["--output-last-message", str(output_path)])
    return cmd
