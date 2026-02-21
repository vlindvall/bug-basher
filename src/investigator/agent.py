import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

from shared.config import InvestigationConfig
from shared.models import (
    AggregatedFindings,
    Action,
    BugReport,
    FileChange,
    InvestigationResult,
    ProposedFix,
    Repository,
    TriageResult,
)

logger = logging.getLogger(__name__)
DEFAULT_CLAUDE_INVESTIGATION_MODEL = "claude-sonnet-4-6"


def build_investigation_prompt(bug: BugReport, repo_name: str) -> str:
    lines = [
        "You are a senior software engineer investigating a production bug. "
        "You have access to a cloned repository. Explore the codebase to find "
        "the root cause and, if possible, propose a fix.",
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
    lines.append(f"## Repository: {repo_name}")
    lines.append("")
    lines.append(
        "Investigate this repository for the root cause of the bug above. "
        "Use `git log`, `Grep`, `Glob`, and `Read` to explore the code. "
        "Look at recent commits, relevant source files, error handling, "
        "and test coverage."
    )
    lines.append("")
    lines.append(
        "Respond with a single JSON object (no markdown fences) with these fields:\n"
        "{\n"
        '  "root_cause_found": bool,\n'
        '  "confidence": float (0.0-1.0),\n'
        '  "root_cause": "description of root cause",\n'
        '  "evidence": ["list of evidence found"],\n'
        '  "recent_suspect_commits": ["commit hashes"],\n'
        '  "proposed_fix": {\n'
        '    "description": "what the fix does",\n'
        '    "files_changed": [{"path": "src/file.py", "diff": "unified diff"}]\n'
        "  },\n"
        '  "next_steps": ["suggested follow-up actions"]\n'
        "}"
    )
    return "\n".join(lines)


def _extract_json_object(text: str) -> str | None:
    """Extract a JSON object from text, handling markdown fences and prose."""
    # Try ```json ... ``` fences first
    match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if match:
        candidate = match.group(1).strip()
        if candidate.startswith("{"):
            return candidate
    # Try bare JSON object
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return match.group(0).strip()
    return None


def parse_investigation_response(
    raw: str, repo_name: str
) -> InvestigationResult | None:
    extracted = _extract_json_object(raw)
    if not extracted:
        logger.warning("No JSON object found in investigation response")
        return None

    try:
        data = json.loads(extracted)
    except json.JSONDecodeError:
        logger.warning("Failed to parse investigation JSON: %s", extracted[:200])
        return None

    if not isinstance(data, dict):
        logger.warning("Investigation response is not a JSON object")
        return None

    # Parse proposed_fix if present
    proposed_fix = None
    if "proposed_fix" in data and isinstance(data["proposed_fix"], dict):
        fix_data = data["proposed_fix"]
        files_changed = []
        for fc in fix_data.get("files_changed", []):
            if isinstance(fc, dict) and "path" in fc:
                files_changed.append(FileChange(path=fc["path"], diff=fc.get("diff", "")))
        proposed_fix = ProposedFix(
            description=fix_data.get("description", ""),
            files_changed=files_changed,
        )

    try:
        return InvestigationResult(
            repo=repo_name,
            root_cause_found=bool(data.get("root_cause_found", False)),
            confidence=float(data.get("confidence", 0.0)),
            root_cause=str(data.get("root_cause", "")),
            evidence=list(data.get("evidence", [])),
            recent_suspect_commits=list(data.get("recent_suspect_commits", [])),
            proposed_fix=proposed_fix,
            next_steps=list(data.get("next_steps", [])),
        )
    except (ValueError, TypeError) as e:
        logger.warning("Failed to build InvestigationResult: %s", e)
        return None


async def clone_repo(github_slug: str, config: InvestigationConfig) -> Path:
    """Shallow-clone a repo to a temp directory. Raises RuntimeError on failure."""
    tmpdir = Path(tempfile.mkdtemp(prefix="bug-basher-"))
    repo_name = github_slug.split("/")[-1]
    dest = tmpdir / repo_name

    if config.github_token:
        url = f"https://x-access-token:{config.github_token}@github.com/{github_slug}.git"
    else:
        url = f"https://github.com/{github_slug}.git"

    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                "git",
                "clone",
                "--depth",
                str(config.clone_depth),
                "--single-branch",
                url,
                str(dest),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            timeout=config.clone_timeout_seconds,
        )
        stdout, stderr = await proc.communicate()
    except FileNotFoundError:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RuntimeError("git not found")
    except (asyncio.TimeoutError, TimeoutError):
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RuntimeError(f"Clone timed out after {config.clone_timeout_seconds}s")

    if proc.returncode != 0:
        error_msg = stderr.decode().strip()
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RuntimeError(f"Clone failed: {error_msg}")

    return dest


async def run_investigation_agent(
    bug: BugReport,
    repo_name: str,
    repo_dir: Path,
    config: InvestigationConfig,
) -> InvestigationResult | None:
    """Run configured CLI agent against a cloned repo."""
    prompt = build_investigation_prompt(bug, repo_name)
    provider = _normalize_provider(config.provider)
    output_path: Path | None = None

    try:
        raw_output = ""
        if provider == "codex":
            fd, path = tempfile.mkstemp(prefix="bug-basher-investigation-", suffix=".txt")
            os.close(fd)
            output_path = Path(path)
        cmd = _build_agent_command(prompt, repo_dir, config, output_path=output_path)
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=config.agent_timeout_seconds,
        )
        if proc.returncode != 0:
            logger.error("%s CLI exited with %d for %s: %s", provider, proc.returncode, repo_name, stderr.decode())
            return None

        if provider == "codex" and output_path is not None:
            raw_output = output_path.read_text() if output_path.is_file() else ""
        if not raw_output:
            raw_output = stdout.decode()

        if provider == "claude":
            # Unwrap {"result": "..."} envelope from claude CLI JSON output
            try:
                envelope = json.loads(raw_output)
                if isinstance(envelope, dict) and "result" in envelope:
                    raw_output = envelope["result"]
            except json.JSONDecodeError:
                pass

        return parse_investigation_response(raw_output, repo_name)
    except FileNotFoundError:
        logger.error("%s CLI not found", provider)
        return None
    except (asyncio.TimeoutError, TimeoutError):
        logger.error("Agent timed out for %s", repo_name)
        return None
    finally:
        if output_path is not None and output_path.exists():
            output_path.unlink(missing_ok=True)


def _normalize_provider(provider: str) -> str:
    normalized = provider.lower()
    if normalized not in {"claude", "codex"}:
        raise ValueError(f"Unsupported provider: {provider}")
    return normalized


def _build_agent_command(
    prompt: str,
    repo_dir: Path,
    config: InvestigationConfig,
    *,
    output_path: Path | None = None,
) -> list[str]:
    provider = _normalize_provider(config.provider)
    if provider == "claude":
        model = config.model or DEFAULT_CLAUDE_INVESTIGATION_MODEL
        return [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "json",
            "--model",
            model,
            "--allowedTools",
            "Bash(git log:*),Read,Grep,Glob",
            "--add-dir",
            str(repo_dir),
            "--max-budget-usd",
            str(config.max_budget_usd),
        ]

    cmd = [
        "codex",
        "exec",
        prompt,
        "--cd",
        str(repo_dir),
        "--color",
        "never",
    ]
    if config.model:
        cmd.extend(["--model", config.model])
    if output_path is not None:
        cmd.extend(["--output-last-message", str(output_path)])
    return cmd


async def investigate_repos(
    bug: BugReport,
    triage_results: list[TriageResult],
    repos: list[Repository],
    config: InvestigationConfig,
) -> list[InvestigationResult]:
    """Clone and investigate triaged repos in parallel."""
    slug_map: dict[str, str] = {r.name: r.github_slug for r in repos if r.github_slug}

    async def _investigate_single(triage_result: TriageResult) -> InvestigationResult | None:
        repo_name = triage_result.repo
        slug = slug_map.get(repo_name)
        if not slug:
            logger.warning("No github_slug for repo %s, skipping", repo_name)
            return None

        repo_dir: Path | None = None
        try:
            repo_dir = await clone_repo(slug, config)
            return await run_investigation_agent(bug, repo_name, repo_dir, config)
        except RuntimeError as e:
            logger.error("Failed to clone %s: %s", slug, e)
            return None
        finally:
            if repo_dir is not None:
                shutil.rmtree(repo_dir.parent, ignore_errors=True)

    semaphore = asyncio.Semaphore(config.max_parallel_agents)

    async def _throttled(tr: TriageResult) -> InvestigationResult | None:
        async with semaphore:
            return await _investigate_single(tr)

    tasks = [_throttled(tr) for tr in triage_results]
    raw_results = await asyncio.gather(*tasks)

    results = [r for r in raw_results if r is not None]
    results.sort(key=lambda r: r.confidence, reverse=True)
    return results


def aggregate_findings(
    bug: BugReport,
    results: list[InvestigationResult],
    config: InvestigationConfig,
) -> AggregatedFindings:
    """Decide action based on investigation results."""
    best = results[0] if results else None

    if best is None:
        action = Action(action_type="comment_summary", confidence=0.0, has_fix=False)
    else:
        has_fix = (
            best.proposed_fix is not None
            and len(best.proposed_fix.files_changed) > 0
        )
        if best.confidence >= config.high_confidence_threshold:
            if has_fix:
                action_type = "pr"
            else:
                action_type = "comment_root_cause"
        elif best.confidence >= config.uncertain_confidence_threshold:
            action_type = "comment_uncertain"
        else:
            action_type = "comment_summary"

        action = Action(
            action_type=action_type,
            confidence=best.confidence,
            has_fix=has_fix,
        )

    return AggregatedFindings(
        bug=bug,
        results=results,
        best_result=best,
        action=action,
    )
