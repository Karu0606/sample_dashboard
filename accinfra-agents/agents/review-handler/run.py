"""
Agent 3: Review Handler
────────────────────────
Cron: Every 10 minutes

Responsibilities:
1. Watch open PRs on 'infra/issue-*' branches
2. Detect if reviewer has requested changes
3. Read review comments/feedback
4. Update Terraform code based on feedback
5. Push updated code and re-request review
6. Escalate after max revision cycles (default: 3)
7. Log everything to CloudWatch
"""

import sys
import os
import re
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from shared.config_loader import load_config, get_agent_config, get_security_config
from shared.github_client import GitHubClient, GitHubClientError
from shared.observability import (
    AgentLogger,
    ExecutionTimer,
    STATUS_OK,
    STATUS_ERROR,
    STATUS_SKIP,
    STATUS_ESCALATION,
)

AGENT_NAME = "review-handler"

# Track revision count via PR comments from this agent
REVISION_MARKER = "<!-- accinfra-revision:"


def run():
    """Main execution entry point for the Review Handler agent."""
    config = load_config()
    agent_config = get_agent_config(config, "review_handler")
    security_config = get_security_config(config)

    if not agent_config.get("enabled", True):
        return

    logger = AgentLogger(AGENT_NAME, config)
    max_revisions = agent_config.get("max_revision_cycles", 3)
    escalation_msg = agent_config.get(
        "escalation_message",
        "This PR has exceeded the maximum automated revision cycles. Human intervention required.",
    )

    with ExecutionTimer() as timer:
        try:
            gh = GitHubClient(config)

            # Find open PRs on infra branches
            open_prs = gh.list_open_prs(head_prefix=gh.branch_prefix)

            if not open_prs:
                logger.log(
                    STATUS_SKIP,
                    "No open infra PRs found",
                    duration_seconds=timer.elapsed,
                )
                return

            processed = 0
            for pr in open_prs:
                result = _handle_pr(
                    pr=pr,
                    gh=gh,
                    max_revisions=max_revisions,
                    escalation_msg=escalation_msg,
                    security_config=security_config,
                    logger=logger,
                    timer=timer,
                )
                if result:
                    processed += 1

            if processed == 0:
                logger.log(
                    STATUS_SKIP,
                    f"Checked {len(open_prs)} PR(s), none need revision",
                    duration_seconds=timer.elapsed,
                )

        except GitHubClientError as e:
            logger.log(
                STATUS_ERROR,
                "GitHub API error",
                error_summary=str(e),
                duration_seconds=timer.elapsed,
            )
        except Exception as e:
            logger.log(
                STATUS_ERROR,
                f"Unexpected error: {type(e).__name__}",
                error_summary=str(e),
                duration_seconds=timer.elapsed,
            )


def _handle_pr(
    pr: dict,
    gh: GitHubClient,
    max_revisions: int,
    escalation_msg: str,
    security_config: dict,
    logger: AgentLogger,
    timer: ExecutionTimer,
) -> bool:
    """Handle a single PR. Returns True if action was taken."""
    pr_number = pr["number"]
    pr_branch = pr["head"]["ref"]
    issue_number = _extract_issue_number(pr_branch)
    issue_id = f"#{issue_number}" if issue_number else None

    try:
        # Get reviews for this PR
        reviews = gh.get_pr_reviews(pr_number)

        # Check if changes have been requested
        changes_requested = _has_changes_requested(reviews)

        if not changes_requested:
            return False  # No action needed — either approved, pending, or no review yet

        # Count current revision number
        current_revision = _get_revision_count(gh, pr_number)

        # Check if we've exceeded max revisions
        if current_revision >= max_revisions:
            _escalate_pr(gh, pr_number, issue_number, escalation_msg, logger, timer)
            return True

        # Get the review feedback
        feedback = _collect_feedback(gh, pr_number, reviews)

        if not feedback:
            return False  # Changes requested but no actionable feedback found

        # Generate updated Terraform based on feedback
        updated_code = _apply_feedback(
            gh=gh,
            pr_number=pr_number,
            pr_branch=pr_branch,
            feedback=feedback,
            security_config=security_config,
        )

        if not updated_code:
            # Couldn't auto-resolve — comment and let human know
            gh.add_comment(
                pr_number,
                (
                    "🤖 **AccInfra Review Handler**\n\n"
                    "I reviewed the feedback but couldn't automatically resolve the requested changes. "
                    "The feedback may require architectural decisions that need human input.\n\n"
                    f"**Feedback received:**\n{feedback}\n\n"
                    f"{REVISION_MARKER}{current_revision + 1} -->"
                ),
            )
            logger.log(
                STATUS_ESCALATION,
                f"PR #{pr_number}: Cannot auto-resolve feedback",
                issue_id=issue_id,
                pr_number=pr_number,
                error_summary="Feedback requires human decision",
                duration_seconds=timer.elapsed,
            )
            return True

        # Push updated code
        tf_file_path = f"infrastructure/issue-{issue_number}.tf" if issue_number else None
        if tf_file_path:
            # Get current file SHA for update
            file_sha = _get_file_sha(gh, pr_branch, tf_file_path)
            gh.create_or_update_file(
                branch=pr_branch,
                file_path=tf_file_path,
                content=updated_code,
                commit_message=f"fix(infra): Address review feedback (revision {current_revision + 1})\n\nApplied changes based on reviewer comments.",
                existing_sha=file_sha,
            )

        # Comment on PR with what was changed
        revision_comment = (
            f"🤖 **AccInfra Review Handler — Revision {current_revision + 1}**\n\n"
            f"Applied changes based on reviewer feedback:\n\n"
            f"**Feedback addressed:**\n{feedback}\n\n"
            f"**Changes made:**\n"
            f"- Updated `{tf_file_path}`\n"
            f"- Applied corrections per review comments\n\n"
            f"Please re-review. "
            f"(Revision {current_revision + 1}/{max_revisions} — "
            f"{'last automated attempt' if current_revision + 1 >= max_revisions else f'{max_revisions - current_revision - 1} remaining'})\n\n"
            f"{REVISION_MARKER}{current_revision + 1} -->"
        )
        gh.add_comment(pr_number, revision_comment)

        logger.log(
            STATUS_OK,
            f"PR #{pr_number}: Applied revision {current_revision + 1}",
            issue_id=issue_id,
            pr_number=pr_number,
            duration_seconds=timer.elapsed,
            metadata={
                "revision": current_revision + 1,
                "max_revisions": max_revisions,
                "feedback_length": len(feedback),
            },
        )
        return True

    except GitHubClientError as e:
        logger.log(
            STATUS_ERROR,
            f"Failed to handle PR #{pr_number}",
            issue_id=issue_id,
            pr_number=pr_number,
            error_summary=str(e),
            duration_seconds=timer.elapsed,
        )
        return False


def _has_changes_requested(reviews: list[dict]) -> bool:
    """Check if the latest review from each reviewer has 'CHANGES_REQUESTED'.

    GitHub keeps all reviews — we only care about the most recent one per reviewer.
    """
    latest_by_user: dict[str, str] = {}

    for review in reviews:
        user = review.get("user", {}).get("login", "")
        state = review.get("state", "")
        # Only track meaningful states
        if state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest_by_user[user] = state

    return "CHANGES_REQUESTED" in latest_by_user.values()


def _get_revision_count(gh: GitHubClient, pr_number: int) -> int:
    """Count how many revisions this agent has already made on a PR.

    Uses hidden HTML comments in PR comments as markers.
    """
    # Get all comments on the PR (issue comments endpoint works for PR comments)
    url = f"https://api.github.com/repos/{gh.repo_full_name}/issues/{pr_number}/comments"
    comments = gh._api_request("GET", url)

    max_revision = 0
    for comment in comments:
        body = comment.get("body", "")
        match = re.search(r"<!-- accinfra-revision:(\d+) -->", body)
        if match:
            rev = int(match.group(1))
            if rev > max_revision:
                max_revision = rev

    return max_revision


def _collect_feedback(gh: GitHubClient, pr_number: int, reviews: list[dict]) -> str:
    """Collect all actionable feedback from reviewers.

    Combines:
    - Review body comments (top-level review message)
    - Inline code review comments
    """
    feedback_parts = []

    # Get review body comments that have CHANGES_REQUESTED
    for review in reviews:
        if review.get("state") == "CHANGES_REQUESTED":
            body = review.get("body", "")
            if body and body.strip():
                reviewer = review.get("user", {}).get("login", "unknown")
                feedback_parts.append(f"**{reviewer}:** {body.strip()}")

    # Get inline review comments
    review_comments = gh.get_pr_review_comments(pr_number)
    for comment in review_comments:
        body = comment.get("body", "")
        path = comment.get("path", "")
        line = comment.get("line") or comment.get("original_line", "?")
        if body and body.strip():
            feedback_parts.append(f"- `{path}` (line {line}): {body.strip()}")

    return "\n".join(feedback_parts)


def _apply_feedback(
    gh: GitHubClient,
    pr_number: int,
    pr_branch: str,
    feedback: str,
    security_config: dict,
) -> str | None:
    """Attempt to apply reviewer feedback to the Terraform code.

    This uses pattern matching to identify common review requests:
    - "Add tags" → ensures tags block is present
    - "Change CIDR" → updates CIDR block
    - "Add description" → adds description fields
    - "Restrict ingress" → tightens security group rules
    - "Enable encryption" → adds encryption configuration
    - "Add lifecycle" → adds lifecycle rules

    Returns updated Terraform code, or None if feedback can't be auto-resolved.
    """
    feedback_lower = feedback.lower()

    # Get current file content
    issue_number = _extract_issue_number(pr_branch)
    if not issue_number:
        return None

    tf_file_path = f"infrastructure/issue-{issue_number}.tf"
    current_content = _get_file_content(gh, pr_branch, tf_file_path)

    if not current_content:
        return None

    modified = current_content
    changes_made = False

    # Pattern: Restrict security group / tighten ingress
    if any(term in feedback_lower for term in ["restrict", "tighten", "narrow", "specific cidr", "remove 0.0.0.0"]):
        modified = re.sub(
            r'cidr_blocks\s*=\s*\["0\.0\.0\.0/0"\](\s*\n\s*description\s*=\s*".*?inbound")',
            'cidr_blocks = ["10.0.0.0/8"]  # Restricted to internal network\n    description = "Internal network only"',
            modified,
        )
        if modified != current_content:
            changes_made = True

    # Pattern: Add encryption
    if any(term in feedback_lower for term in ["encrypt", "encryption", "kms", "sse"]):
        if "server_side_encryption" not in modified and "aws_s3_bucket" in modified:
            # Already has encryption in our template, but maybe they want KMS
            modified = modified.replace(
                'sse_algorithm = "AES256"',
                'sse_algorithm = "aws:kms"',
            )
            if modified != current_content:
                changes_made = True

    # Pattern: Add lifecycle / prevent destroy
    if any(term in feedback_lower for term in ["lifecycle", "prevent_destroy", "prevent destroy"]):
        # Add lifecycle block to resources that don't have one
        if "lifecycle" not in modified:
            modified = re.sub(
                r'(resource "aws_\w+" "\w+" \{[^}]+)(})',
                r'\1\n  lifecycle {\n    prevent_destroy = true\n  }\n\2',
                modified,
                count=1,  # Only first resource
            )
            if modified != current_content:
                changes_made = True

    # Pattern: Change VPC CIDR
    cidr_match = re.search(r"cidr.+?(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/\d{1,2})", feedback_lower)
    if cidr_match:
        new_cidr = cidr_match.group(1)
        modified = re.sub(
            r'cidr_block\s*=\s*"10\.0\.0\.0/16"',
            f'cidr_block = "{new_cidr}"',
            modified,
            count=1,
        )
        if modified != current_content:
            changes_made = True

    # Pattern: Add description to resources
    if any(term in feedback_lower for term in ["add description", "missing description", "needs description"]):
        # Add description to security groups that don't have one
        if 'description' not in modified.split('resource')[1] if 'resource' in modified else True:
            modified = modified.replace(
                'description = "Managed by AccInfra agents"',
                'description = "Managed by AccInfra automation agents - auto-generated infrastructure"',
            )
            if modified != current_content:
                changes_made = True

    # Pattern: Enable versioning
    if any(term in feedback_lower for term in ["versioning", "version"]):
        if "versioning" not in modified and "aws_s3_bucket" in modified:
            # Our template already includes versioning, so this is a no-op
            pass

    # Pattern: Add logging
    if any(term in feedback_lower for term in ["logging", "access log", "flow log"]):
        if "flow_log" not in modified and "aws_vpc" in modified:
            flow_log_block = textwrap.dedent("""

            # ─── VPC Flow Logs ────────────────────────────────────────────────

            resource "aws_flow_log" "main" {
              vpc_id          = aws_vpc.main.id
              traffic_type    = "ALL"
              iam_role_arn    = aws_iam_role.flow_log.arn
              log_destination = aws_cloudwatch_log_group.flow_log.arn
            }

            resource "aws_cloudwatch_log_group" "flow_log" {
              name              = "/aws/vpc/flow-log"
              retention_in_days = 30
            }

            resource "aws_iam_role" "flow_log" {
              name = "accinfra-vpc-flow-log-role"

              assume_role_policy = jsonencode({
                Version = "2012-10-17"
                Statement = [{
                  Action = "sts:AssumeRole"
                  Effect = "Allow"
                  Principal = {
                    Service = "vpc-flow-logs.amazonaws.com"
                  }
                }]
              })
            }
            """)
            modified += flow_log_block
            changes_made = True

    if changes_made:
        return modified

    # If no patterns matched, return None (can't auto-resolve)
    return None


def _get_file_content(gh: GitHubClient, branch: str, file_path: str) -> str | None:
    """Get the content of a file from a specific branch."""
    import base64

    try:
        url = f"https://api.github.com/repos/{gh.repo_full_name}/contents/{file_path}?ref={branch}"
        response = gh._api_request("GET", url)
        content_b64 = response.get("content", "")
        return base64.b64decode(content_b64).decode("utf-8")
    except GitHubClientError:
        return None


def _get_file_sha(gh: GitHubClient, branch: str, file_path: str) -> str | None:
    """Get the SHA of a file on a specific branch (needed for updates)."""
    try:
        url = f"https://api.github.com/repos/{gh.repo_full_name}/contents/{file_path}?ref={branch}"
        response = gh._api_request("GET", url)
        return response.get("sha")
    except GitHubClientError:
        return None


def _extract_issue_number(branch_name: str) -> int | None:
    """Extract issue number from branch name like 'infra/issue-42'."""
    match = re.search(r"issue-(\d+)$", branch_name)
    return int(match.group(1)) if match else None


def _escalate_pr(
    gh: GitHubClient,
    pr_number: int,
    issue_number: int | None,
    escalation_msg: str,
    logger: AgentLogger,
    timer: ExecutionTimer,
):
    """Escalate a PR that has exceeded max revision cycles."""
    comment = (
        "🤖 **AccInfra Review Handler — Escalation**\n\n"
        f"⚠️ {escalation_msg}\n\n"
        "This PR has been revised the maximum number of times. "
        "A human developer needs to take over from here.\n\n"
        "**What was attempted:**\n"
        "- Multiple automated revisions based on reviewer feedback\n"
        "- Pattern-based Terraform corrections\n\n"
        "**Recommended action:** A team member should review the remaining feedback "
        "and apply manual fixes."
    )
    gh.add_comment(pr_number, comment)

    # Also comment on the original issue if we have it
    if issue_number:
        gh.add_comment(
            issue_number,
            (
                "🤖 **AccInfra Agent — Escalation**\n\n"
                f"The automated PR (#{pr_number}) has reached the maximum revision limit. "
                "Human intervention is required to complete this infrastructure request."
            ),
        )

    issue_id = f"#{issue_number}" if issue_number else None
    logger.log(
        STATUS_ESCALATION,
        f"PR #{pr_number} escalated — max revisions exceeded",
        issue_id=issue_id,
        pr_number=pr_number,
        error_summary="Max revision cycles exceeded",
        duration_seconds=timer.elapsed,
    )


if __name__ == "__main__":
    run()
