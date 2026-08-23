# ──────────────────────────────────────────────────────────────────────────────
# AccInfra Agents — CloudWatch Observability Dashboard
# ──────────────────────────────────────────────────────────────────────────────
# Creates:
#   - 4 Log Groups (one per agent)
#   - CloudWatch Dashboard (4 panels: one per agent)
#   - Metric Filters (extract error counts from logs)
#   - CloudWatch Alarms (alert on consecutive failures)
#
# Deploy this alongside the IAM role (one-time per account).
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
      Component   = "observability"
      Environment = var.environment
    }
  }
}

# ─── Variables ────────────────────────────────────────────────────────────────

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Environment name"
  type        = string
  default     = "production"
}

variable "log_retention_days" {
  description = "CloudWatch log retention in days"
  type        = number
  default     = 30
}

variable "alarm_sns_topic_arn" {
  description = "SNS topic ARN for alarm notifications (optional — leave empty to skip alarms)"
  type        = string
  default     = ""
}

# ─── Locals ───────────────────────────────────────────────────────────────────

locals {
  log_group_prefix = "/kirocrew/accinfra"

  agents = {
    issue-watcher      = "Issue Watcher"
    solution-architect = "Solution Architect"
    review-handler     = "Review Handler"
    deploy-closer      = "Deploy Closer"
  }
}

# ─── Log Groups ───────────────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "agents" {
  for_each = local.agents

  name              = "${local.log_group_prefix}/${each.key}"
  retention_in_days = var.log_retention_days
}

# ─── Metric Filters (Extract error count from structured JSON logs) ───────────

resource "aws_cloudwatch_log_metric_filter" "agent_errors" {
  for_each = local.agents

  name           = "accinfra-${each.key}-errors"
  log_group_name = aws_cloudwatch_log_group.agents[each.key].name
  pattern        = "{ $.status = \"ERROR\" }"

  metric_transformation {
    name          = "AgentErrors"
    namespace     = "AccInfra/Agents"
    value         = "1"
    default_value = "0"
    dimensions = {
      AgentName = each.key
    }
  }
}

resource "aws_cloudwatch_log_metric_filter" "agent_escalations" {
  for_each = local.agents

  name           = "accinfra-${each.key}-escalations"
  log_group_name = aws_cloudwatch_log_group.agents[each.key].name
  pattern        = "{ $.status = \"ESCALATION\" }"

  metric_transformation {
    name          = "AgentEscalations"
    namespace     = "AccInfra/Agents"
    value         = "1"
    default_value = "0"
    dimensions = {
      AgentName = each.key
    }
  }
}

resource "aws_cloudwatch_log_metric_filter" "agent_success" {
  for_each = local.agents

  name           = "accinfra-${each.key}-success"
  log_group_name = aws_cloudwatch_log_group.agents[each.key].name
  pattern        = "{ $.status = \"OK\" }"

  metric_transformation {
    name          = "AgentSuccess"
    namespace     = "AccInfra/Agents"
    value         = "1"
    default_value = "0"
    dimensions = {
      AgentName = each.key
    }
  }
}

# ─── Alarms (only created if SNS topic is provided) ──────────────────────────

resource "aws_cloudwatch_metric_alarm" "agent_error_alarm" {
  for_each = var.alarm_sns_topic_arn != "" ? local.agents : {}

  alarm_name          = "accinfra-${each.key}-errors"
  alarm_description   = "AccInfra ${each.value} agent has 3+ consecutive errors"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 3
  metric_name         = "AgentErrors"
  namespace           = "AccInfra/Agents"
  period              = 300 # 5 minutes
  statistic           = "Sum"
  threshold           = 1
  treat_missing_data  = "notBreaching"

  dimensions = {
    AgentName = each.key
  }

  alarm_actions = [var.alarm_sns_topic_arn]
  ok_actions    = [var.alarm_sns_topic_arn]
}

# ─── CloudWatch Dashboard ────────────────────────────────────────────────────

resource "aws_cloudwatch_dashboard" "accinfra" {
  dashboard_name = "AccInfra-Agent-Health"

  dashboard_body = jsonencode({
    widgets = [
      # ─── Row 1: Overview header ───
      {
        type   = "text"
        x      = 0
        y      = 0
        width  = 24
        height = 2
        properties = {
          markdown = "# AccInfra Agent Health Dashboard\nReal-time status of all 4 infrastructure automation agents. Green = healthy. Red = needs attention."
        }
      },

      # ─── Row 2: Agent Success/Error metrics (4 panels) ───

      # Issue Watcher
      {
        type   = "metric"
        x      = 0
        y      = 2
        width  = 6
        height = 6
        properties = {
          title   = "Issue Watcher"
          region  = var.aws_region
          view    = "timeSeries"
          stacked = true
          metrics = [
            ["AccInfra/Agents", "AgentSuccess", "AgentName", "issue-watcher", { color = "#2ca02c", label = "Success" }],
            ["AccInfra/Agents", "AgentErrors", "AgentName", "issue-watcher", { color = "#d62728", label = "Error" }],
            ["AccInfra/Agents", "AgentEscalations", "AgentName", "issue-watcher", { color = "#ff7f0e", label = "Escalation" }],
          ]
          period = 300
          stat   = "Sum"
        }
      },

      # Solution Architect
      {
        type   = "metric"
        x      = 6
        y      = 2
        width  = 6
        height = 6
        properties = {
          title   = "Solution Architect"
          region  = var.aws_region
          view    = "timeSeries"
          stacked = true
          metrics = [
            ["AccInfra/Agents", "AgentSuccess", "AgentName", "solution-architect", { color = "#2ca02c", label = "Success" }],
            ["AccInfra/Agents", "AgentErrors", "AgentName", "solution-architect", { color = "#d62728", label = "Error" }],
            ["AccInfra/Agents", "AgentEscalations", "AgentName", "solution-architect", { color = "#ff7f0e", label = "Escalation" }],
          ]
          period = 300
          stat   = "Sum"
        }
      },

      # Review Handler
      {
        type   = "metric"
        x      = 12
        y      = 2
        width  = 6
        height = 6
        properties = {
          title   = "Review Handler"
          region  = var.aws_region
          view    = "timeSeries"
          stacked = true
          metrics = [
            ["AccInfra/Agents", "AgentSuccess", "AgentName", "review-handler", { color = "#2ca02c", label = "Success" }],
            ["AccInfra/Agents", "AgentErrors", "AgentName", "review-handler", { color = "#d62728", label = "Error" }],
            ["AccInfra/Agents", "AgentEscalations", "AgentName", "review-handler", { color = "#ff7f0e", label = "Escalation" }],
          ]
          period = 300
          stat   = "Sum"
        }
      },

      # Deploy Closer
      {
        type   = "metric"
        x      = 18
        y      = 2
        width  = 6
        height = 6
        properties = {
          title   = "Deploy Closer"
          region  = var.aws_region
          view    = "timeSeries"
          stacked = true
          metrics = [
            ["AccInfra/Agents", "AgentSuccess", "AgentName", "deploy-closer", { color = "#2ca02c", label = "Success" }],
            ["AccInfra/Agents", "AgentErrors", "AgentName", "deploy-closer", { color = "#d62728", label = "Error" }],
            ["AccInfra/Agents", "AgentEscalations", "AgentName", "deploy-closer", { color = "#ff7f0e", label = "Escalation" }],
          ]
          period = 300
          stat   = "Sum"
        }
      },

      # ─── Row 3: Combined error rate ───
      {
        type   = "metric"
        x      = 0
        y      = 8
        width  = 12
        height = 6
        properties = {
          title   = "All Agents — Error Rate (5min)"
          region  = var.aws_region
          view    = "timeSeries"
          stacked = false
          metrics = [
            ["AccInfra/Agents", "AgentErrors", "AgentName", "issue-watcher", { label = "Issue Watcher" }],
            ["AccInfra/Agents", "AgentErrors", "AgentName", "solution-architect", { label = "Solution Architect" }],
            ["AccInfra/Agents", "AgentErrors", "AgentName", "review-handler", { label = "Review Handler" }],
            ["AccInfra/Agents", "AgentErrors", "AgentName", "deploy-closer", { label = "Deploy Closer" }],
          ]
          period = 300
          stat   = "Sum"
        }
      },

      # ─── Row 3: Combined success rate ───
      {
        type   = "metric"
        x      = 12
        y      = 8
        width  = 12
        height = 6
        properties = {
          title   = "All Agents — Success Rate (5min)"
          region  = var.aws_region
          view    = "timeSeries"
          stacked = false
          metrics = [
            ["AccInfra/Agents", "AgentSuccess", "AgentName", "issue-watcher", { label = "Issue Watcher" }],
            ["AccInfra/Agents", "AgentSuccess", "AgentName", "solution-architect", { label = "Solution Architect" }],
            ["AccInfra/Agents", "AgentSuccess", "AgentName", "review-handler", { label = "Review Handler" }],
            ["AccInfra/Agents", "AgentSuccess", "AgentName", "deploy-closer", { label = "Deploy Closer" }],
          ]
          period = 300
          stat   = "Sum"
        }
      },

      # ─── Row 4: Log Insights queries ───
      {
        type   = "log"
        x      = 0
        y      = 14
        width  = 24
        height = 6
        properties = {
          title   = "Recent Agent Errors (All Agents)"
          region  = var.aws_region
          query   = "SOURCE '${local.log_group_prefix}/issue-watcher' | SOURCE '${local.log_group_prefix}/solution-architect' | SOURCE '${local.log_group_prefix}/review-handler' | SOURCE '${local.log_group_prefix}/deploy-closer' | fields @timestamp, agent, status, message, error_summary, issue_id | filter status = 'ERROR' or status = 'ESCALATION' | sort @timestamp desc | limit 20"
          view    = "table"
        }
      },
    ]
  })
}

# ─── Outputs ──────────────────────────────────────────────────────────────────

output "dashboard_url" {
  description = "Direct URL to the CloudWatch Dashboard"
  value       = "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards:name=AccInfra-Agent-Health"
}

output "log_groups" {
  description = "Created log group names"
  value       = { for k, v in aws_cloudwatch_log_group.agents : k => v.name }
}
