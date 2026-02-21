from unittest.mock import AsyncMock

from investigator.reporter import (
    create_pr_from_findings,
    format_jira_comment,
    format_pr_body,
    format_pr_title,
    format_slack_message,
    report_findings,
)
from shared.models import Repository
from tests.conftest import make_aggregated_findings


# ---------------------------------------------------------------------------
# format_pr_title
# ---------------------------------------------------------------------------


class TestFormatPrTitle:
    def test_basic_title(self):
        findings = make_aggregated_findings()
        title = format_pr_title(findings)
        assert title.startswith("[Bug Basher] BUY-1234:")
        assert "Checkout fails" in title

    def test_truncation(self):
        findings = make_aggregated_findings(
            summary="A" * 100,
        )
        title = format_pr_title(findings)
        assert len(title) <= 72
        assert title.endswith("...")


# ---------------------------------------------------------------------------
# format_pr_body
# ---------------------------------------------------------------------------


class TestFormatPrBody:
    def test_includes_bug_link(self):
        findings = make_aggregated_findings()
        body = format_pr_body(findings)
        assert "BUY-1234" in body
        assert "jira.example.com" in body

    def test_includes_root_cause(self):
        findings = make_aggregated_findings(root_cause="Null pointer in checkout handler")
        body = format_pr_body(findings)
        assert "Null pointer in checkout handler" in body

    def test_includes_evidence(self):
        findings = make_aggregated_findings(evidence=["Stack trace in logs"])
        body = format_pr_body(findings)
        assert "Stack trace in logs" in body

    def test_includes_confidence(self):
        findings = make_aggregated_findings(confidence=0.85)
        body = format_pr_body(findings)
        assert "85%" in body

    def test_includes_auto_generated_notice(self):
        findings = make_aggregated_findings()
        body = format_pr_body(findings)
        assert "auto-generated" in body


# ---------------------------------------------------------------------------
# format_jira_comment
# ---------------------------------------------------------------------------


class TestFormatJiraComment:
    def test_valid_adf(self):
        findings = make_aggregated_findings()
        doc = format_jira_comment(findings)
        assert doc["type"] == "doc"
        assert doc["version"] == 1
        assert len(doc["content"]) > 0

    def test_includes_pr_url(self):
        findings = make_aggregated_findings()
        doc = format_jira_comment(findings, pr_url="https://github.com/org/repo/pull/1")
        texts = _extract_all_text(doc)
        assert any("https://github.com/org/repo/pull/1" in t for t in texts)

    def test_includes_next_steps_for_uncertain(self):
        findings = make_aggregated_findings(
            action_type="comment_uncertain",
            confidence=0.6,
            has_fix=False,
            next_steps=["Check logs", "Review config"],
        )
        doc = format_jira_comment(findings)
        texts = _extract_all_text(doc)
        assert any("Check logs" in t for t in texts)

    def test_no_next_steps_for_pr(self):
        findings = make_aggregated_findings(action_type="pr")
        doc = format_jira_comment(findings)
        headings = _extract_headings(doc)
        assert "Suggested Next Steps" not in headings


# ---------------------------------------------------------------------------
# format_slack_message
# ---------------------------------------------------------------------------


class TestFormatSlackMessage:
    def test_text_and_blocks(self):
        findings = make_aggregated_findings()
        text, blocks = format_slack_message(findings)
        assert "BUY-1234" in text
        assert len(blocks) >= 2

    def test_pr_link(self):
        findings = make_aggregated_findings()
        text, blocks = format_slack_message(
            findings, pr_url="https://github.com/org/repo/pull/1"
        )
        block_text = _blocks_to_text(blocks)
        assert "https://github.com/org/repo/pull/1" in block_text

    def test_truncates_long_root_cause(self):
        findings = make_aggregated_findings(root_cause="A" * 300)
        _, blocks = format_slack_message(findings)
        block_text = _blocks_to_text(blocks)
        assert "..." in block_text
        # Root cause block text should be under 250 chars including label
        for b in blocks:
            if b.get("type") == "section" and "Root Cause" in str(b.get("text", {})):
                assert len(b["text"]["text"]) < 250


# ---------------------------------------------------------------------------
# create_pr_from_findings
# ---------------------------------------------------------------------------


class TestCreatePrFromFindings:
    async def test_full_success(self):
        findings = make_aggregated_findings()
        repos = [
            Repository(
                name="checkout-service",
                github_slug="example-org/checkout-service",
            )
        ]
        mock_client = AsyncMock()
        mock_client.get_default_branch.return_value = "main"
        mock_client.get_branch_sha.return_value = "abc123"
        mock_client.get_file_content.return_value = ("old content", "old-sha")
        mock_client.create_pull_request.return_value = {
            "html_url": "https://github.com/example-org/checkout-service/pull/42"
        }

        result = await create_pr_from_findings(findings, repos, mock_client)

        assert result == "https://github.com/example-org/checkout-service/pull/42"
        mock_client.create_branch.assert_called_once()
        mock_client.update_file.assert_called_once()
        mock_client.create_pull_request.assert_called_once()

    async def test_returns_none_without_fix(self):
        findings = make_aggregated_findings(has_fix=False)
        result = await create_pr_from_findings(findings, [], AsyncMock())
        assert result is None

    async def test_returns_none_without_slug(self):
        findings = make_aggregated_findings()
        repos = [Repository(name="other-service", github_slug=None)]
        result = await create_pr_from_findings(findings, repos, AsyncMock())
        assert result is None


# ---------------------------------------------------------------------------
# report_findings
# ---------------------------------------------------------------------------


class TestReportFindings:
    async def test_always_comments_on_jira(self):
        findings = make_aggregated_findings(action_type="comment_root_cause")
        mock_jira = AsyncMock()

        await report_findings(findings, [], mock_jira)

        mock_jira.add_comment.assert_called_once()
        call_args = mock_jira.add_comment.call_args
        assert call_args[0][0] == "BUY-1234"

    async def test_skips_slack_without_channel(self):
        findings = make_aggregated_findings(action_type="comment_root_cause")
        mock_jira = AsyncMock()
        mock_slack = AsyncMock()

        await report_findings(findings, [], mock_jira, slack_client=mock_slack, slack_channel="")

        mock_slack.post_message.assert_not_called()

    async def test_jira_failure_does_not_block_slack(self):
        findings = make_aggregated_findings(action_type="comment_root_cause")
        mock_jira = AsyncMock()
        mock_jira.add_comment.side_effect = RuntimeError("JIRA down")
        mock_slack = AsyncMock()

        await report_findings(
            findings, [], mock_jira, slack_client=mock_slack, slack_channel="#bugs"
        )

        mock_slack.post_message.assert_called_once()

    async def test_returns_pr_url(self):
        findings = make_aggregated_findings(action_type="pr")
        repos = [
            Repository(
                name="checkout-service",
                github_slug="example-org/checkout-service",
            )
        ]
        mock_jira = AsyncMock()
        mock_github = AsyncMock()
        mock_github.get_default_branch.return_value = "main"
        mock_github.get_branch_sha.return_value = "sha123"
        mock_github.get_file_content.return_value = ("old", "old-sha")
        mock_github.create_pull_request.return_value = {
            "html_url": "https://github.com/org/repo/pull/1"
        }

        pr_url = await report_findings(
            findings, repos, mock_jira, github_client=mock_github
        )

        assert pr_url == "https://github.com/org/repo/pull/1"
        # JIRA comment should include the PR URL
        comment_body = mock_jira.add_comment.call_args[0][1]
        assert _find_text_in_adf(comment_body, "https://github.com/org/repo/pull/1")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_all_text(doc: dict) -> list[str]:
    texts: list[str] = []
    for node in doc.get("content", []):
        for child in node.get("content", []):
            if child.get("type") == "text":
                texts.append(child["text"])
    return texts


def _extract_headings(doc: dict) -> list[str]:
    headings: list[str] = []
    for node in doc.get("content", []):
        if node.get("type") == "heading":
            for child in node.get("content", []):
                if child.get("type") == "text":
                    headings.append(child["text"])
    return headings


def _blocks_to_text(blocks: list[dict]) -> str:
    parts: list[str] = []
    for b in blocks:
        if "text" in b:
            t = b["text"]
            if isinstance(t, dict):
                parts.append(t.get("text", ""))
            else:
                parts.append(str(t))
        for f in b.get("fields", []):
            parts.append(f.get("text", ""))
    return " ".join(parts)


def _find_text_in_adf(doc: dict, needle: str) -> bool:
    for text in _extract_all_text(doc):
        if needle in text:
            return True
    return False
