#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
#  AccInfra — One-Shot Installer (run ON the Kiro Crew EC2 instance)
# ══════════════════════════════════════════════════════════════════════════════
#  Single-account mode (Account A does everything).
#  Uses the EC2 instance role for AWS access — no keys, no assume-role hop.
#
#  Run via SSM:
#    aws ssm send-command \
#      --instance-ids <ID> \
#      --document-name "AWS-RunShellScript" \
#      --parameters 'commands=["cd /home/kirocrew/accinfra-agents && bash install-accinfra.sh"]'
#
#  Or directly on the box:
#    cd ~/accinfra-agents && bash install-accinfra.sh
# ══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

# ─── Config (defaults set for the demo — Account A) ───────────────────────────
AWS_REGION="${AWS_REGION:-us-east-1}"
GITHUB_REPO="${GITHUB_REPO:-Karu0606/sample_dashboard}"
GITHUB_APPROVER="${GITHUB_APPROVER:-Karu0606}"
STATE_BUCKET_SUFFIX="${STATE_BUCKET_SUFFIX:-}"   # leave empty; script fills with account id
# ───────────────────────────────────────────────────────────────────────────────

green() { printf "\033[0;32m%s\033[0m\n" "$1"; }
yellow() { printf "\033[0;33m%s\033[0m\n" "$1"; }
cyan() { printf "\033[0;36m%s\033[0m\n" "$1"; }
info() { printf "  → %s\n" "$1"; }

cyan "══════════════════════════════════════════════════════"
cyan "  AccInfra Installer — single-account (Account A)"
cyan "══════════════════════════════════════════════════════"

# ─── 1. Resolve account + identity from the instance role ──────────────────────
green "[1/7] Resolving AWS identity (instance role)"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
CALLER_ARN="$(aws sts get-caller-identity --query Arn --output text)"
info "Account: $ACCOUNT_ID"
info "Identity: $CALLER_ARN"
info "Region: $AWS_REGION"

STATE_BUCKET="accinfra-tfstate-${ACCOUNT_ID}"
LOCK_TABLE="accinfra-tfstate-lock"

# ─── 2. Create Terraform state backend (S3 + DynamoDB) ─────────────────────────
green "[2/7] Terraform state backend"
if aws s3api head-bucket --bucket "$STATE_BUCKET" 2>/dev/null; then
    info "State bucket already exists: $STATE_BUCKET"
else
    info "Creating state bucket: $STATE_BUCKET"
    if [ "$AWS_REGION" = "us-east-1" ]; then
        aws s3api create-bucket --bucket "$STATE_BUCKET" --region "$AWS_REGION"
    else
        aws s3api create-bucket --bucket "$STATE_BUCKET" --region "$AWS_REGION" \
            --create-bucket-configuration LocationConstraint="$AWS_REGION"
    fi
    aws s3api put-bucket-versioning --bucket "$STATE_BUCKET" \
        --versioning-configuration Status=Enabled
    aws s3api put-bucket-encryption --bucket "$STATE_BUCKET" \
        --server-side-encryption-configuration \
        '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'
    aws s3api put-public-access-block --bucket "$STATE_BUCKET" \
        --public-access-block-configuration \
        'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true'
fi

if aws dynamodb describe-table --table-name "$LOCK_TABLE" --region "$AWS_REGION" >/dev/null 2>&1; then
    info "Lock table already exists: $LOCK_TABLE"
else
    info "Creating lock table: $LOCK_TABLE"
    aws dynamodb create-table --table-name "$LOCK_TABLE" \
        --attribute-definitions AttributeName=LockID,AttributeType=S \
        --key-schema AttributeName=LockID,KeyType=HASH \
        --billing-mode PAY_PER_REQUEST --region "$AWS_REGION" >/dev/null
    aws dynamodb wait table-exists --table-name "$LOCK_TABLE" --region "$AWS_REGION"
fi

# ─── 3. Deploy IAM (agent role + deploy role + GitHub OIDC) ────────────────────
green "[3/7] IAM roles + GitHub OIDC provider (Terraform)"
pushd terraform/iam >/dev/null
cat > terraform.tfvars <<EOF
aws_region                  = "${AWS_REGION}"
environment                 = "demo"
kiro_crew_instance_role_arn = "${CALLER_ARN}"
terraform_state_bucket      = "${STATE_BUCKET}"
terraform_state_key         = "infra/terraform.tfstate"
terraform_lock_table        = "${LOCK_TABLE}"
github_repo                 = "${GITHUB_REPO}"
EOF
info "Wrote terraform.tfvars"
terraform init -input=false >/dev/null
terraform apply -auto-approve -input=false
DEPLOY_ROLE_ARN="$(terraform output -raw deploy_role_arn)"
AGENT_ROLE_ARN="$(terraform output -raw agent_role_arn)"
popd >/dev/null
info "Deploy role: $DEPLOY_ROLE_ARN"
info "Agent role:  $AGENT_ROLE_ARN"

# ─── 4. Deploy CloudWatch observability ────────────────────────────────────────
green "[4/7] CloudWatch dashboard + log groups (Terraform)"
pushd terraform/observability >/dev/null
terraform init -input=false >/dev/null
terraform apply -auto-approve -input=false -var="aws_region=${AWS_REGION}" -var="environment=demo"
DASHBOARD_URL="$(terraform output -raw dashboard_url 2>/dev/null || echo 'see CloudWatch console')"
popd >/dev/null
info "Dashboard: $DASHBOARD_URL"

# ─── 5. Patch the runtime config with real values ──────────────────────────────
green "[5/7] Updating accinfra-config.json with resolved values"
CONFIG_FILE="config/accinfra-config.json"
python3 - "$CONFIG_FILE" "$ACCOUNT_ID" "$AWS_REGION" "$AGENT_ROLE_ARN" "$STATE_BUCKET" "$LOCK_TABLE" "$GITHUB_REPO" "$GITHUB_APPROVER" <<'PYEOF'
import json, sys
cfg_path, account, region, agent_role, bucket, lock, repo, approver = sys.argv[1:9]
with open(cfg_path) as f:
    cfg = json.load(f)
owner, name = repo.split("/", 1)
cfg["github"]["repo_owner"] = owner
cfg["github"]["repo_name"] = name
cfg["github"]["approver_username"] = approver
# For single-account demo, agents use the instance role directly.
cfg["aws"]["region"] = region
cfg["aws"]["agent_role_arn"] = agent_role
cfg["aws"]["terraform_state_bucket"] = bucket
cfg["aws"]["terraform_lock_table"] = lock
with open(cfg_path, "w") as f:
    json.dump(cfg, f, indent=2)
print("  → config updated")
PYEOF

# ─── 6. Install Python deps ────────────────────────────────────────────────────
green "[6/7] Installing Python dependencies"
python3 -m pip install --quiet -r requirements.txt || yellow "pip install had warnings (continuing)"

# ─── 7. Smoke test ─────────────────────────────────────────────────────────────
green "[7/7] Smoke test — AWS + config load"
python3 - <<'PYEOF'
from shared.config_loader import load_config
from shared.aws_client import AWSClient
cfg = load_config()
aws = AWSClient(cfg, session_name="install-smoketest")
ec2 = aws.get_client("ec2")
n = len(ec2.describe_vpcs().get("Vpcs", []))
print(f"  → AWS OK. VPCs visible: {n}")
print("  → Config loaded OK.")
PYEOF

cyan "══════════════════════════════════════════════════════"
green "  ✅ AccInfra installed."
cyan "══════════════════════════════════════════════════════"
echo
echo "  Deploy role ARN (put in GitHub Actions env):"
echo "    $DEPLOY_ROLE_ARN"
echo "  State bucket:  $STATE_BUCKET"
echo "  Lock table:    $LOCK_TABLE"
echo "  Dashboard:     $DASHBOARD_URL"
echo
echo "  Remaining manual steps:"
echo "    1. Drop GitHub App key at ~/.kiro/crew/accinfra-github-app.pem"
echo "    2. Set app_id + app_installation_id in config/accinfra-config.json"
echo "    3. Add workflow files + labels to the GitHub repo"
echo "    4. Register crons (or use demo/trigger.py for the live demo)"
echo
