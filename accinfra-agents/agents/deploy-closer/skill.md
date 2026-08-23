# Skill: Deploy Closer

## Purpose
After a PR is merged and GitHub Actions deploys the infrastructure, close the loop by updating the original issue with deployment details and closing it.

## Trigger
Cron schedule: every 10 minutes

## Behavior

### Detection
1. List recently closed (merged) PRs on `infra/issue-*` branches
2. Skip PRs where the linked issue is already closed by this agent
3. Check GitHub Actions workflow status for each merged PR

### On Deployment Success
1. Read Terraform state from S3 to identify deployed resources
2. Build a summary with:
   - PR reference and link
   - Branch name
   - List of deployed resources (type, name, ID/ARN)
3. Comment the summary on the original issue
4. Add `infra-automation/deployed` label
5. Close the issue

### On Deployment Failure
1. Comment on the issue with:
   - Failure notification
   - Likely causes (permissions, conflicts, quotas)
   - Recommended manual actions
2. Add `infra-automation/failed` label
3. Issue remains OPEN for human follow-up

### On Deployment Pending
- Skip and check again on next cron cycle
- No comment or label change (avoid noise)

### Idempotency
- Uses hidden HTML marker (`<!-- accinfra-closed -->`) to prevent double-processing
- Also checks issue state — if already closed, skips
- Safe to run multiple times on the same PR

## Resource Discovery
The agent reads the Terraform state file from the configured S3 backend to extract:
- Resource types (aws_vpc, aws_subnet, etc.)
- Resource names
- Resource IDs and ARNs
- Filters to only `ManagedBy: accinfra-agents` tagged resources

If state can't be read (permissions, not configured), it gracefully falls back to a generic "check console" message.

## Configuration
- `agents.deploy_closer.cron_interval_minutes`: Check frequency (default: 10)
- `agents.deploy_closer.enabled`: Toggle on/off
- `aws.terraform_state_bucket`: S3 bucket for state (needed for resource discovery)
- `aws.terraform_state_key`: Key path in bucket

## Error Handling
- Per-PR error isolation
- State file read failures: non-fatal (comments without resource details)
- GitHub API failures: logged with PR context
- Already-closed issues: silently skipped
