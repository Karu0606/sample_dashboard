"""
Agent 1: Issue Watcher
──────────────────────
Cron: Every 5 minutes

Responsibilities:
1. Poll GitHub for new issues with 'accinfra:' prefix
2. Skip issues already labelled as triaged
3. Label new matching issues as 'infra-automation/triaged'
4. Log execution to CloudWatch
5. Notify on errors (if Slack/Teams configured)

This agent does NOT solve issues — it only detects and triages.
Agent 2 (Solution Architect) handles the actual resolution.
"""

import sys
import os

# Add project root to path for shared module imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from shared.config_loader import load_config, get_agent_config
from shared.github_client import GitHubClient, GitHubClientError
from shared.observability import (
    AgentLogger,
    ExecutionTimer,
    STATUS_OK,
    STATUS_ERROR,
    STATUS_SKIP,
)

AGENT_NAME = "issue-watcher"


def run():
    """Main execution entry point for the Issue Watcher agent."""
    config = load_config()
    agent_config = get_agent_config(config, "issue_watcher")

    # Check if agent is enabled
    if not agent_config.get("enabled", True):
        return

    logger = AgentLogger(AGENT_NAME, config)

    with ExecutionTimer() as timer:
        try:
            # Initialize GitHub client
            gh = GitHubClient(config)

            # Find new accinfra issues that haven't been triaged
            new_issues = gh.get_new_accinfra_issues()

            if not new_issues:
                logger.log(
                    STATUS_SKIP,
                    "No new accinfra issues found",
                    duration_seconds=timer.elapsed,
                )
                return

            # Process each new issue
            triaged_count = 0
            for issue in new_issues:
                issue_number = issue["number"]
                issue_title = issue["title"]

                try:
                    # Add triaged label
                    triaged_label = gh.labels.get("triaged", "infra-automation/triaged")
                    gh.add_label(issue_number, triaged_label)

                    # Add acknowledgment comment
                    comment_body = (
                        "🤖 **AccInfra Agent — Issue Received**\n\n"
                        f"This issue has been triaged by the infrastructure automation system.\n\n"
                        "**Next steps:**\n"
                        "1. Searching existing issues for similar solutions\n"
                        "2. Running infrastructure feasibility checks\n"
                        "3. Proposing a solution with Terraform code\n\n"
                        "A solution will be posted shortly. Please wait for the automated response."
                    )
                    gh.add_comment(issue_number, comment_body)

                    triaged_count += 1
                    print(f"[OK] Triaged issue #{issue_number}: {issue_title}")

                except GitHubClientError as e:
                    logger.log(
                        STATUS_ERROR,
                        f"Failed to triage issue #{issue_number}",
                        issue_id=f"#{issue_number}",
                        error_summary=str(e),
                        duration_seconds=timer.elapsed,
                    )

            # Log success summary
            logger.log(
                STATUS_OK,
                f"Triaged {triaged_count} new issue(s)",
                duration_seconds=timer.elapsed,
                metadata={"issues_found": len(new_issues), "issues_triaged": triaged_count},
            )

        except GitHubClientError as e:
            logger.log(
                STATUS_ERROR,
                "GitHub API error during issue scan",
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


if __name__ == "__main__":
    run()
