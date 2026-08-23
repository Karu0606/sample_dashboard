"""
AccInfra AWS Client
───────────────────
Handles AWS STS role assumption and provides service clients.
All agents use this for secure, least-privilege AWS access.
"""

from typing import Any

import boto3
from botocore.exceptions import ClientError


class AWSClientError(Exception):
    """Raised on AWS operation failures."""
    pass


class AWSClient:
    """AWS client with STS role assumption for accinfra agents.

    Each agent assumes the configured role (from accinfra-config.json) and
    gets temporary credentials. No long-lived keys stored anywhere.
    """

    def __init__(self, config: dict[str, Any], session_name: str = "accinfra-agent"):
        """
        Args:
            config: Full accinfra config dict.
            session_name: STS session name for audit trail.
        """
        self.aws_config = config["aws"]
        self.region = self.aws_config["region"]
        self.role_arn = self.aws_config["agent_role_arn"]
        self.session_name = session_name

        self._credentials = self._assume_role()

    def _assume_role(self) -> dict[str, str]:
        """Assume the agent IAM role via STS.

        Returns:
            Dict with AccessKeyId, SecretAccessKey, SessionToken.
        """
        # If role ARN has placeholder, use default credentials (local dev)
        if "ACCOUNT_ID" in self.role_arn:
            return {}

        sts = boto3.client("sts", region_name=self.region)
        try:
            response = sts.assume_role(
                RoleArn=self.role_arn,
                RoleSessionName=self.session_name,
                DurationSeconds=3600,
            )
            return {
                "aws_access_key_id": response["Credentials"]["AccessKeyId"],
                "aws_secret_access_key": response["Credentials"]["SecretAccessKey"],
                "aws_session_token": response["Credentials"]["SessionToken"],
            }
        except ClientError as e:
            raise AWSClientError(f"Failed to assume role {self.role_arn}: {e}") from e

    def get_client(self, service_name: str):
        """Get a boto3 client for the specified AWS service.

        Args:
            service_name: AWS service name (e.g., 'ec2', 'iam', 'service-quotas').

        Returns:
            boto3 client with assumed-role credentials.
        """
        if self._credentials:
            return boto3.client(
                service_name,
                region_name=self.region,
                **self._credentials,
            )
        return boto3.client(service_name, region_name=self.region)

    def get_resource(self, service_name: str):
        """Get a boto3 resource for the specified AWS service."""
        if self._credentials:
            return boto3.resource(
                service_name,
                region_name=self.region,
                **self._credentials,
            )
        return boto3.resource(service_name, region_name=self.region)


# ─── Infrastructure Feasibility Checker ─────────────────────────────────────

class InfraFeasibilityChecker:
    """Checks AWS service quotas and current usage to determine
    whether a requested infrastructure change is feasible.

    This prevents the agent from proposing solutions that would
    fail at terraform apply due to account limits.
    """

    def __init__(self, aws_client: AWSClient):
        self.aws = aws_client

    def check_all(self, checks: list[str]) -> dict[str, dict[str, Any]]:
        """Run all configured feasibility checks.

        Args:
            checks: List of check names from config (e.g., ['vpc_limit', 'iam_roles_limit']).

        Returns:
            Dict mapping check_name -> {feasible: bool, current: int, limit: int, message: str}
        """
        check_methods = {
            "vpc_limit": self._check_vpc_limit,
            "iam_roles_limit": self._check_iam_roles_limit,
            "iam_users_limit": self._check_iam_users_limit,
            "subnet_availability": self._check_subnet_availability,
            "elastic_ip_limit": self._check_elastic_ip_limit,
            "security_group_limit": self._check_security_group_limit,
            "nat_gateway_limit": self._check_nat_gateway_limit,
        }

        results = {}
        for check_name in checks:
            method = check_methods.get(check_name)
            if method:
                try:
                    results[check_name] = method()
                except (ClientError, Exception) as e:
                    results[check_name] = {
                        "feasible": None,
                        "current": None,
                        "limit": None,
                        "message": f"Check failed: {str(e)}",
                    }
            else:
                results[check_name] = {
                    "feasible": None,
                    "current": None,
                    "limit": None,
                    "message": f"Unknown check: {check_name}",
                }

        return results

    def get_feasibility_report(self, checks: list[str]) -> str:
        """Generate a human-readable feasibility report.

        Args:
            checks: List of check names.

        Returns:
            Markdown-formatted report string.
        """
        results = self.check_all(checks)
        lines = ["## Infrastructure Feasibility Report", ""]
        lines.append("| Check | Status | Current | Limit | Notes |")
        lines.append("|-------|--------|---------|-------|-------|")

        all_feasible = True
        for name, result in results.items():
            if result["feasible"] is True:
                status = "✅ OK"
            elif result["feasible"] is False:
                status = "❌ BLOCKED"
                all_feasible = False
            else:
                status = "⚠️ UNKNOWN"

            current = result["current"] if result["current"] is not None else "—"
            limit = result["limit"] if result["limit"] is not None else "—"
            lines.append(f"| {name} | {status} | {current} | {limit} | {result['message']} |")

        lines.append("")
        if all_feasible:
            lines.append("**Verdict: ✅ All checks passed. Infrastructure can be provisioned.**")
        else:
            lines.append("**Verdict: ❌ One or more checks failed. Review limits before proceeding.**")

        return "\n".join(lines)

    def _check_vpc_limit(self) -> dict[str, Any]:
        """Check VPC count against account limit."""
        ec2 = self.aws.get_client("ec2")
        vpcs = ec2.describe_vpcs()
        current = len(vpcs.get("Vpcs", []))

        # Get quota from Service Quotas
        limit = self._get_service_quota("vpc", "L-F678F1CE")  # VPCs per Region
        if limit is None:
            limit = 5  # Default AWS limit

        return {
            "feasible": current < limit,
            "current": current,
            "limit": limit,
            "message": f"{limit - current} VPCs available" if current < limit else "VPC limit reached",
        }

    def _check_iam_roles_limit(self) -> dict[str, Any]:
        """Check IAM roles count against limit."""
        iam = self.aws.get_client("iam")
        # Use account summary for quick counts
        summary = iam.get_account_summary()
        current = summary["SummaryMap"].get("Roles", 0)
        limit = summary["SummaryMap"].get("RolesQuota", 1000)

        return {
            "feasible": current < (limit * 0.9),  # 90% threshold
            "current": current,
            "limit": limit,
            "message": f"{limit - current} roles available" if current < limit else "Approaching role limit",
        }

    def _check_iam_users_limit(self) -> dict[str, Any]:
        """Check IAM users count against limit."""
        iam = self.aws.get_client("iam")
        summary = iam.get_account_summary()
        current = summary["SummaryMap"].get("Users", 0)
        limit = summary["SummaryMap"].get("UsersQuota", 5000)

        return {
            "feasible": current < (limit * 0.9),
            "current": current,
            "limit": limit,
            "message": f"{limit - current} users available",
        }

    def _check_subnet_availability(self) -> dict[str, Any]:
        """Check subnet count per VPC."""
        ec2 = self.aws.get_client("ec2")
        subnets = ec2.describe_subnets()
        current = len(subnets.get("Subnets", []))

        # Default limit: 200 subnets per VPC
        limit = 200

        return {
            "feasible": current < limit,
            "current": current,
            "limit": limit,
            "message": f"{limit - current} subnets available",
        }

    def _check_elastic_ip_limit(self) -> dict[str, Any]:
        """Check Elastic IP allocation."""
        ec2 = self.aws.get_client("ec2")
        eips = ec2.describe_addresses()
        current = len(eips.get("Addresses", []))

        limit = self._get_service_quota("ec2", "L-0263D0A3")  # EIPs per Region
        if limit is None:
            limit = 5

        return {
            "feasible": current < limit,
            "current": current,
            "limit": limit,
            "message": f"{limit - current} EIPs available" if current < limit else "EIP limit reached",
        }

    def _check_security_group_limit(self) -> dict[str, Any]:
        """Check security group count per VPC."""
        ec2 = self.aws.get_client("ec2")
        sgs = ec2.describe_security_groups()
        current = len(sgs.get("SecurityGroups", []))

        limit = self._get_service_quota("vpc", "L-E79EC296")  # SGs per Region
        if limit is None:
            limit = 2500

        return {
            "feasible": current < (limit * 0.9),
            "current": current,
            "limit": limit,
            "message": f"{limit - current} security groups available",
        }

    def _check_nat_gateway_limit(self) -> dict[str, Any]:
        """Check NAT Gateway count."""
        ec2 = self.aws.get_client("ec2")
        nats = ec2.describe_nat_gateways(
            Filter=[{"Name": "state", "Values": ["available", "pending"]}]
        )
        current = len(nats.get("NatGateways", []))

        limit = self._get_service_quota("vpc", "L-FE5A380F")  # NAT GWs per AZ
        if limit is None:
            limit = 5

        return {
            "feasible": current < limit,
            "current": current,
            "limit": limit,
            "message": f"{limit - current} NAT gateways available" if current < limit else "NAT GW limit reached",
        }

    def _get_service_quota(self, service_code: str, quota_code: str) -> int | None:
        """Retrieve a service quota value from AWS Service Quotas.

        Returns None if the API call fails (falls back to default).
        """
        try:
            sq = self.aws.get_client("service-quotas")
            response = sq.get_service_quota(
                ServiceCode=service_code,
                QuotaCode=quota_code,
            )
            return int(response["Quota"]["Value"])
        except (ClientError, KeyError):
            return None
