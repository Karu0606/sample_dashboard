# Skill: Review Handler

## Purpose
Watch open infrastructure PRs for reviewer feedback. When changes are requested, automatically apply corrections to the Terraform code and re-submit for review. Escalate when the automated revision limit is reached.

## Trigger
Cron schedule: every 10 minutes

## Behavior

### Detection
1. List all open PRs on `infra/issue-*` branches
2. Check each PR's reviews for `CHANGES_REQUESTED` state
3. Skip PRs that are approved, pending, or have no review yet

### Feedback Collection
- Top-level review body comments
- Inline code review comments (with file path and line number)
- Only considers the latest review state per reviewer

### Auto-Resolution Patterns
The agent can automatically address these common review requests:

| Pattern | Action |
|---------|--------|
| "Restrict ingress" / "remove 0.0.0.0/0" | Tightens CIDR to internal network |
| "Add encryption" / "KMS" | Upgrades to aws:kms encryption |
| "Add lifecycle" / "prevent destroy" | Adds lifecycle block |
| "Change CIDR to X.X.X.X/X" | Updates VPC/subnet CIDR |
| "Add description" | Enhances resource descriptions |
| "Add logging" / "flow logs" | Adds VPC flow log resources |

### Revision Tracking
- Uses hidden HTML comments (`<!-- accinfra-revision:N -->`) to track revision count
- Each automated revision increments the counter
- Default max: 3 revisions (configurable)

### Escalation
When max revisions exceeded:
- Comments on PR explaining the limit
- Comments on the linked issue
- Logs as ESCALATION status
- Sends notification (if Slack/Teams configured)

### If Feedback Can't Be Auto-Resolved
- Comments on PR acknowledging the feedback
- States it requires human architectural decisions
- Counts as a revision cycle

## Configuration
- `agents.review_handler.cron_interval_minutes`: Check frequency (default: 10)
- `agents.review_handler.max_revision_cycles`: Max auto-revisions (default: 3)
- `agents.review_handler.escalation_message`: Custom escalation text
- `agents.review_handler.enabled`: Toggle on/off

## Security
- Only modifies files already created by Solution Architect agent
- Respects the same security allowlist/blocklist
- Never adds resources to the blocked list
- All changes committed with clear audit messages

## Error Handling
- Per-PR error isolation
- GitHub API failures logged with PR context
- File access failures (file deleted/moved) handled gracefully
- Revision count persists across agent restarts (stored in PR comments)
