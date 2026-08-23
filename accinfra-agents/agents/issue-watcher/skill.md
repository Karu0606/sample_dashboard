# Skill: Issue Watcher

## Purpose
Monitor the configured GitHub repository for new issues that start with the `accinfra:` prefix. Triage them by labelling and acknowledging, then hand off to the Solution Architect agent.

## Trigger
Cron schedule: every 5 minutes

## Behavior
1. Connect to GitHub using the configured App credentials
2. List all open issues
3. Filter to those whose title starts with `accinfra:` (case-insensitive)
4. Exclude any issue already labelled `infra-automation/triaged`
5. For each new matching issue:
   - Add the `infra-automation/triaged` label
   - Post an acknowledgment comment explaining next steps
6. Log results to CloudWatch (`/kirocrew/accinfra/issue-watcher`)
7. If errors occur and Slack/Teams webhook is configured, send alert

## Does NOT
- Solve or analyze the issue content
- Create branches or PRs
- Interact with AWS infrastructure

## Configuration
- `agents.issue_watcher.cron_interval_minutes`: Poll frequency (default: 5)
- `agents.issue_watcher.enabled`: Toggle on/off

## Error Handling
- GitHub API failures: logged + notified, agent exits cleanly
- Individual issue triage failure: logged, continues to next issue
- Auth failures: logged + notified with clear error message
