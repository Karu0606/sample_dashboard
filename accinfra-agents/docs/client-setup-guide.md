# AccInfra Agents — Client Setup Guide

Complete guide to deploy the AccInfra automation system on a client's AWS account with Kiro Crew.

---

## Prerequisites

| Requirement | Purpose |
|-------------|---------|
| Kiro Crew running on AWS (cloud instance) | Agent runtime |
| AWS Account with admin access (initial setup) | IAM roles, state bucket |
| GitHub repository | Issues, PRs, deployments |
| GitHub App (created per-org) | Secure API access |
| Terraform >= 1.5 (local, for initial setup) | Deploy IAM + observability |
| Python 3.10+ (on Kiro Crew instance) | Agent scripts |

---

## Setup Steps

### Step 1: Create the GitHub App

1. Go to **GitHub → Settings → Developer Settings → GitHub Apps → New GitHub App**
2. Configure:

| Setting | Value |
|---------|-------|
| App name | `AccInfra Bot` (or client-specific) |
| Homepage URL | `https://github.com/<org>` |
| Webhook | Deactivate (agents poll, no webhook needed) |

3. Permissions (Repository):

| Permission | Access |
|------------|--------|
| Issues | Read & Write |
| Pull requests | Read & Write |
| Contents | Read & Write |
| Actions | Read |
| Metadata | Read |

4. Click **Create GitHub App**
5. Note the **App ID** (numeric)
6. Generate a **Private Key** (downloads as `.pem`)
7. Install the app on the target repository
8. Note the **Installation ID** (from the URL after installing: `.../installations/<ID>`)

### Step 2: Store the GitHub App Key on Kiro Crew

SSH into your Kiro Crew instance (or use `kirocrew cloud connect`):

```bash
# Create secure directory
mkdir -p ~/.kiro/crew/
chmod 700 ~/.kiro/crew/

# Copy the private key (from your local machine)
# Option A: SCP through SSM port-forward
# Option B: Paste content directly
cat > ~/.kiro/crew/accinfra-github-app.pem << 'EOF'
-----BEGIN RSA PRIVATE KEY-----
<paste your key content here>
-----END RSA PRIVATE KEY-----
EOF

chmod 600 ~/.kiro/crew/accinfra-github-app.pem
```

### Step 3: Deploy IAM Roles (Terraform)

From your local machine with AWS admin credentials:

```bash
cd accinfra-agents/terraform/iam

# Copy and fill in variables
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars`:
```hcl
aws_region                 = "ap-southeast-1"       # Client's region
environment                = "production"
kiro_crew_instance_role_arn = "arn:aws:iam::<ACCOUNT_ID>:role/kirocrew-ec2-kirocrew"
terraform_state_bucket     = "accinfra-tfstate-<ACCOUNT_ID>"
terraform_state_key        = "infra/terraform.tfstate"
terraform_lock_table       = "accinfra-tfstate-lock"
github_repo                = "<org>/<repo>"
```

Deploy:
```bash
terraform init
terraform plan
terraform apply
```

Note the outputs:
```
agent_role_arn    = "arn:aws:iam::<ACCOUNT_ID>:role/accinfra-agent-role"
deploy_role_arn   = "arn:aws:iam::<ACCOUNT_ID>:role/accinfra-deploy-role"
```

### Step 4: Deploy Observability (Terraform)

```bash
cd accinfra-agents/terraform/observability

terraform init
terraform plan -var="aws_region=ap-southeast-1"
terraform apply -var="aws_region=ap-southeast-1"
```

Note the dashboard URL from the output.

### Step 5: Bootstrap Terraform Backend

Two options:

**Option A: GitHub Actions (recommended)**
1. Copy `github-actions/tf-backend-bootstrap.yml` to `.github/workflows/` in the target repo
2. Update the env vars (ACCOUNT_ID, region)
3. Go to Actions → "Bootstrap Terraform Backend" → Run workflow
4. Type `bootstrap` to confirm

**Option B: AWS CLI (manual)**
```bash
# Create S3 bucket
aws s3api create-bucket \
  --bucket accinfra-tfstate-<ACCOUNT_ID> \
  --region ap-southeast-1 \
  --create-bucket-configuration LocationConstraint=ap-southeast-1

aws s3api put-bucket-versioning \
  --bucket accinfra-tfstate-<ACCOUNT_ID> \
  --versioning-configuration Status=Enabled

aws s3api put-bucket-encryption \
  --bucket accinfra-tfstate-<ACCOUNT_ID> \
  --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

aws s3api put-public-access-block \
  --bucket accinfra-tfstate-<ACCOUNT_ID> \
  --public-access-block-configuration '{"BlockPublicAcls":true,"IgnorePublicAcls":true,"BlockPublicPolicy":true,"RestrictPublicBuckets":true}'

# Create DynamoDB lock table
aws dynamodb create-table \
  --table-name accinfra-tfstate-lock \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region ap-southeast-1
```

### Step 6: Configure accinfra-config.json

On the Kiro Crew instance, edit the config:

```json
{
  "github": {
    "repo_owner": "<org>",
    "repo_name": "<repo>",
    "app_id": "<from step 1>",
    "app_installation_id": "<from step 1>",
    "app_private_key_path": "~/.kiro/crew/accinfra-github-app.pem",
    "issue_prefix": "accinfra:",
    "approver_username": "<github-username>"
  },
  "aws": {
    "region": "ap-southeast-1",
    "agent_role_arn": "arn:aws:iam::<ACCOUNT_ID>:role/accinfra-agent-role",
    "terraform_state_bucket": "accinfra-tfstate-<ACCOUNT_ID>",
    "terraform_state_key": "infra/terraform.tfstate",
    "terraform_lock_table": "accinfra-tfstate-lock",
    "cloudwatch_log_group_prefix": "/kirocrew/accinfra"
  },
  "notifications": {
    "slack_webhook_url": "",
    "teams_webhook_url": ""
  }
}
```

### Step 7: Copy GitHub Actions to Target Repo

```bash
# From the accinfra-agents directory
mkdir -p <target-repo>/.github/workflows/
cp github-actions/deploy.yml <target-repo>/.github/workflows/accinfra-deploy.yml
cp github-actions/tf-backend-bootstrap.yml <target-repo>/.github/workflows/accinfra-tf-bootstrap.yml
```

Edit `accinfra-deploy.yml` env vars:
```yaml
env:
  AWS_REGION: "ap-southeast-1"
  DEPLOY_ROLE_ARN: "arn:aws:iam::<ACCOUNT_ID>:role/accinfra-deploy-role"
  TF_STATE_BUCKET: "accinfra-tfstate-<ACCOUNT_ID>"
  TF_STATE_KEY: "infra/terraform.tfstate"
  TF_LOCK_TABLE: "accinfra-tfstate-lock"
```

Commit and push.

### Step 8: Create GitHub Labels

Create these labels in the target repository:

| Label | Color | Description |
|-------|-------|-------------|
| `infra-automation/triaged` | `#0E8A16` | Issue picked up by agents |
| `infra-automation/in-progress` | `#1D76DB` | Solution being generated |
| `infra-automation/awaiting-review` | `#FBCA04` | PR open, waiting for human review |
| `infra-automation/deployed` | `#0E8A16` | Successfully deployed |
| `infra-automation/failed` | `#D93F0B` | Deployment failed |

### Step 9: Install Agent Dependencies on Kiro Crew

```bash
# On the Kiro Crew instance
cd /path/to/accinfra-agents
pip install -r requirements.txt
```

### Step 10: Register Cron Jobs in Kiro Crew

Register the 4 agent crons in your Kiro Crew dashboard:

| Name | Schedule | Command |
|------|----------|---------|
| accinfra-issue-watcher | `*/5 * * * *` | `python agents/issue-watcher/run.py` |
| accinfra-solution-architect | `*/5 * * * *` | `python agents/solution-architect/run.py` |
| accinfra-review-handler | `*/10 * * * *` | `python agents/review-handler/run.py` |
| accinfra-deploy-closer | `*/10 * * * *` | `python agents/deploy-closer/run.py` |

Or register via CLI:
```bash
kirocrew cron add --name "accinfra-issue-watcher" \
  --schedule "*/5 * * * *" \
  --command "python /path/to/accinfra-agents/agents/issue-watcher/run.py"
```

### Step 11: Verify Setup

1. **Test GitHub connection:**
```bash
python -c "
from shared.config_loader import load_config
from shared.github_client import GitHubClient
config = load_config()
gh = GitHubClient(config)
print(f'Connected to: {gh.repo_full_name}')
print(f'Default branch: {gh.get_default_branch()}')
"
```

2. **Test AWS connection:**
```bash
python -c "
from shared.config_loader import load_config
from shared.aws_client import AWSClient
config = load_config()
aws = AWSClient(config, session_name='test')
ec2 = aws.get_client('ec2')
vpcs = ec2.describe_vpcs()
print(f'VPCs found: {len(vpcs[\"Vpcs\"])}')
"
```

3. **Create a test issue:**
   - Title: `accinfra: Test VPC creation`
   - Body: `Create a new VPC for testing the automation pipeline.`

4. **Watch the agents work** (within 5 minutes the issue should be triaged).

---

## Optional: Enable Notifications

### Slack
1. Create an [Incoming Webhook](https://api.slack.com/messaging/webhooks) in your Slack workspace
2. Add the webhook URL to config: `notifications.slack_webhook_url`

### Microsoft Teams
1. Create an [Incoming Webhook connector](https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/how-to/add-incoming-webhook) in your Teams channel
2. Add the webhook URL to config: `notifications.teams_webhook_url`

### Behavior
- If both URLs are empty → silent (no notifications sent)
- If either is set → notifications on errors and escalations
- Configure which events trigger notifications: `notifications.notify_on`

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          YOUR AWS ACCOUNT                                │
│                                                                          │
│  ┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐  │
│  │ Kiro Crew EC2    │    │ CloudWatch       │    │ S3 + DynamoDB    │  │
│  │ (4 agent crons)  │───▶│ (Logs+Dashboard) │    │ (TF State+Lock)  │  │
│  └────────┬─────────┘    └──────────────────┘    └──────────────────┘  │
│           │                                                              │
│           │ STS AssumeRole                                               │
│           ▼                                                              │
│  ┌──────────────────┐                                                   │
│  │ accinfra-agent-  │  READ: EC2, IAM, Quotas, S3 state                │
│  │ role             │  WRITE: CloudWatch Logs only                       │
│  └──────────────────┘                                                   │
│                                                                          │
│  ┌──────────────────┐                                                   │
│  │ accinfra-deploy- │  WRITE: VPC, Subnets, SGs, IAM roles, ECS, S3   │
│  │ role (OIDC)      │  DENY: CreateUser, AccessKeys, Orgs, KMS delete  │
│  └──────────────────┘                                                   │
└─────────────────────────────────────────────────────────────────────────┘
          │                              ▲
          │ GitHub API                   │ GitHub OIDC
          ▼                              │
┌─────────────────────────────────────────────────────────────────────────┐
│                          GITHUB                                          │
│                                                                          │
│  Issues (accinfra:*)  →  PRs (infra/issue-*)  →  Actions (deploy.yml)  │
│                                                                          │
│  Human approver reviews PR  →  Merge  →  terraform apply  →  Done      │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Workflow (End-to-End)

```
1. User creates issue: "accinfra: Create VPC for team-alpha"
         │
         ▼  (5 min)
2. Agent 1 (Issue Watcher): Labels + acknowledges
         │
         ▼  (5 min)
3. Agent 2 (Solution Architect):
   - Searches existing closed issues for "VPC" patterns
   - Checks: VPC limit OK? Subnet quota OK? EIP available?
   - Generates Terraform code
   - Creates branch: infra/issue-42
   - Opens PR, requests review from approver
   - Comments full solution on issue
         │
         ▼  (human reviews PR)
4. Human approver:
   - Reviews Terraform code
   - Option A: Approves → merge
   - Option B: Requests changes → Agent 3 picks up
         │
         ▼  (10 min, if changes requested)
5. Agent 3 (Review Handler):
   - Reads feedback
   - Updates code (max 3 attempts)
   - Re-requests review
         │
         ▼  (after merge)
6. GitHub Actions: terraform plan → apply
         │
         ▼  (10 min)
7. Agent 4 (Deploy Closer):
   - Checks Actions status = success
   - Reads TF state for resource details
   - Comments ARNs/IDs on issue
   - Closes issue ✅
```

---

## Troubleshooting

### Agent not picking up issues
- Check cron is registered: `kirocrew cron list`
- Verify GitHub App has permissions on the repo
- Check issue title starts with `accinfra:` (case-insensitive)
- Look at CloudWatch logs: `/kirocrew/accinfra/issue-watcher`

### AWS STS assume role fails
- Verify the Kiro Crew instance role is in the trust policy
- Check external ID matches: `accinfra-agents`
- Ensure the agent role exists: `aws iam get-role --role-name accinfra-agent-role`

### GitHub Actions OIDC fails
- Verify GitHub OIDC provider exists in the account
- Check the `sub` claim matches: `repo:<org>/<repo>:ref:refs/heads/main`
- Ensure the deploy role trust policy references the correct repo

### Terraform state lock error
- Check if another apply is running
- If stale lock: `aws dynamodb delete-item --table-name accinfra-tfstate-lock --key '{"LockID":{"S":"<state-path>"}}'`

### Notifications not sending
- Check `notifications.slack_webhook_url` or `teams_webhook_url` is set in config
- Test webhook directly: `curl -X POST -H 'Content-type: application/json' --data '{"text":"test"}' <webhook_url>`

---

## Security Considerations for Client Deployments

1. **No long-lived credentials** — Agents use STS temporary credentials. GitHub Actions use OIDC. No secrets stored in GitHub.
2. **Least privilege** — Agent role is read-only. Deploy role is scoped to `accinfra-*` prefixed resources.
3. **Explicit deny** — Dangerous actions (CreateUser, CreateAccessKey, Organizations, KMS deletion) are explicitly denied.
4. **Human gate** — No infrastructure is provisioned without human PR approval.
5. **Audit trail** — Every agent action logged to CloudWatch with structured JSON.
6. **Resource allowlist** — Agents can only generate Terraform for approved resource types.
7. **Revision limit** — Auto-remediation capped at 3 attempts, then human escalation.

---

## Estimated Costs

| Component | Approximate Monthly Cost |
|-----------|-------------------------|
| CloudWatch Logs (4 agents, 30-day retention) | ~$1-5 |
| CloudWatch Dashboard | Free (3 dashboards free tier) |
| S3 Terraform state | < $1 |
| DynamoDB lock table (pay-per-request) | < $1 |
| GitHub Actions minutes | Free tier usually sufficient |
| **Total overhead** | **~$2-7/month** |

The actual infrastructure deployed (VPCs, ECS, etc.) is separate and depends on what's requested.

---

## Updating the System

To update agents:
1. Pull latest code on the Kiro Crew instance
2. `pip install -r requirements.txt`
3. Crons auto-pick up new code on next execution

To update IAM/observability:
1. Edit terraform.tfvars if needed
2. `terraform plan` → `terraform apply`

To add new notification channels:
1. Get webhook URL
2. Update `accinfra-config.json`
3. Agents pick up changes on next cron cycle (config reloaded each run)
