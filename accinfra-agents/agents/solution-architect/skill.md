# Skill: Solution Architect

## Purpose
Process triaged infrastructure issues by searching for existing solutions, running feasibility checks, generating Terraform code, and opening a PR for review.

## Trigger
Runs on the same cron as Issue Watcher (every 5 minutes), but processes issues labelled `infra-automation/triaged` that are NOT yet `infra-automation/in-progress`.

## Behavior

### Step 1: Knowledge Reuse
- Extract keywords from the issue title/body
- Search all closed issues in the same repo for similar past solutions
- Reference relevant solutions in the generated code (as comments) and PR body
- **Goal:** Never reinvent the wheel — reuse existing patterns

### Step 2: Infrastructure Feasibility Check
Before proposing any solution, verify the target AWS account can support it:
- VPC count vs quota
- IAM roles/users count vs quota
- Subnet availability
- Elastic IP allocation
- Security group count
- NAT Gateway count

If any check is **blocking** (limit reached), the agent:
- Posts the feasibility report on the issue
- Does NOT generate code or open a PR
- Labels the issue for escalation
- Logs as ESCALATION status

### Step 3: Generate Terraform
- Detect requested resources from issue text
- Filter against security allowlist/blocklist
- Generate properly tagged Terraform code
- Apply default tags from config (ManagedBy, Environment, Project)
- Include references to similar past solutions

### Step 4: Branch + PR
- Create branch `infra/issue-<number>`
- Push Terraform file as `infrastructure/issue-<number>.tf`
- Open PR targeting the default branch
- Request review from configured approver

### Step 5: Comment on Issue
- Post the full solution (feasibility report + similar issues + Terraform code + PR link)
- Label as `infra-automation/awaiting-review`

## Security
- Only generates resources on the `allowed_terraform_resources` list
- Rejects anything on the `blocked_terraform_resources` list
- Never generates IAM users or access keys
- All resources tagged with audit metadata

## Error Handling
- Per-issue error isolation (one failure doesn't stop others)
- Feasibility failures = ESCALATION (not ERROR)
- AWS/GitHub API failures = ERROR with notification
- Max retry: none (will be re-attempted on next cron cycle if issue state allows)
