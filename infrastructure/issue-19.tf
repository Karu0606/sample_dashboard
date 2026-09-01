# infrastructure/issue-19.tf
#
# accinfra: Sample S3 Bucket and Lambda Setup  (Issue #19)
# https://github.com/Karu0606/sample_dashboard/issues/19
#
# Request: a sample S3 bucket + associated Lambda function for dev/testing,
# including IAM permissions, env vars, triggers, and config to test the setup.
#
# Reused pattern: Issue #1 (closed) - "accinfra: Need S3 + Lambda for data pipeline".
# That issue provisioned a two-bucket ingestion->processor pipeline. This issue is
# a scoped-down variant: a single sample bucket + one S3-triggered Lambda for
# development/testing. IAM/S3/Lambda pattern is reused; footprint reduced to 1 bucket.
#
# Managed by accinfra-agents. Deployment is gated on human PR approval + GitHub
# Actions. Never applied by the agent.

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

provider "aws" {
  region = "us-east-1"

  default_tags {
    tags = {
      ManagedBy   = "accinfra-agents"
      Environment = "demo"
      Project     = "accinfra"
    }
  }
}

locals {
  issue_number = 19
  name_prefix  = "accinfra-issue-19"
  account_id   = "359367063226"
}

# ---------------------------------------------------------------------------
# Sample S3 bucket
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "sample" {
  # Bucket name is globally unique; account id keeps it collision-free.
  bucket = "${local.name_prefix}-sample-${local.account_id}"

  tags = {
    Name = "${local.name_prefix}-sample"
  }
}

resource "aws_s3_bucket_versioning" "sample" {
  bucket = aws_s3_bucket.sample.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "sample" {
  bucket = aws_s3_bucket.sample.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "sample" {
  bucket                  = aws_s3_bucket.sample.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ---------------------------------------------------------------------------
# CloudWatch log group for the Lambda (explicit, so retention is controlled)
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/kirocrew/accinfra/${local.name_prefix}-processor"
  retention_in_days = 14

  tags = {
    Name = "${local.name_prefix}-processor-logs"
  }
}

# ---------------------------------------------------------------------------
# IAM role for the Lambda (least privilege)
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${local.name_prefix}-processor-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json

  tags = {
    Name = "${local.name_prefix}-processor-role"
  }
}

# Least-privilege inline policy: write its own logs + read objects from the
# sample bucket only. No wildcard resources beyond the log stream requirement.
data "aws_iam_policy_document" "lambda_policy" {
  statement {
    sid    = "WriteOwnLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.lambda.arn}:*"]
  }

  statement {
    sid    = "ReadSampleBucketObjects"
    effect = "Allow"
    actions = [
      "s3:GetObject",
    ]
    resources = ["${aws_s3_bucket.sample.arn}/*"]
  }

  statement {
    sid       = "ListSampleBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.sample.arn]
  }
}

resource "aws_iam_policy" "lambda" {
  name   = "${local.name_prefix}-processor-policy"
  policy = data.aws_iam_policy_document.lambda_policy.json

  tags = {
    Name = "${local.name_prefix}-processor-policy"
  }
}

resource "aws_iam_role_policy_attachment" "lambda" {
  role       = aws_iam_role.lambda.name
  policy_arn = aws_iam_policy.lambda.arn
}

# ---------------------------------------------------------------------------
# Sample Lambda function (Python 3.12). Handler logs each S3 object that lands
# in the sample bucket - a minimal, verifiable dev/test processor.
# ---------------------------------------------------------------------------
data "archive_file" "lambda_zip" {
  type        = "zip"
  output_path = "${path.module}/build/issue-19-processor.zip"

  source {
    content  = <<-PY
      import json
      import logging

      logger = logging.getLogger()
      logger.setLevel(logging.INFO)


      def handler(event, context):
          """Sample processor: logs each object created in the sample bucket."""
          records = event.get("Records", [])
          for record in records:
              s3 = record.get("s3", {})
              bucket = s3.get("bucket", {}).get("name")
              key = s3.get("object", {}).get("key")
              logger.info("New object: s3://%s/%s", bucket, key)
          return {
              "statusCode": 200,
              "body": json.dumps({"processed": len(records)}),
          }
    PY
    filename = "handler.py"
  }
}

resource "aws_lambda_function" "processor" {
  function_name    = "${local.name_prefix}-processor"
  role             = aws_iam_role.lambda.arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  timeout          = 30
  memory_size      = 128
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  environment {
    variables = {
      SAMPLE_BUCKET = aws_s3_bucket.sample.id
      LOG_LEVEL     = "INFO"
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.lambda.name
  }

  depends_on = [
    aws_iam_role_policy_attachment.lambda,
    aws_cloudwatch_log_group.lambda,
  ]

  tags = {
    Name = "${local.name_prefix}-processor"
  }
}

# Allow S3 to invoke the Lambda.
resource "aws_lambda_permission" "s3_invoke" {
  statement_id  = "AllowS3Invoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.processor.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = aws_s3_bucket.sample.arn
  source_account = local.account_id
}

# S3 -> Lambda trigger on object creation.
resource "aws_s3_bucket_notification" "sample" {
  bucket = aws_s3_bucket.sample.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.processor.arn
    events              = ["s3:ObjectCreated:*"]
  }

  depends_on = [aws_lambda_permission.s3_invoke]
}

# ---------------------------------------------------------------------------
# Outputs (config details for testing the setup)
# ---------------------------------------------------------------------------
output "sample_bucket_name" {
  description = "Name of the sample S3 bucket. Upload an object here to trigger the Lambda."
  value       = aws_s3_bucket.sample.id
}

output "lambda_function_name" {
  description = "Name of the sample Lambda function."
  value       = aws_lambda_function.processor.function_name
}

output "lambda_log_group" {
  description = "CloudWatch log group where the Lambda writes its output."
  value       = aws_cloudwatch_log_group.lambda.name
}
