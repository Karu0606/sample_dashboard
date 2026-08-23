"""
Agent 2: Solution Architect
────────────────────────────
Triggered after Agent 1 triages an issue.

Responsibilities:
1. Pick up issues labelled 'infra-automation/triaged' but NOT 'infra-automation/in-progress'
2. Search existing closed issues for similar past solutions (knowledge reuse)
3. Run AWS infrastructure feasibility checks (VPC, IAM, subnets, quotas)
4. Generate Terraform code based on the request
5. Create a branch (infra/issue-<id>), push code, open a PR
6. Comment the solution + feasibility report on the issue
7. Request review from configured approver
8. Log everything to CloudWatch
"""

import sys
import os
import re
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from shared.config_loader import load_config, get_agent_config, get_security_config
from shared.github_client import GitHubClient, GitHubClientError
from shared.aws_client import AWSClient, AWSClientError, InfraFeasibilityChecker
from shared.observability import (
    AgentLogger,
    ExecutionTimer,
    STATUS_OK,
    STATUS_ERROR,
    STATUS_SKIP,
    STATUS_ESCALATION,
)

AGENT_NAME = "solution-architect"


def run():
    """Main execution entry point for the Solution Architect agent."""
    config = load_config()
    agent_config = get_agent_config(config, "solution_architect")
    security_config = get_security_config(config)

    if not agent_config.get("enabled", True):
        return

    logger = AgentLogger(AGENT_NAME, config)

    with ExecutionTimer() as timer:
        try:
            gh = GitHubClient(config)
            aws = AWSClient(config, session_name="accinfra-solution-architect")
            feasibility_checker = InfraFeasibilityChecker(aws)

            # Find triaged issues that haven't been picked up yet
            triaged_issues = _get_triaged_unprocessed_issues(gh)

            if not triaged_issues:
                logger.log(
                    STATUS_SKIP,
                    "No triaged issues awaiting processing",
                    duration_seconds=timer.elapsed,
                )
                return

            for issue in triaged_issues:
                _process_issue(
                    issue=issue,
                    gh=gh,
                    feasibility_checker=feasibility_checker,
                    agent_config=agent_config,
                    security_config=security_config,
                    config=config,
                    logger=logger,
                    timer=timer,
                )

        except GitHubClientError as e:
            logger.log(
                STATUS_ERROR,
                "GitHub API error",
                error_summary=str(e),
                duration_seconds=timer.elapsed,
            )
        except AWSClientError as e:
            logger.log(
                STATUS_ERROR,
                "AWS access error",
                error_summary=str(e),
                duration_seconds=timer.elapsed,
            )
        except Exception as e:
            logger.log(
                STATUS_ERROR,
                f"Unexpected error: {type(e).__name__}",
                error_summary=str(e),
                duration_seconds=timer.elapsed,
            )


def _get_triaged_unprocessed_issues(gh: GitHubClient) -> list[dict]:
    """Get issues that are triaged but not yet in-progress."""
    triaged_label = gh.labels.get("triaged", "infra-automation/triaged")
    in_progress_label = gh.labels.get("in_progress", "infra-automation/in-progress")

    all_issues = gh.list_open_issues(label_filter=triaged_label)

    unprocessed = []
    for issue in all_issues:
        if "pull_request" in issue:
            continue
        issue_labels = [lbl["name"] for lbl in issue.get("labels", [])]
        if in_progress_label not in issue_labels:
            unprocessed.append(issue)

    return unprocessed


def _process_issue(
    issue: dict,
    gh: GitHubClient,
    feasibility_checker: InfraFeasibilityChecker,
    agent_config: dict,
    security_config: dict,
    config: dict,
    logger: AgentLogger,
    timer: ExecutionTimer,
):
    """Process a single triaged issue end-to-end."""
    issue_number = issue["number"]
    issue_title = issue["title"]
    issue_body = issue.get("body", "") or ""
    issue_id = f"#{issue_number}"

    try:
        # Mark as in-progress
        in_progress_label = gh.labels.get("in_progress", "infra-automation/in-progress")
        gh.add_label(issue_number, in_progress_label)

        # ─── Step 1: Search existing issues for precedent ──────────────
        similar_solutions = _search_existing_solutions(gh, issue_title, issue_body, agent_config)

        # ─── Step 2: Run infrastructure feasibility checks ─────────────
        infra_checks = agent_config.get("infra_checks", [])
        feasibility_report = feasibility_checker.get_feasibility_report(infra_checks)

        # Check if any check is blocking
        check_results = feasibility_checker.check_all(infra_checks)
        blocking_checks = [
            name for name, result in check_results.items()
            if result["feasible"] is False
        ]

        if blocking_checks:
            # Post feasibility failure and stop
            comment = (
                "🤖 **AccInfra Agent — Infrastructure Check Failed**\n\n"
                f"{feasibility_report}\n\n"
                "**Action Required:** The following limits are blocking this request:\n"
                f"- {', '.join(blocking_checks)}\n\n"
                "Please resolve the capacity issues or adjust the request, then re-open this issue."
            )
            gh.add_comment(issue_number, comment)
            logger.log(
                STATUS_ESCALATION,
                f"Issue {issue_id} blocked by infra limits: {', '.join(blocking_checks)}",
                issue_id=issue_id,
                error_summary=f"Blocked: {', '.join(blocking_checks)}",
                duration_seconds=timer.elapsed,
            )
            return

        # ─── Step 3: Generate Terraform code ───────────────────────────
        terraform_code = _generate_terraform(
            issue_title=issue_title,
            issue_body=issue_body,
            similar_solutions=similar_solutions,
            security_config=security_config,
            config=config,
        )

        # ─── Step 4: Create branch and push code ──────────────────────
        branch_name = f"{gh.branch_prefix}{issue_number}"
        default_branch = gh.get_default_branch()
        base_sha = gh.get_branch_sha(default_branch)

        # Create branch
        gh.create_branch(branch_name, base_sha)

        # Push Terraform file
        tf_file_path = f"infrastructure/issue-{issue_number}.tf"
        gh.create_or_update_file(
            branch=branch_name,
            file_path=tf_file_path,
            content=terraform_code,
            commit_message=f"feat(infra): Add infrastructure for issue #{issue_number}\n\n{issue_title}",
        )

        # ─── Step 5: Open PR ──────────────────────────────────────────
        pr_body = _build_pr_body(
            issue_number=issue_number,
            issue_title=issue_title,
            feasibility_report=feasibility_report,
            similar_solutions=similar_solutions,
            terraform_code=terraform_code,
        )

        pr = gh.create_pull_request(
            title=f"infra: {issue_title} (#{issue_number})",
            body=pr_body,
            head_branch=branch_name,
            base_branch=default_branch,
            reviewers=[gh.approver] if gh.approver else None,
        )

        pr_number = pr.get("number")
        pr_url = pr.get("html_url", "")

        # ─── Step 6: Comment solution on issue ────────────────────────
        # Update label
        awaiting_label = gh.labels.get("awaiting_review", "infra-automation/awaiting-review")
        gh.add_label(issue_number, awaiting_label)

        solution_comment = _build_solution_comment(
            feasibility_report=feasibility_report,
            similar_solutions=similar_solutions,
            terraform_code=terraform_code,
            pr_url=pr_url,
            pr_number=pr_number,
        )
        gh.add_comment(issue_number, solution_comment)

        logger.log(
            STATUS_OK,
            f"Solution proposed for {issue_id}, PR #{pr_number} opened",
            issue_id=issue_id,
            pr_number=pr_number,
            duration_seconds=timer.elapsed,
            metadata={
                "branch": branch_name,
                "similar_issues_found": len(similar_solutions),
                "blocking_checks": 0,
            },
        )

    except GitHubClientError as e:
        logger.log(
            STATUS_ERROR,
            f"Failed to process issue {issue_id}",
            issue_id=issue_id,
            error_summary=str(e),
            duration_seconds=timer.elapsed,
        )
    except AWSClientError as e:
        logger.log(
            STATUS_ERROR,
            f"AWS error processing issue {issue_id}",
            issue_id=issue_id,
            error_summary=str(e),
            duration_seconds=timer.elapsed,
        )


def _search_existing_solutions(
    gh: GitHubClient,
    issue_title: str,
    issue_body: str,
    agent_config: dict,
) -> list[dict]:
    """Search closed issues for similar past solutions.

    Extracts keywords from the title (removing the accinfra: prefix)
    and searches for matching closed issues.

    Returns:
        List of relevant closed issues with titles and bodies.
    """
    max_results = agent_config.get("max_similar_issues_to_search", 20)

    # Extract meaningful keywords from title
    clean_title = re.sub(r"^accinfra:\s*", "", issue_title, flags=re.IGNORECASE)
    # Extract infra-related keywords
    keywords = _extract_keywords(clean_title + " " + issue_body[:200])

    if not keywords:
        return []

    search_query = " ".join(keywords[:5])  # Limit to top 5 keywords
    similar = gh.search_closed_issues(search_query, max_results=max_results)

    # Return simplified results
    return [
        {
            "number": item["number"],
            "title": item["title"],
            "body": (item.get("body") or "")[:500],
            "url": item.get("html_url", ""),
        }
        for item in similar[:10]  # Cap at 10 most relevant
    ]


def _extract_keywords(text: str) -> list[str]:
    """Extract infrastructure-relevant keywords from text."""
    # Common infra terms to look for
    infra_terms = {
        "vpc", "subnet", "security group", "iam", "role", "policy",
        "ec2", "ecs", "eks", "rds", "s3", "lambda", "api gateway",
        "load balancer", "alb", "nlb", "nat gateway", "internet gateway",
        "route table", "elastic ip", "eip", "cloudfront", "dynamodb",
        "sqs", "sns", "ecr", "fargate", "auto scaling", "target group",
    }

    text_lower = text.lower()
    found_terms = []

    for term in infra_terms:
        if term in text_lower:
            found_terms.append(term)

    # Also extract generic words (nouns likely to be resource names)
    words = re.findall(r"\b[a-z]{3,}\b", text_lower)
    # Remove common stop words
    stop_words = {"the", "and", "for", "that", "this", "with", "from", "are", "was", "will", "can", "new", "create", "need", "please", "should"}
    meaningful = [w for w in words if w not in stop_words and len(w) > 3]

    return found_terms + meaningful[:5]


def _generate_terraform(
    issue_title: str,
    issue_body: str,
    similar_solutions: list[dict],
    security_config: dict,
    config: dict,
) -> str:
    """Generate Terraform code based on the issue request.

    This is the core IaC generation logic. It:
    1. Parses the request to identify required resources
    2. Checks against allowed/blocked resource lists
    3. Generates compliant Terraform code with proper tagging

    NOTE: In production, this would integrate with an LLM for complex
    generation. For now, it uses template-based generation for common
    patterns.
    """
    clean_title = re.sub(r"^accinfra:\s*", "", issue_title, flags=re.IGNORECASE)
    full_text = f"{clean_title} {issue_body}".lower()

    allowed_resources = security_config.get("allowed_terraform_resources", [])
    blocked_resources = security_config.get("blocked_terraform_resources", [])
    default_tags = config.get("terraform", {}).get("default_tags", {})

    # Build tags block
    tags_block = _build_tags_block(default_tags, clean_title)

    # Detect what type of infrastructure is being requested
    resources = _detect_requested_resources(full_text)

    # Filter out blocked resources
    resources = [r for r in resources if r not in blocked_resources]

    # Filter to only allowed resources
    if allowed_resources:
        resources = [r for r in resources if r in allowed_resources]

    # Generate Terraform based on detected resources
    tf_sections = [
        _terraform_header(clean_title),
    ]

    if "aws_vpc" in resources:
        tf_sections.append(_terraform_vpc(tags_block))
    if "aws_subnet" in resources:
        tf_sections.append(_terraform_subnets(tags_block))
    if "aws_security_group" in resources:
        tf_sections.append(_terraform_security_group(tags_block))
    if "aws_iam_role" in resources:
        tf_sections.append(_terraform_iam_role(clean_title, tags_block))
    if "aws_ecs_cluster" in resources:
        tf_sections.append(_terraform_ecs_cluster(tags_block))
    if "aws_s3_bucket" in resources:
        tf_sections.append(_terraform_s3_bucket(tags_block))
    if "aws_lb" in resources:
        tf_sections.append(_terraform_alb(tags_block))
    if "aws_cloudwatch_log_group" in resources:
        tf_sections.append(_terraform_log_group(tags_block))
    if "aws_lambda_function" in resources:
        tf_sections.append(_terraform_lambda(clean_title, tags_block))

    # If no specific resources detected, generate a placeholder
    if len(tf_sections) == 1:
        tf_sections.append(_terraform_placeholder(clean_title, tags_block))

    # Add reference to similar solutions as comments
    if similar_solutions:
        ref_comment = "\n# ─── Referenced from similar past issues ───\n"
        for sol in similar_solutions[:3]:
            ref_comment += f"# - Issue #{sol['number']}: {sol['title']}\n"
        tf_sections.insert(1, ref_comment)

    return "\n".join(tf_sections)


def _detect_requested_resources(text: str) -> list[str]:
    """Detect which AWS resources are being requested based on text analysis."""
    resource_keywords = {
        "aws_vpc": ["vpc", "virtual private cloud", "network", "networking"],
        "aws_subnet": ["subnet", "subnets", "availability zone"],
        "aws_security_group": ["security group", "firewall", "ingress", "egress", "port"],
        "aws_iam_role": ["iam role", "role", "permissions", "access"],
        "aws_iam_policy": ["iam policy", "policy", "permissions"],
        "aws_ecs_cluster": ["ecs", "container", "fargate", "docker"],
        "aws_ecs_service": ["ecs service", "service", "container service"],
        "aws_s3_bucket": ["s3", "bucket", "storage", "object storage"],
        "aws_lb": ["load balancer", "alb", "nlb", "elb"],
        "aws_nat_gateway": ["nat", "nat gateway"],
        "aws_internet_gateway": ["internet gateway", "igw"],
        "aws_ecr_repository": ["ecr", "container registry", "docker registry"],
        "aws_dynamodb_table": ["dynamodb", "dynamo", "nosql"],
        "aws_cloudwatch_log_group": ["cloudwatch", "logging", "logs", "log group"],
        "aws_lambda_function": ["lambda", "function", "serverless"],
    }

    detected = []
    for resource, keywords in resource_keywords.items():
        for keyword in keywords:
            if keyword in text:
                detected.append(resource)
                break

    return list(set(detected))


def _build_tags_block(default_tags: dict, request_name: str) -> str:
    """Build a Terraform tags block."""
    tags = dict(default_tags)
    tags["Name"] = _sanitize_name(request_name)
    lines = ["  tags = {"]
    for key, value in tags.items():
        lines.append(f'    {key} = "{value}"')
    lines.append("  }")
    return "\n".join(lines)


def _sanitize_name(name: str) -> str:
    """Sanitize a name for use in AWS resource names."""
    clean = re.sub(r"[^a-zA-Z0-9-]", "-", name.lower())
    clean = re.sub(r"-+", "-", clean).strip("-")
    return clean[:63]


def _terraform_header(title: str) -> str:
    return textwrap.dedent(f"""\
        # ──────────────────────────────────────────────────────────────────
        # AccInfra Auto-Generated Terraform
        # Request: {title}
        # Generated by: Solution Architect Agent
        # ──────────────────────────────────────────────────────────────────

        terraform {{
          required_providers {{
            aws = {{
              source  = "hashicorp/aws"
              version = ">= 5.0"
            }}
            archive = {{
              source  = "hashicorp/archive"
              version = ">= 2.0"
            }}
          }}
        }}
    """)


def _terraform_vpc(tags_block: str) -> str:
    return textwrap.dedent(f"""\

        # ─── VPC ──────────────────────────────────────────────────────────

        resource "aws_vpc" "main" {{
          cidr_block           = "10.0.0.0/16"
          enable_dns_hostnames = true
          enable_dns_support   = true

        {tags_block}
        }}

        resource "aws_internet_gateway" "main" {{
          vpc_id = aws_vpc.main.id

        {tags_block}
        }}
    """)


def _terraform_subnets(tags_block: str) -> str:
    return textwrap.dedent(f"""\

        # ─── Subnets ─────────────────────────────────────────────────────

        resource "aws_subnet" "public_a" {{
          vpc_id                  = aws_vpc.main.id
          cidr_block              = "10.0.1.0/24"
          availability_zone       = "${{var.aws_region}}a"
          map_public_ip_on_launch = true

        {tags_block}
        }}

        resource "aws_subnet" "public_b" {{
          vpc_id                  = aws_vpc.main.id
          cidr_block              = "10.0.2.0/24"
          availability_zone       = "${{var.aws_region}}b"
          map_public_ip_on_launch = true

        {tags_block}
        }}

        resource "aws_subnet" "private_a" {{
          vpc_id            = aws_vpc.main.id
          cidr_block        = "10.0.10.0/24"
          availability_zone = "${{var.aws_region}}a"

        {tags_block}
        }}

        resource "aws_subnet" "private_b" {{
          vpc_id            = aws_vpc.main.id
          cidr_block        = "10.0.11.0/24"
          availability_zone = "${{var.aws_region}}b"

        {tags_block}
        }}
    """)


def _terraform_security_group(tags_block: str) -> str:
    return textwrap.dedent(f"""\

        # ─── Security Group ──────────────────────────────────────────────

        resource "aws_security_group" "main" {{
          name_prefix = "accinfra-"
          vpc_id      = aws_vpc.main.id
          description = "Managed by AccInfra agents"

          ingress {{
            from_port   = 443
            to_port     = 443
            protocol    = "tcp"
            cidr_blocks = ["0.0.0.0/0"]
            description = "HTTPS inbound"
          }}

          egress {{
            from_port   = 0
            to_port     = 0
            protocol    = "-1"
            cidr_blocks = ["0.0.0.0/0"]
            description = "All outbound"
          }}

        {tags_block}
        }}
    """)


def _terraform_iam_role(title: str, tags_block: str) -> str:
    role_name = _sanitize_name(title)[:32]
    return textwrap.dedent(f"""\

        # ─── IAM Role ────────────────────────────────────────────────────

        resource "aws_iam_role" "main" {{
          name = "{role_name}-role"

          assume_role_policy = jsonencode({{
            Version = "2012-10-17"
            Statement = [
              {{
                Action = "sts:AssumeRole"
                Effect = "Allow"
                Principal = {{
                  Service = "ecs-tasks.amazonaws.com"
                }}
              }}
            ]
          }})

        {tags_block}
        }}

        resource "aws_iam_role_policy_attachment" "main_execution" {{
          role       = aws_iam_role.main.name
          policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
        }}
    """)


def _terraform_ecs_cluster(tags_block: str) -> str:
    return textwrap.dedent(f"""\

        # ─── ECS Cluster ─────────────────────────────────────────────────

        resource "aws_ecs_cluster" "main" {{
          name = "accinfra-cluster"

          setting {{
            name  = "containerInsights"
            value = "enabled"
          }}

        {tags_block}
        }}
    """)


def _terraform_s3_bucket(tags_block: str) -> str:
    return textwrap.dedent(f"""\

        # ─── S3 Bucket ───────────────────────────────────────────────────

        resource "aws_s3_bucket" "main" {{
          bucket_prefix = "accinfra-"

        {tags_block}
        }}

        resource "aws_s3_bucket_versioning" "main" {{
          bucket = aws_s3_bucket.main.id
          versioning_configuration {{
            status = "Enabled"
          }}
        }}

        resource "aws_s3_bucket_server_side_encryption_configuration" "main" {{
          bucket = aws_s3_bucket.main.id
          rule {{
            apply_server_side_encryption_by_default {{
              sse_algorithm = "AES256"
            }}
          }}
        }}

        resource "aws_s3_bucket_public_access_block" "main" {{
          bucket = aws_s3_bucket.main.id

          block_public_acls       = true
          block_public_policy     = true
          ignore_public_acls      = true
          restrict_public_buckets = true
        }}
    """)


def _terraform_alb(tags_block: str) -> str:
    return textwrap.dedent(f"""\

        # ─── Application Load Balancer ───────────────────────────────────

        resource "aws_lb" "main" {{
          name               = "accinfra-alb"
          internal           = false
          load_balancer_type = "application"
          security_groups    = [aws_security_group.main.id]
          subnets            = [aws_subnet.public_a.id, aws_subnet.public_b.id]

        {tags_block}
        }}

        resource "aws_lb_target_group" "main" {{
          name     = "accinfra-tg"
          port     = 80
          protocol = "HTTP"
          vpc_id   = aws_vpc.main.id

          health_check {{
            path                = "/health"
            healthy_threshold   = 2
            unhealthy_threshold = 3
            timeout             = 5
            interval            = 30
          }}

        {tags_block}
        }}
    """)


def _terraform_log_group(tags_block: str) -> str:
    return textwrap.dedent(f"""\

        # ─── CloudWatch Log Group ────────────────────────────────────────

        resource "aws_cloudwatch_log_group" "main" {{
          name              = "/accinfra/demo-app"
          retention_in_days = 14

        {tags_block}
        }}
    """)


def _terraform_lambda(title: str, tags_block: str) -> str:
    fn_name = _sanitize_name(title)[:40]
    return textwrap.dedent(f"""\

        # ─── Lambda Function ─────────────────────────────────────────────

        data "archive_file" "lambda_zip" {{
          type        = "zip"
          output_path = "${{path.module}}/lambda_{fn_name}.zip"
          source {{
            content  = "def handler(event, context):\\n    return {{'statusCode': 200, 'body': 'accinfra demo'}}\\n"
            filename = "index.py"
          }}
        }}

        resource "aws_iam_role" "lambda_exec" {{
          name = "{fn_name}-lambda-role"

          assume_role_policy = jsonencode({{
            Version = "2012-10-17"
            Statement = [{{
              Action    = "sts:AssumeRole"
              Effect    = "Allow"
              Principal = {{ Service = "lambda.amazonaws.com" }}
            }}]
          }})

        {tags_block}
        }}

        resource "aws_iam_role_policy_attachment" "lambda_logs" {{
          role       = aws_iam_role.lambda_exec.name
          policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
        }}

        resource "aws_lambda_function" "main" {{
          function_name    = "{fn_name}"
          role             = aws_iam_role.lambda_exec.arn
          handler          = "index.handler"
          runtime          = "python3.12"
          filename         = data.archive_file.lambda_zip.output_path
          source_code_hash = data.archive_file.lambda_zip.output_base64sha256

        {tags_block}
        }}
    """)


def _terraform_placeholder(title: str, tags_block: str) -> str:
    return textwrap.dedent(f"""\

        # ─── Placeholder ─────────────────────────────────────────────────
        # The request "{title}" did not match a known infrastructure pattern.
        # This placeholder was generated for manual review and completion.
        # Please specify the required resources more clearly in the issue.

        # Example: Add specific resources below
        # resource "aws_<type>" "example" {{
        #   ...
        # {tags_block}
        # }}
    """)


def _build_pr_body(
    issue_number: int,
    issue_title: str,
    feasibility_report: str,
    similar_solutions: list[dict],
    terraform_code: str,
) -> str:
    """Build the PR description."""
    body = f"""## Infrastructure Change for Issue #{issue_number}

**Request:** {issue_title}

Closes #{issue_number}

---

{feasibility_report}

---

## Similar Past Issues Referenced
"""
    if similar_solutions:
        for sol in similar_solutions[:5]:
            body += f"- #{sol['number']}: {sol['title']}\n"
    else:
        body += "No similar past issues found.\n"

    body += f"""
---

## Terraform Code

```hcl
{terraform_code[:2000]}
```

---

## Checklist
- [x] Infrastructure feasibility checked
- [x] Existing solutions reviewed
- [x] Terraform code generated
- [x] Security allowlist validated
- [ ] Human review approved
- [ ] Terraform plan verified
- [ ] Deployed successfully

---

*Generated by AccInfra Solution Architect Agent*
"""
    return body


def _build_solution_comment(
    feasibility_report: str,
    similar_solutions: list[dict],
    terraform_code: str,
    pr_url: str,
    pr_number: int,
) -> str:
    """Build the issue comment with the proposed solution."""
    comment = f"""🤖 **AccInfra Agent — Solution Proposed**

---

{feasibility_report}

---

### Similar Past Issues
"""
    if similar_solutions:
        for sol in similar_solutions[:5]:
            comment += f"- [#{sol['number']}: {sol['title']}]({sol['url']})\n"
    else:
        comment += "No similar past issues found. This appears to be a new pattern.\n"

    comment += f"""
---

### Generated Terraform Code

```hcl
{terraform_code[:3000]}
```

---

### Pull Request

A PR has been opened for review: [PR #{pr_number}]({pr_url})

**Next steps:**
1. The configured approver will review the Terraform code
2. Once approved and merged, GitHub Actions will run `terraform apply`
3. After successful deployment, this issue will be automatically closed with resource details

---
*If the proposed solution needs adjustment, please comment on the PR with your feedback.*
"""
    return comment


if __name__ == "__main__":
    run()
