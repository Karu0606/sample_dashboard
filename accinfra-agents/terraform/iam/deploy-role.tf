# ──────────────────────────────────────────────────────────────────────────────
# AccInfra — GitHub Actions Deploy Role
# ──────────────────────────────────────────────────────────────────────────────
# This is the role used by GitHub Actions to run `terraform apply`.
# It has WRITE access to create/modify/delete infrastructure.
#
# Separation of concerns:
#   - Agent role (main.tf): READ-only, for checks and observability
#   - Deploy role (this file): WRITE, for actual infrastructure provisioning
#
# Trust: GitHub OIDC provider (no long-lived keys in GitHub secrets)
# ──────────────────────────────────────────────────────────────────────────────

# ─── GitHub OIDC Provider ─────────────────────────────────────────────────────
# Create this once per account. If you already have one, import it or remove this.

resource "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"

  client_id_list = ["sts.amazonaws.com"]

  # GitHub's OIDC thumbprint (stable, published by GitHub)
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

# ─── Deploy Role ──────────────────────────────────────────────────────────────

resource "aws_iam_role" "accinfra_deploy" {
  name        = "accinfra-deploy-role"
  description = "Role assumed by GitHub Actions for terraform apply (infrastructure provisioning)"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = aws_iam_openid_connect_provider.github.arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          }
          StringLike = {
            # Only allow from the specific repo and infra branches
            "token.actions.githubusercontent.com:sub" = "repo:${var.github_repo}:ref:refs/heads/main"
          }
        }
      }
    ]
  })

  max_session_duration = 3600
}

# ─── Deploy Policy ────────────────────────────────────────────────────────────
# Scoped to the resource types agents are allowed to create.
# This mirrors the security.allowed_terraform_resources in config.

resource "aws_iam_policy" "deploy_infra" {
  name        = "accinfra-deploy-infra"
  description = "Infrastructure provisioning permissions for GitHub Actions deploy"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "VPCNetworking"
        Effect = "Allow"
        Action = [
          "ec2:CreateVpc",
          "ec2:DeleteVpc",
          "ec2:ModifyVpcAttribute",
          "ec2:CreateSubnet",
          "ec2:DeleteSubnet",
          "ec2:CreateSecurityGroup",
          "ec2:DeleteSecurityGroup",
          "ec2:AuthorizeSecurityGroupIngress",
          "ec2:AuthorizeSecurityGroupEgress",
          "ec2:RevokeSecurityGroupIngress",
          "ec2:RevokeSecurityGroupEgress",
          "ec2:CreateInternetGateway",
          "ec2:DeleteInternetGateway",
          "ec2:AttachInternetGateway",
          "ec2:DetachInternetGateway",
          "ec2:CreateNatGateway",
          "ec2:DeleteNatGateway",
          "ec2:AllocateAddress",
          "ec2:ReleaseAddress",
          "ec2:AssociateAddress",
          "ec2:DisassociateAddress",
          "ec2:CreateRouteTable",
          "ec2:DeleteRouteTable",
          "ec2:CreateRoute",
          "ec2:DeleteRoute",
          "ec2:AssociateRouteTable",
          "ec2:DisassociateRouteTable",
          "ec2:CreateFlowLogs",
          "ec2:DeleteFlowLogs",
          "ec2:CreateTags",
          "ec2:DeleteTags",
          "ec2:Describe*",
        ]
        Resource = "*"
      },
      {
        Sid    = "IAMRolesAndPolicies"
        Effect = "Allow"
        Action = [
          "iam:CreateRole",
          "iam:DeleteRole",
          "iam:GetRole",
          "iam:UpdateRole",
          "iam:PassRole",
          "iam:TagRole",
          "iam:UntagRole",
          "iam:CreatePolicy",
          "iam:DeletePolicy",
          "iam:GetPolicy",
          "iam:GetPolicyVersion",
          "iam:CreatePolicyVersion",
          "iam:DeletePolicyVersion",
          "iam:ListPolicyVersions",
          "iam:AttachRolePolicy",
          "iam:DetachRolePolicy",
          "iam:PutRolePolicy",
          "iam:DeleteRolePolicy",
          "iam:GetRolePolicy",
          "iam:ListRolePolicies",
          "iam:ListAttachedRolePolicies",
          "iam:CreateInstanceProfile",
          "iam:DeleteInstanceProfile",
          "iam:AddRoleToInstanceProfile",
          "iam:RemoveRoleFromInstanceProfile",
          "iam:GetInstanceProfile",
          "iam:ListInstanceProfiles",
          "iam:ListInstanceProfilesForRole",
        ]
        # Restrict to accinfra-prefixed roles only
        Resource = [
          "arn:aws:iam::*:role/accinfra-*",
          "arn:aws:iam::*:policy/accinfra-*",
          "arn:aws:iam::*:instance-profile/accinfra-*",
        ]
      },
      {
        Sid    = "ECS"
        Effect = "Allow"
        Action = [
          "ecs:CreateCluster",
          "ecs:DeleteCluster",
          "ecs:DescribeClusters",
          "ecs:CreateService",
          "ecs:DeleteService",
          "ecs:UpdateService",
          "ecs:DescribeServices",
          "ecs:RegisterTaskDefinition",
          "ecs:DeregisterTaskDefinition",
          "ecs:DescribeTaskDefinition",
          "ecs:ListClusters",
          "ecs:ListServices",
          "ecs:TagResource",
          "ecs:UntagResource",
        ]
        Resource = "*"
      },
      {
        Sid    = "S3"
        Effect = "Allow"
        Action = [
          "s3:CreateBucket",
          "s3:DeleteBucket",
          "s3:PutBucketVersioning",
          "s3:GetBucketVersioning",
          "s3:PutEncryptionConfiguration",
          "s3:GetEncryptionConfiguration",
          "s3:PutBucketPublicAccessBlock",
          "s3:GetBucketPublicAccessBlock",
          "s3:PutBucketTagging",
          "s3:GetBucketTagging",
          "s3:PutBucketPolicy",
          "s3:GetBucketPolicy",
          "s3:DeleteBucketPolicy",
          "s3:ListBucket",
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
        ]
        Resource = [
          "arn:aws:s3:::accinfra-*",
          "arn:aws:s3:::accinfra-*/*",
        ]
      },
      {
        Sid    = "LoadBalancer"
        Effect = "Allow"
        Action = [
          "elasticloadbalancing:CreateLoadBalancer",
          "elasticloadbalancing:DeleteLoadBalancer",
          "elasticloadbalancing:ModifyLoadBalancerAttributes",
          "elasticloadbalancing:DescribeLoadBalancers",
          "elasticloadbalancing:CreateTargetGroup",
          "elasticloadbalancing:DeleteTargetGroup",
          "elasticloadbalancing:ModifyTargetGroup",
          "elasticloadbalancing:DescribeTargetGroups",
          "elasticloadbalancing:CreateListener",
          "elasticloadbalancing:DeleteListener",
          "elasticloadbalancing:DescribeListeners",
          "elasticloadbalancing:AddTags",
          "elasticloadbalancing:RemoveTags",
        ]
        Resource = "*"
      },
      {
        Sid    = "DynamoDB"
        Effect = "Allow"
        Action = [
          "dynamodb:CreateTable",
          "dynamodb:DeleteTable",
          "dynamodb:DescribeTable",
          "dynamodb:UpdateTable",
          "dynamodb:TagResource",
          "dynamodb:UntagResource",
          "dynamodb:ListTables",
        ]
        Resource = "arn:aws:dynamodb:*:*:table/accinfra-*"
      },
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:DeleteLogGroup",
          "logs:PutRetentionPolicy",
          "logs:TagLogGroup",
          "logs:DescribeLogGroups",
        ]
        Resource = "*"
      },
      {
        Sid    = "Lambda"
        Effect = "Allow"
        Action = [
          "lambda:CreateFunction",
          "lambda:DeleteFunction",
          "lambda:GetFunction",
          "lambda:UpdateFunctionCode",
          "lambda:UpdateFunctionConfiguration",
          "lambda:ListVersionsByFunction",
          "lambda:TagResource",
          "lambda:UntagResource",
          "lambda:AddPermission",
          "lambda:RemovePermission",
          "lambda:GetPolicy",
        ]
        Resource = "arn:aws:lambda:*:*:function:accinfra-*"
      },
      {
        Sid    = "ECR"
        Effect = "Allow"
        Action = [
          "ecr:CreateRepository",
          "ecr:DeleteRepository",
          "ecr:DescribeRepositories",
          "ecr:TagResource",
          "ecr:UntagResource",
          "ecr:PutLifecyclePolicy",
          "ecr:GetLifecyclePolicy",
        ]
        Resource = "arn:aws:ecr:*:*:repository/accinfra-*"
      },
      {
        Sid    = "TerraformState"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucket",
        ]
        Resource = [
          "arn:aws:s3:::${var.terraform_state_bucket}",
          "arn:aws:s3:::${var.terraform_state_bucket}/*",
        ]
      },
      {
        Sid    = "TerraformLock"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:DeleteItem",
          "dynamodb:DescribeTable",
        ]
        Resource = "arn:aws:dynamodb:*:*:table/${var.terraform_lock_table}"
      },
    ]
  })
}

# ─── Explicit Deny: Block Dangerous Actions ───────────────────────────────────

resource "aws_iam_policy" "deploy_deny" {
  name        = "accinfra-deploy-deny"
  description = "Explicit deny for dangerous actions that agents should never perform"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DenyDangerous"
        Effect = "Deny"
        Action = [
          "iam:CreateUser",
          "iam:CreateAccessKey",
          "iam:DeleteAccountAlias",
          "organizations:*",
          "kms:ScheduleKeyDeletion",
          "kms:DisableKey",
          "ec2:TerminateInstances",
          "rds:DeleteDBInstance",
          "rds:DeleteDBCluster",
        ]
        Resource = "*"
      }
    ]
  })
}

# ─── Attach Policies to Deploy Role ──────────────────────────────────────────

resource "aws_iam_role_policy_attachment" "deploy_infra" {
  role       = aws_iam_role.accinfra_deploy.name
  policy_arn = aws_iam_policy.deploy_infra.arn
}

resource "aws_iam_role_policy_attachment" "deploy_deny" {
  role       = aws_iam_role.accinfra_deploy.name
  policy_arn = aws_iam_policy.deploy_deny.arn
}

# ─── Variables ────────────────────────────────────────────────────────────────

variable "github_repo" {
  description = "GitHub repo in 'owner/name' format for OIDC trust (e.g., 'Karu0606/sample_dashboard')"
  type        = string
  default     = "Karu0606/sample_dashboard"
}

# ─── Outputs ──────────────────────────────────────────────────────────────────

output "deploy_role_arn" {
  description = "ARN of the deploy role — use in GitHub Actions workflow"
  value       = aws_iam_role.accinfra_deploy.arn
}

output "github_oidc_provider_arn" {
  description = "ARN of the GitHub OIDC provider"
  value       = aws_iam_openid_connect_provider.github.arn
}
