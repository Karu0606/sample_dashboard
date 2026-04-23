variable "aws_region" {
  description = "AWS region for resources"
  type        = string
  default     = "us-east-1"
}

variable "aws_account_id" {
  description = "AWS account ID used in the S3 data path"
  type        = string
}

variable "project_name" {
  description = "Project name for resource naming"
  type        = string
  default     = "qdeveloper-analytics"
}

variable "s3_bucket_name" {
  description = "S3 bucket name including prefix (e.g. kiro-dev/user)"
  type        = string
}

variable "glue_database_name" {
  description = "Glue database name"
  type        = string
  default     = "qdeveloper_analytics"
}

variable "glue_crawler_schedule" {
  description = "Cron expression for Glue crawler schedule"
  type        = string
  default     = "cron(0 2 * * ? *)"  # Daily at 2 AM UTC
}

variable "identity_store_id" {
  description = "AWS IAM Identity Center (SSO) Identity Store ID for username lookup (e.g. d-1234567890)"
  type        = string
}

variable "cur_s3_bucket_name" {
  description = "S3 bucket name for CUR v2 data (e.g. my-cur-bucket/cur-prefix). Leave empty to skip CUR integration."
  type        = string
  default     = ""
}

variable "cur_glue_database_name" {
  description = "Glue database name for CUR data"
  type        = string
  default     = "kiro_cur_analytics"
}

variable "tags" {
  description = "Tags to apply to resources"
  type        = map(string)
  default = {
    Project     = "QDeveloper Analytics"
    Environment = "Production"
    ManagedBy   = "Terraform"
  }
}
