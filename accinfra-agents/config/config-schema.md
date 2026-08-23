# AccInfra Config Schema Reference

## How to Use

Copy `accinfra-config.json` and fill in client-specific values. The system reads this single file at runtime — no code changes needed per client.

## Sections

### `github`
| Field | Required | Description |
|-------|----------|-------------|
| `repo_owner` | Yes | GitHub org or user that owns the repo |
| `repo_name` | Yes | Repository name where issues are raised |
| `app_id` | Yes | GitHub App ID (numeric) |
| `app_installation_id` | Yes | Installation ID for the target repo |
| `app_private_key_path` | Yes | Path to GitHub App private key PEM file |
| `issue_prefix` | Yes | Prefix that triggers the agents (e.g., `accinfra:`) |
| `labels` | Yes | Labels applied at each stage of the workflow |
| `approver_username` | Yes | GitHub user who approves PRs (expandable to team later) |
| `branch_prefix` | Yes | Branch naming convention for infra PRs |

### `aws`
| Field | Required | Description |
|-------|----------|-------------|
| `region` | Yes | AWS region for all operations |
| `agent_role_arn` | Yes | IAM Role ARN the agents assume (STS) |
| `terraform_state_bucket` | Yes | S3 bucket for Terraform remote state |
| `terraform_state_key` | Yes | Key path within the bucket |
| `terraform_lock_table` | Yes | DynamoDB table for state locking |
| `cloudwatch_log_group_prefix` | Yes | CloudWatch log group naming prefix |

### `notifications`
| Field | Required | Description |
|-------|----------|-------------|
| `slack_webhook_url` | No | Slack incoming webhook URL. Empty = disabled. |
| `teams_webhook_url` | No | MS Teams incoming webhook URL. Empty = disabled. |
| `notify_on` | No | Event types that trigger notifications: `error`, `escalation`, `success` |
| `channel_name` | No | Display name (informational only) |

**Behavior:** If both `slack_webhook_url` and `teams_webhook_url` are empty, no notifications are sent. The system is silent. Set either one (or both) to enable.

### `agents`
Per-agent settings: enable/disable, intervals, thresholds.

### `terraform`
Working directory, backend config, provider versions, default tags applied to all resources.

### `security`
- `require_approval_before_apply`: When true, terraform apply only runs after PR merge (human gate).
- `allowed_terraform_resources`: Whitelist of resource types agents can create. Anything not listed is rejected.
- `blocked_terraform_resources`: Explicit deny list — agents will never generate these even if asked.
