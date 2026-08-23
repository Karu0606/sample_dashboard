# AccInfra — Live Demo Runbook

**Demo goal:** Show the full loop live — GitHub issue → infra feasibility check → Terraform PR → human rejects → agent auto-corrects → human approves → GitHub Actions deploys → issue auto-closed. All in the cloud, observable in CloudWatch.

**Architecture:** Single account (Account A). Kiro Crew on EC2. You drive via SSM. Agents use the EC2 instance role directly (no assume-role hop).

---

## Part 0 — Pre-Demo Setup (do the evening before)

### 0.1 Confirm instance role has needed permissions
The EC2 instance role must be able to: create IAM roles, create an OIDC provider, S3, DynamoDB, CloudWatch, EC2 describe. If it's an admin-ish demo account, this is usually fine. If not, attach the policy in `demo/instance-role-policy.json` once (as admin).

### 0.2 Get code onto the instance
```bash
# Via SSM or git clone on the instance
cd /home/kirocrew
git clone <your-repo-with-accinfra-agents> accinfra-agents
# OR copy the accinfra-agents folder up
```

### 0.3 Run the installer (one command)
```bash
cd /home/kirocrew/accinfra-agents
AWS_REGION=us-east-1 GITHUB_REPO=Karu0606/sample_dashboard GITHUB_APPROVER=Karu0606 \
  bash install-accinfra.sh
```
Copy the printed **Deploy role ARN** — you need it for the GitHub workflow.

### 0.4 GitHub App key + config
```bash
mkdir -p ~/.kiro/crew
nano ~/.kiro/crew/accinfra-github-app.pem      # paste the .pem
chmod 600 ~/.kiro/crew/accinfra-github-app.pem
nano config/accinfra-config.json               # set app_id + app_installation_id
```

### 0.5 GitHub repo prep (browser)
- Copy `github-actions/deploy.yml` → repo `.github/workflows/accinfra-deploy.yml`
  - Set `DEPLOY_ROLE_ARN`, `AWS_REGION`, `TF_STATE_BUCKET`, `TF_LOCK_TABLE` in its `env:`
- Copy `github-actions/tf-backend-bootstrap.yml` (optional — installer already made the backend)
- Create `infrastructure/` folder with a `.gitkeep`
- Create the 5 labels (see main setup guide)
- Push to `main`

### 0.6 FULL REHEARSAL (critical)
Run the entire Part 1 flow once with a throwaway issue titled `accinfra: rehearsal test`. Confirm every stage works. Then close/delete it. **Do not skip this.**

### 0.7 Cleanup after rehearsal
```bash
# If rehearsal created real resources, destroy them so the live demo is clean
cd infrastructure && terraform destroy -auto-approve  # only rehearsal resources
```

---

## Part 1 — The Live Demo (the actual show)

Keep two windows open:
- **Window L:** SSM session to the instance (to fire agents via `demo/trigger.py`)
- **Window B:** Browser on the GitHub repo + a CloudWatch dashboard tab

### Step 1 — Create the request (30 sec)
**Browser:** New issue in `sample_dashboard`:
- **Title:** `accinfra: Create S3 bucket and log group for demo-app`
- **Body:** `Provision an S3 bucket and a CloudWatch log group for the demo-app project.`

> **Say:** "A developer files an infrastructure request as a GitHub issue. Nothing else — no tickets, no forms."

### Step 2 — Agent 1 detects it (Issue Watcher)
**Window L:**
```bash
cd ~/accinfra-agents && python demo/trigger.py issue-watcher
```
**Browser:** Refresh the issue → labelled `triaged`, bot comment appears.

> **Say:** "Our first agent monitors the repo, recognizes the accinfra prefix, and acknowledges it."

### Step 3 — Agent 2 proposes a solution (Solution Architect)
**Window L:**
```bash
python demo/trigger.py solution-architect
```
**Browser:** Refresh the issue →
- Feasibility report (VPC/IAM/quota checks against the real account)
- Similar past issues referenced
- Terraform code
- A new PR opened on `infra/issue-<N>`

> **Say:** "The second agent first checks the account can actually support this — real quota checks. Then it reuses patterns from past issues, generates Terraform, and opens a PR. It does NOT touch infrastructure yet — a human must approve."

### Step 4 — Human REJECTS (you, as approver)
**Browser:** Open the PR → **Files changed** → **Review changes** → **Request changes**.
Leave a comment like:
> `Please restrict the S3 bucket — add encryption with KMS and enable versioning.`

> **Say:** "Here's the human gate. I'm the approver and I'm not happy — I want KMS encryption. I request changes, just like any code review."

### Step 5 — Agent 3 auto-corrects (Review Handler)
**Window L:**
```bash
python demo/trigger.py review-handler
```
**Browser:** Refresh the PR → new commit + a revision comment addressing your feedback.

> **Say:** "The third agent reads my review, applies the correction, pushes a new commit, and asks for re-review. No human wrote that fix."

### Step 6 — Human APPROVES + merges
**Browser:** Review the PR → **Approve** → **Merge pull request**.

> **Say:** "Now I'm satisfied. I approve and merge. Merging is the trigger for deployment."

### Step 7 — GitHub Actions deploys
**Browser:** Repo → **Actions** tab → watch the `AccInfra: Terraform Deploy` run → `terraform plan` → `apply` → green check.

> **Say:** "Merge kicks off GitHub Actions. It assumes an AWS role via OIDC — no stored keys — and runs terraform apply. The infrastructure is now real."

### Step 8 — Agent 4 closes the loop (Deploy Closer)
**Window L:**
```bash
python demo/trigger.py deploy-closer
```
**Browser:** Refresh the issue →
- Comment with deployed resource names/ARNs + PR reference
- Issue is **closed**, labelled `deployed`

> **Say:** "The fourth agent confirms the deploy succeeded, reads the Terraform state, posts the created resource names back on the issue, and closes it. Full circle."

### Step 9 — Observability
**Browser:** CloudWatch dashboard tab → "AccInfra-Agent-Health".

> **Say:** "Every agent self-reports to CloudWatch. Per-agent panels, error/escalation tracking, and a live error table. If any agent fails, an admin sees exactly which one — no digging through a flat log."

---

## Part 2 — The Message (close strong)

> "What you saw is a skeleton. Today the agents provision infrastructure. Tomorrow the same skeleton runs a code-review agent, an application-monitoring admin, or any workflow you define. Kiro Crew is the persistent Admin Agent platform — you drop in the idea, the platform runs it, observes it, and keeps it accountable. And this scales cross-account: one Kiro Crew hub serving many target accounts."

---

## Fallbacks (if something stalls live)

| Problem | Fallback |
|---------|----------|
| Agent 2 feasibility check errors | You pre-ran it in rehearsal — show the rehearsal PR, say "here's one from moments ago" |
| GitHub Actions OIDC fails | Switch narrative: show the plan output on the PR, say apply runs on approval; demo apply from instance manually |
| Agent auto-correct doesn't match feedback | Use a feedback phrase you tested in rehearsal (e.g., "add encryption", "restrict ingress") — stick to known patterns |
| Cron fires mid-demo and double-processes | DISABLE all crons during the demo; rely only on `demo/trigger.py`. Re-enable after. |
| Everything breaks | Have the rehearsal recording as a backup video |

### Disable crons during demo (avoid surprises)
```bash
kirocrew cron disable accinfra-issue-watcher
kirocrew cron disable accinfra-solution-architect
kirocrew cron disable accinfra-review-handler
kirocrew cron disable accinfra-deploy-closer
```
Re-enable after the demo if you want autonomous mode.

---

## Post-Demo Cleanup
```bash
cd ~/accinfra-agents/infrastructure
terraform destroy -auto-approve   # removes demo-created S3/log group
```

---

## The 3 things that make or break this
1. **Rehearse the full loop once (0.6).** Non-negotiable.
2. **Disable crons; drive with `demo/trigger.py`.** Timing control.
3. **Use review-feedback phrases you tested** so Agent 3's auto-correct matches.
