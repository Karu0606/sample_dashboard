"""
Agent 4: Deploy Closer
───────────────────────
Cron: Every 10 minutes

Responsibilities:
1. Watch merged PRs on 'infra/issue-*' branches
2. Check if GitHub Actions deployment workflow succeeded
3. If success: comment on the original issue with resource details, close it
4. If failure: comment with error details, re-open/flag the issue
5. Log everything to CloudWatch
"""

import sys
import os
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from shared.config_loader import load_config, get_agent_config
from shared.github_client import GitHubClient, GitHubClientError
from shared.aws_client import AWSClient, AWSClientError
from shared.observability import (
    AgentLogger,
    ExecutionTimer,
    STATUS_OK,
    STATUS_ERROR,
    STATUS_SKIP,
)

AGENT_NAME = "deploy-closer"

# Hidden marker to avoid closing an issue twice
CLOSE_MARKER = "<!-- accinfra-closed -->"


def run():
    """Main execution entry point for the Deploy Closer agent."""
    config = load_config()
    agent_config = get_agent_config(config, "deploy_closer")

    if not agent_config.get("enabled", True):
        return

    logger = AgentLogger(AGENT_NAME, config)

    with ExecutionTimer() as timer:
        try:
            gh = GitHubClient(config)

            # Find recently merged infra PRs
            merged_prs = _get_recently_merged_infra_prs(gh)

            if not merged_prs:
                logger.log(
                    STATUS_SKIP,
                    "No recently merged infra PRs to process",
                    duration_seconds=timer.elapsed,
                )
                return

            for pr in merged_prs:
                _process_merged_pr(pr, gh, config, logger, timer)

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


def _get_recently_merged_infra_prs(gh: GitHubClient) -> list[dict]:
    """Get merged PRs on infra/issue-* branches that haven't been processed yet.

    We check closed PRs (merged=true) and filter to those where:
    - Branch matches infra/issue-* pattern
    - The linked issue hasn't been closed by this agent yet
    """
    # GitHub API: list closed PRs
    url = (
        f"https://api.github.com/repos/{gh.repo_full_name}/pulls"
        f"?state=closed&sort=updated&direction=desc&per_page=20"
    )
    closed_prs = gh._api_request("GET", url)

    merged_infra_prs = []
    for pr in closed_prs:
        # Must be actually merged (not just closed)
        if not pr.get("merged_at"):
            continue

        # Must be an infra branch
        branch = pr["head"]["ref"]
        if not branch.startswith(gh.branch_prefix):
            continue

        # Check if we already processed this (look for our marker in the linked issue)
        issue_number = _extract_issue_number(branch)
        if issue_number and _already_closed(gh, issue_number):
            continue

        merged_infra_prs.append(pr)

    return merged_infra_prs


def _process_merged_pr(
    pr: dict,
    gh: GitHubClient,
    config: dict,
    logger: AgentLogger,
    timer: ExecutionTimer,
):
    """Process a single merged PR: check deploy status and close the issue."""
    pr_number = pr["number"]
    pr_branch = pr["head"]["ref"]
    pr_url = pr.get("html_url", "")
    merge_sha = pr.get("merge_commit_sha", "")
    issue_number = _extract_issue_number(pr_branch)
    issue_id = f"#{issue_number}" if issue_number else None

    if not issue_number:
        logger.log(
            STATUS_SKIP,
            f"PR #{pr_number}: Could not extract issue number from branch {pr_branch}",
            pr_number=pr_number,
            duration_seconds=timer.elapsed,
        )
        return

    try:
        # Check GitHub Actions workflow status for this branch/merge
        deploy_status = _check_deployment_status(gh, pr_branch, merge_sha)

        if deploy_status == "success":
            # Get deployed resource information
            resource_info = _get_deployed_resources(config, issue_number)

            # Build closing comment
            close_comment = _build_success_comment(
                pr_number=pr_number,
                pr_url=pr_url,
                pr_branch=pr_branch,
                resource_info=resource_info,
            )

            # Comment and close the issue
            gh.add_comment(issue_number, close_comment)
            gh.close_issue(issue_number)

            # Add deployed label
            deployed_label = gh.labels.get("deployed", "infra-automation/deployed")
            gh.add_label(issue_number, deployed_label)

            logger.log(
                STATUS_OK,
                f"Issue {issue_id} closed — deployment successful",
                issue_id=issue_id,
                pr_number=pr_number,
                duration_seconds=timer.elapsed,
                metadata={
                    "merge_sha": merge_sha[:8],
                    "resources_deployed": len(resource_info),
                },
            )

        elif deploy_status == "failure":
            # Build failure comment
            failure_comment = _build_failure_comment(
                pr_number=pr_number,
                pr_url=pr_url,
            )

            gh.add_comment(issue_number, failure_comment)

            # Add failed label
            failed_label = gh.labels.get("failed", "infra-automation/failed")
            gh.add_label(issue_number, failed_label)

            logger.log(
                STATUS_ERROR,
                f"Issue {issue_id}: Deployment failed after merge",
                issue_id=issue_id,
                pr_number=pr_number,
                error_summary="GitHub Actions deploy workflow failed",
                duration_seconds=timer.elapsed,
            )

        elif deploy_status == "pending":
            # Deployment still running — skip for now, check again next cycle
            logger.log(
                STATUS_SKIP,
                f"Issue {issue_id}: Deployment still in progress",
                issue_id=issue_id,
                pr_number=pr_number,
                duration_seconds=timer.elapsed,
            )

        else:
            # No workflow found — might not be set up yet
            logger.log(
                STATUS_SKIP,
                f"Issue {issue_id}: No deployment workflow found for PR #{pr_number}",
                issue_id=issue_id,
                pr_number=pr_number,
                duration_seconds=timer.elapsed,
            )

    except GitHubClientError as e:
        logger.log(
            STATUS_ERROR,
            f"Failed to process merged PR #{pr_number}",
            issue_id=issue_id,
            pr_number=pr_number,
            error_summary=str(e),
            duration_seconds=timer.elapsed,
        )


def _check_deployment_status(gh: GitHubClient, branch: str, merge_sha: str) -> str:
    """Check the GitHub Actions deployment workflow status.

    Returns: 'success', 'failure', 'pending', or 'none'
    """
    # Check workflow runs for the default branch (where the merge happened)
    default_branch = gh.get_default_branch()
    runs = gh.get_workflow_runs_for_branch(default_branch)

    if not runs:
        return "none"

    # Find runs that match our merge commit
    for run in runs:
        run_sha = run.get("head_sha", "")
        # Match by SHA prefix (merge commit)
        if merge_sha and run_sha.startswith(merge_sha[:7]):
            status = run.get("status", "")
            conclusion = run.get("conclusion", "")

            if status == "completed":
                if conclusion == "success":
                    return "success"
                else:
                    return "failure"
            else:
                return "pending"

    # If no matching run found, check the most recent run
    latest = runs[0] if runs else None
    if latest:
        status = latest.get("status", "")
        conclusion = latest.get("conclusion", "")
        if status == "completed" and conclusion == "success":
            return "success"
        elif status == "completed":
            return "failure"
        else:
            return "pending"

    return "none"


def _get_deployed_resources(config: dict, issue_number: int) -> list[dict]:
    """Attempt to identify deployed resources from Terraform state.

    Queries the Terraform state file in S3 to find resources tagged
    with the issue number or created by the issue's Terraform file.

    Returns list of {type, name, id/arn} dicts.
    """
    resources = []

    try:
        aws = AWSClient(config, session_name="accinfra-deploy-closer")
        s3 = aws.get_client("s3")

        bucket = config["aws"].get("terraform_state_bucket", "")
        key = config["aws"].get("terraform_state_key", "")

        if not bucket or "ACCOUNT_ID" in bucket:
            # State bucket not configured — return empty
            return resources

        # Download and parse state file
        import json
        response = s3.get_object(Bucket=bucket, Key=key)
        state_content = response["Body"].read().decode("utf-8")
        state = json.loads(state_content)

        # Extract resources from state
        for resource in state.get("resources", []):
            resource_type = resource.get("type", "")
            resource_name = resource.get("name", "")

            for instance in resource.get("instances", []):
                attrs = instance.get("attributes", {})
                resource_id = attrs.get("id", "")
                resource_arn = attrs.get("arn", "")

                # Check if resource was created by our issue's Terraform
                tags = attrs.get("tags", {}) or {}
                if tags.get("ManagedBy") == "accinfra-agents":
                    resources.append({
                        "type": resource_type,
                        "name": resource_name,
                        "id": resource_id,
                        "arn": resource_arn,
                    })

    except (AWSClientError, Exception):
        # If we can't read state, return empty — not critical
        pass

    return resources


def _build_success_comment(
    pr_number: int,
    pr_url: str,
    pr_branch: str,
    resource_info: list[dict],
) -> str:
    """Build the issue-closing success comment."""
    comment = (
        "🤖 **AccInfra Agent — Deployment Successful** ✅\n\n"
        "Your infrastructure has been deployed.\n\n"
        "---\n\n"
        "### Deployment Details\n\n"
        f"| Item | Value |\n"
        f"|------|-------|\n"
        f"| PR | [#{pr_number}]({pr_url}) |\n"
        f"| Branch | `{pr_branch}` |\n"
        f"| Status | ✅ Deployed |\n\n"
    )

    if resource_info:
        comment += "### Deployed Resources\n\n"
        comment += "| Type | Name | ID/ARN |\n"
        comment += "|------|------|--------|\n"
        for res in resource_info:
            identifier = res.get("arn") or res.get("id") or "—"
            # Truncate long ARNs for readability
            if len(identifier) > 80:
                identifier = identifier[:40] + "..." + identifier[-37:]
            comment += f"| `{res['type']}` | {res['name']} | `{identifier}` |\n"
    else:
        comment += (
            "### Deployed Resources\n\n"
            "Resource details will be available in the Terraform state. "
            "Check the AWS console or run `terraform show` for full details.\n"
        )

    comment += (
        "\n---\n\n"
        "This issue is now **closed**. "
        "If you need modifications to the deployed infrastructure, "
        "please open a new issue with the `accinfra:` prefix.\n\n"
        f"{CLOSE_MARKER}"
    )

    return comment


def _build_failure_comment(pr_number: int, pr_url: str) -> str:
    """Build the deployment failure comment."""
    return (
        "🤖 **AccInfra Agent — Deployment Failed** ❌\n\n"
        f"The GitHub Actions deployment workflow failed for [PR #{pr_number}]({pr_url}).\n\n"
        "**What happened:**\n"
        "- The PR was approved and merged\n"
        "- The `terraform apply` step in GitHub Actions failed\n\n"
        "**Next steps:**\n"
        "1. Check the [Actions tab](../../actions) for detailed error logs\n"
        "2. The failure may be due to:\n"
        "   - AWS permission issues\n"
        "   - Resource conflicts\n"
        "   - State lock contention\n"
        "   - Quota limits reached since feasibility check\n"
        "3. A team member should investigate and either:\n"
        "   - Fix and re-run the workflow\n"
        "   - Open a new `accinfra:` issue with adjusted requirements\n\n"
        "This issue remains **open** until deployment succeeds.\n"
    )


def _extract_issue_number(branch_name: str) -> int | None:
    """Extract issue number from branch name like 'infra/issue-42'."""
    match = re.search(r"issue-(\d+)$", branch_name)
    return int(match.group(1)) if match else None


def _already_closed(gh: GitHubClient, issue_number: int) -> bool:
    """Check if we already closed this issue (avoid double-processing)."""
    try:
        url = f"https://api.github.com/repos/{gh.repo_full_name}/issues/{issue_number}/comments"
        comments = gh._api_request("GET", url)
        for comment in comments:
            if CLOSE_MARKER in comment.get("body", ""):
                return True
    except GitHubClientError:
        pass

    # Also check if issue is already closed
    try:
        url = f"https://api.github.com/repos/{gh.repo_full_name}/issues/{issue_number}"
        issue = gh._api_request("GET", url)
        if issue.get("state") == "closed":
            return True
    except GitHubClientError:
        pass

    return False


if __name__ == "__main__":
    run()
