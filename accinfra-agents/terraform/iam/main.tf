# ──────────────────────────────────────────────────────────────────────────────
# AccInfra Agents — IAM Role (Client-Portable)
# ──────────────────────────────────────────────────────────────────────────────
# This Terraform creates the IAM role that Kiro Crew agents assume via STS.
# Deploy this ONCE per AWS account. The agents use temporary credentials only.
#
# Principle: Least-privilege. Agents get:
#   - READ access to describe infrastructure (for feasibility checks)
#   - WRITE access to CloudWatch Logs (for observability)
#   - READ access to Terraform state in S3 (for deploy-closer resource discovery)
#   - READ access to Service Quotas (for limit checks)
#
# The agents do NOT directly create infrastructure — that's handled by
# GitHub Actions with a separate, more permissive deploy role.
# ──────────────────────────────────────────────────────────────────────────────

terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      ManagedBy   = "accinfra-agents"
      Project     = "accinfra"
      Component   = "iam"
      Environment = var.environment
    }
  }
}

# ─── Variables ────────────────────────────────────────────────────────────────

variable "aws_region" {
  description = "AWS region for deployment"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Environment name (e.g., production, staging)"
  type        = string
  default     = "production"
}

variable "kiro_crew_instance_role_arn" {
  description = "ARN of the Kiro Crew EC2 instance role (or IAM user) that will assume this agent role. This is the 'trusted entity'."
  type        = string
  # Example: arn:aws:iam::123456789012:role/kirocrew-ec2-kirocrew
  # Or for local dev: arn:aws:iam::123456789012:user/developer
}

variable "terraform_state_bucket" {
  description = "S3 bucket name where Terraform state is stored"
  type        = string
}

variable "terraform_state_key" {
  description = "S3 key path for the Terraform state file"
  type        = string
  default     = "infra/terraform.tfstate"
}

variable "terraform_lock_table" {
  description = "DynamoDB table name for Terraform state locking"
  type        = string
  default     = "accinfra-tfstate-lock"
}

# ─── Agent Role ───────────────────────────────────────────────────────────────

resource "aws_iam_role" "accinfra_agent" {
  name        = "accinfra-agent-role"
  description = "Role assumed by AccInfra Kiro Crew agents for infra checks and observability"

  # Trust policy: only the Kiro Crew instance can assume this role
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          AWS = var.kiro_crew_instance_role_arn
        }
        Action = "sts:AssumeRole"
        Condition = {
          StringEquals = {
            "sts:ExternalId" = "accinfra-agents"
          }
        }
      }
    ]
  })

  # Maximum session duration: 1 hour (agents run for seconds)
  max_session_duration = 3600
}

# ─── Policy: EC2 Describe (Read-Only) ────────────────────────────────────────
# For feasibility checks: VPCs, subnets, security groups, NAT gateways, EIPs

resource "aws_iam_policy" "ec2_describe" {
  name        = "accinfra-agent-ec2-describe"
  description = "Read-only EC2/VPC access for infrastructure feasibility checks"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EC2Describe"
        Effect = "Allow"
        Action = [
          "ec2:DescribeVpcs",
          "ec2:DescribeSubnets",
          "ec2:DescribeSecurityGroups",
          "ec2:DescribeNatGateways",
          "ec2:DescribeAddresses",
          "ec2:DescribeInternetGateways",
          "ec2:DescribeRouteTables",
          "ec2:DescribeNetworkInterfaces",
          "ec2:DescribeAvailabilityZones",
        ]
        Resource = "*"
      }
    ]
  })
}

# ─── Policy: IAM Describe (Read-Only) ────────────────────────────────────────
# For feasibility checks: role/user counts

resource "aws_iam_policy" "iam_describe" {
  name        = "accinfra-agent-iam-describe"
  description = "Read-only IAM access for quota/usage checks"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "IAMDescribe"
        Effect = "Allow"
        Action = [
          "iam:GetAccountSummary",
          "iam:ListRoles",
          "iam:ListUsers",
          "iam:ListPolicies",
        ]
        Resource = "*"
      }
    ]
  })
}

# ─── Policy: Service Quotas (Read-Only) ──────────────────────────────────────
# For reading account-level service limits

resource "aws_iam_policy" "service_quotas" {
  name        = "accinfra-agent-service-quotas"
  description = "Read-only Service Quotas access for limit checks"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ServiceQuotas"
        Effect = "Allow"
        Action = [
          "servicequotas:GetServiceQuota",
          "servicequotas:ListServiceQuotas",
          "servicequotas:GetAWSDefaultServiceQuota",
        ]
        Resource = "*"
      }
    ]
  })
}

# ─── Policy: CloudWatch Logs (Write) ─────────────────────────────────────────
# For agent observability — write structured logs

resource "aws_iam_policy" "cloudwatch_logs" {
  name        = "accinfra-agent-cloudwatch-logs"
  description = "CloudWatch Logs write access for agent observability"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogGroups",
          "logs:DescribeLogStreams",
        ]
        Resource = [
          "arn:aws:logs:${var.aws_region}:*:log-group:/kirocrew/accinfra/*",
          "arn:aws:logs:${var.aws_region}:*:log-group:/kirocrew/accinfra/*:*",
        ]
      }
    ]
  })
}

# ─── Policy: S3 Terraform State (Read-Only) ──────────────────────────────────
# For deploy-closer: read state to discover deployed resources

resource "aws_iam_policy" "s3_tfstate_read" {
  name        = "accinfra-agent-s3-tfstate-read"
  description = "Read-only access to Terraform state bucket"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "S3StateRead"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:ListBucket",
        ]
        Resource = [
          "arn:aws:s3:::${var.terraform_state_bucket}",
          "arn:aws:s3:::${var.terraform_state_bucket}/${var.terraform_state_key}",
        ]
      }
    ]
  })
}

# ─── Policy: DynamoDB State Lock (Read-Only) ──────────────────────────────────
# Agents only READ lock status, never acquire locks

resource "aws_iam_policy" "dynamodb_lock_read" {
  name        = "accinfra-agent-dynamodb-lock-read"
  description = "Read-only access to Terraform state lock table"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DynamoDBLockRead"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:DescribeTable",
        ]
        Resource = "arn:aws:dynamodb:${var.aws_region}:*:table/${var.terraform_lock_table}"
      }
    ]
  })
}

# ─── Attach All Policies to the Agent Role ────────────────────────────────────

resource "aws_iam_role_policy_attachment" "ec2_describe" {
  role       = aws_iam_role.accinfra_agent.name
  policy_arn = aws_iam_policy.ec2_describe.arn
}

resource "aws_iam_role_policy_attachment" "iam_describe" {
  role       = aws_iam_role.accinfra_agent.name
  policy_arn = aws_iam_policy.iam_describe.arn
}

resource "aws_iam_role_policy_attachment" "service_quotas" {
  role       = aws_iam_role.accinfra_agent.name
  policy_arn = aws_iam_policy.service_quotas.arn
}

resource "aws_iam_role_policy_attachment" "cloudwatch_logs" {
  role       = aws_iam_role.accinfra_agent.name
  policy_arn = aws_iam_policy.cloudwatch_logs.arn
}

resource "aws_iam_role_policy_attachment" "s3_tfstate_read" {
  role       = aws_iam_role.accinfra_agent.name
  policy_arn = aws_iam_policy.s3_tfstate_read.arn
}

resource "aws_iam_role_policy_attachment" "dynamodb_lock_read" {
  role       = aws_iam_role.accinfra_agent.name
  policy_arn = aws_iam_policy.dynamodb_lock_read.arn
}

# ─── Outputs ──────────────────────────────────────────────────────────────────

output "agent_role_arn" {
  description = "ARN of the agent role — put this in accinfra-config.json aws.agent_role_arn"
  value       = aws_iam_role.accinfra_agent.arn
}

output "agent_role_name" {
  description = "Name of the agent role"
  value       = aws_iam_role.accinfra_agent.name
}
