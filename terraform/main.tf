terraform {
  required_version = ">= 1.0"
  
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# Construct the full S3 data path from components
# Pattern: {s3_bucket_name}/AWSLogs/{account_id}/KiroLogs/user_report/{region}
locals {
  s3_data_path = "${var.s3_bucket_name}/AWSLogs/${var.aws_account_id}/KiroLogs/user_report/${var.aws_region}"
  # Extract the actual bucket name (before the first slash) for ARN-based policies
  s3_bucket_only   = split("/", var.s3_bucket_name)[0]
  cur_enabled      = var.cur_s3_bucket_name != ""
  cur_bucket_only  = local.cur_enabled ? split("/", var.cur_s3_bucket_name)[0] : ""
}

# S3 Bucket for Athena query results
resource "aws_s3_bucket" "athena_results" {
  bucket = "${var.project_name}-athena-results-${var.aws_account_id}"
  
  #tags = merge(var.tags, {
  #  Name = "${var.project_name}-athena-results"
  #})
}

# Block all public access to Athena results bucket
resource "aws_s3_bucket_public_access_block" "athena_results_public_access_block" {
  bucket = aws_s3_bucket.athena_results.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Enable server-side encryption for Athena results bucket
resource "aws_s3_bucket_server_side_encryption_configuration" "athena_results_encryption" {
  bucket = aws_s3_bucket.athena_results.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

# Enable versioning for Athena results bucket
resource "aws_s3_bucket_versioning" "athena_results_versioning" {
  bucket = aws_s3_bucket.athena_results.id
  
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "athena_results_lifecycle" {
  bucket = aws_s3_bucket.athena_results.id
  
  rule {
    id     = "delete-old-results"
    status = "Enabled"
    
    filter {
      prefix = ""
    }
    
    expiration {
      days = 7
    }
  }
}

# Glue Database
resource "aws_glue_catalog_database" "analytics_db" {
  name        = var.glue_database_name
  description = "Database for Kiro analytics data"
}

# IAM Role for Glue Crawler
resource "aws_iam_role" "glue_crawler_role" {
  name = "${var.project_name}-glue-crawler-role"
  
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "glue.amazonaws.com"
        }
      }
    ]
  })
  
  #tags = var.tags
}

resource "aws_iam_role_policy_attachment" "glue_service_policy" {
  role       = aws_iam_role.glue_crawler_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole"
}

resource "aws_iam_role_policy" "glue_s3_policy" {
  name = "${var.project_name}-glue-s3-policy"
  role = aws_iam_role.glue_crawler_role.id
  
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:ListBucket"
        ]
        Resource = [
          "arn:aws:s3:::${local.s3_bucket_only}",
          "arn:aws:s3:::${local.s3_data_path}/*"
        ]
      }
    ]
  })
}

# Glue Crawler
resource "aws_glue_crawler" "analytics_crawler" {
  name          = "${var.project_name}-crawler"
  role          = aws_iam_role.glue_crawler_role.arn
  database_name = aws_glue_catalog_database.analytics_db.name
  
  s3_target {
    path = "s3://${local.s3_data_path}/"
  }
  
  schema_change_policy {
    delete_behavior = "LOG"
    update_behavior = "UPDATE_IN_DATABASE"
  }
  
  configuration = jsonencode({
    Version = 1.0
    CrawlerOutput = {
      Partitions = { AddOrUpdateBehavior = "InheritFromTable" }
    }
  })
  
  schedule = var.glue_crawler_schedule
  
  #tags = var.tags
}

# Athena Workgroup
resource "aws_athena_workgroup" "analytics_workgroup" {
  name = "${var.project_name}-workgroup"
  
  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true
    
    result_configuration {
      output_location = "s3://${aws_s3_bucket.athena_results.bucket}/"
      
      encryption_configuration {
        encryption_option = "SSE_S3"
      }
    }
  }
  
  #tags = var.tags
}

# IAM Policy for Athena access (for application)
resource "aws_iam_policy" "athena_access_policy" {
  name        = "${var.project_name}-athena-access"
  description = "Policy for accessing Athena and Glue resources"
  
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "athena:StartQueryExecution",
          "athena:GetQueryExecution",
          "athena:GetQueryResults",
          "athena:StopQueryExecution",
          "athena:GetWorkGroup"
        ]
        Resource = [
          aws_athena_workgroup.analytics_workgroup.arn,
          "arn:aws:athena:${var.aws_region}:*:workgroup/${aws_athena_workgroup.analytics_workgroup.name}"
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "glue:GetDatabase",
          "glue:GetTable",
          "glue:GetPartitions"
        ]
        Resource = [
          "arn:aws:glue:${var.aws_region}:*:catalog",
          "arn:aws:glue:${var.aws_region}:*:database/${aws_glue_catalog_database.analytics_db.name}",
          "arn:aws:glue:${var.aws_region}:*:table/${aws_glue_catalog_database.analytics_db.name}/*"
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:ListBucket"
        ]
        Resource = [
          "arn:aws:s3:::${local.s3_bucket_only}",
          "arn:aws:s3:::${local.s3_data_path}/*"
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:ListBucket"
        ]
        Resource = [
          aws_s3_bucket.athena_results.arn,
          "${aws_s3_bucket.athena_results.arn}/*"
        ]
      }
    ]
  })
  
  #tags = var.tags
}

# IAM Role for application (recommended for production - use with ECS, EC2, or Lambda)
# This role can be assumed by ECS tasks, EC2 instances, or Lambda functions
resource "aws_iam_role" "app_role" {
  name = "${var.project_name}-app-role"
  
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = [
            "ecs-tasks.amazonaws.com",
            "ec2.amazonaws.com",
            "lambda.amazonaws.com"
          ]
        }
      }
    ]
  })
  
  #tags = var.tags
}

resource "aws_iam_role_policy_attachment" "app_role_athena_access" {
  role       = aws_iam_role.app_role.name
  policy_arn = aws_iam_policy.athena_access_policy.arn
}

# IAM Instance Profile for EC2 (if running on EC2)
resource "aws_iam_instance_profile" "app_instance_profile" {
  name = "${var.project_name}-app-instance-profile"
  role = aws_iam_role.app_role.name
}

# DEPRECATED: IAM User for application
# WARNING: Using IAM users with long-term credentials is not recommended for production.
# Use IAM roles (above) with ECS, EC2 instance profiles, or Lambda instead.
# This resource is kept for local development/testing only.
resource "aws_iam_user" "app_user" {
  name = "${var.project_name}-app-user"
  
  #tags = var.tags
}

resource "aws_iam_user_policy_attachment" "app_user_athena_access" {
  user       = aws_iam_user.app_user.name
  policy_arn = aws_iam_policy.athena_access_policy.arn
}


# ─── CUR v2 Integration (optional) ───────────────────────────────────────────

# Glue Database for CUR data
resource "aws_glue_catalog_database" "cur_db" {
  count       = local.cur_enabled ? 1 : 0
  name        = var.cur_glue_database_name
  description = "Database for AWS CUR v2 data (Kiro cost analysis)"
}

# IAM policy for CUR S3 access
resource "aws_iam_role_policy" "glue_cur_s3_policy" {
  count = local.cur_enabled ? 1 : 0
  name  = "${var.project_name}-glue-cur-s3-policy"
  role  = aws_iam_role.glue_crawler_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:ListBucket"
        ]
        Resource = [
          "arn:aws:s3:::${local.cur_bucket_only}",
          "arn:aws:s3:::${var.cur_s3_bucket_name}/*"
        ]
      }
    ]
  })
}

# Glue Crawler for CUR data
resource "aws_glue_crawler" "cur_crawler" {
  count         = local.cur_enabled ? 1 : 0
  name          = "${var.project_name}-cur-crawler"
  role          = aws_iam_role.glue_crawler_role.arn
  database_name = aws_glue_catalog_database.cur_db[0].name

  s3_target {
    path = "s3://${var.cur_s3_bucket_name}/"
  }

  schema_change_policy {
    delete_behavior = "LOG"
    update_behavior = "UPDATE_IN_DATABASE"
  }

  configuration = jsonencode({
    Version = 1.0
    CrawlerOutput = {
      Partitions = { AddOrUpdateBehavior = "InheritFromTable" }
    }
  })

  schedule = var.glue_crawler_schedule
}

# Extend Athena access policy for CUR database
resource "aws_iam_policy" "cur_athena_access_policy" {
  count       = local.cur_enabled ? 1 : 0
  name        = "${var.project_name}-cur-athena-access"
  description = "Policy for accessing CUR data via Athena"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "glue:GetDatabase",
          "glue:GetTable",
          "glue:GetTables",
          "glue:GetPartitions"
        ]
        Resource = [
          "arn:aws:glue:${var.aws_region}:*:catalog",
          "arn:aws:glue:${var.aws_region}:*:database/${var.cur_glue_database_name}",
          "arn:aws:glue:${var.aws_region}:*:table/${var.cur_glue_database_name}/*"
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:ListBucket"
        ]
        Resource = [
          "arn:aws:s3:::${local.cur_bucket_only}",
          "arn:aws:s3:::${var.cur_s3_bucket_name}/*"
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "app_role_cur_access" {
  count      = local.cur_enabled ? 1 : 0
  role       = aws_iam_role.app_role.name
  policy_arn = aws_iam_policy.cur_athena_access_policy[0].arn
}

resource "aws_iam_user_policy_attachment" "app_user_cur_access" {
  count      = local.cur_enabled ? 1 : 0
  user       = aws_iam_user.app_user.name
  policy_arn = aws_iam_policy.cur_athena_access_policy[0].arn
}
