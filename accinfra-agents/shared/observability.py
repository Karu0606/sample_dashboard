"""
AccInfra Observability Module
─────────────────────────────
Provides structured logging to CloudWatch and conditional notifications
to Slack/Teams. All 4 agents import this single module.

Behavior:
- Always logs to CloudWatch (per-agent log group).
- Sends Slack/Teams notification ONLY when webhook URL is configured.
- If no webhook URL is set, notification is silently skipped.
"""

from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

from shared.config_loader import load_config, get_aws_config, get_notification_config


# ─── Status Constants ───────────────────────────────────────────────────────

STATUS_OK = "OK"
STATUS_ERROR = "ERROR"
STATUS_SKIP = "SKIP"
STATUS_ESCALATION = "ESCALATION"


# ─── CloudWatch Logger ──────────────────────────────────────────────────────

class AgentLogger:
    """Structured logger for accinfra agents.

    Creates a separate CloudWatch Log Group per agent:
        /kirocrew/accinfra/issue-watcher
        /kirocrew/accinfra/solution-architect
        /kirocrew/accinfra/review-handler
        /kirocrew/accinfra/deploy-closer
    """

    def __init__(self, agent_name: str, config: dict[str, Any] | None = None):
        """
        Args:
            agent_name: Identifier for this agent (e.g., 'issue-watcher').
            config: Pre-loaded config dict. If None, loads from default path.
        """
        self.agent_name = agent_name
        self.config = config or load_config()
        self.aws_config = get_aws_config(self.config)
        self.notif_config = get_notification_config(self.config)

        self.log_group = f"{self.aws_config['cloudwatch_log_group_prefix']}/{agent_name}"
        self.log_stream = datetime.now(timezone.utc).strftime("%Y/%m/%d")

        # Initialize CloudWatch client with role assumption
        self._cw_client = self._get_cloudwatch_client()
        self._ensure_log_group()
        self._sequence_token = None

    def _get_cloudwatch_client(self):
        """Get CloudWatch Logs client using STS role assumption."""
        role_arn = self.aws_config["agent_role_arn"]
        region = self.aws_config["region"]

        # If role ARN contains placeholder, fall back to default credentials
        if "ACCOUNT_ID" in role_arn:
            return boto3.client("logs", region_name=region)

        external_id = self.aws_config.get("external_id", "accinfra-agents")
        sts = boto3.client("sts", region_name=region)
        assumed = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName=f"accinfra-{self.agent_name}",
            DurationSeconds=3600,
            ExternalId=external_id,
        )
        creds = assumed["Credentials"]
        return boto3.client(
            "logs",
            region_name=region,
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )

    def _ensure_log_group(self):
        """Create log group and stream if they don't exist."""
        try:
            self._cw_client.create_log_group(logGroupName=self.log_group)
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
                raise

        try:
            self._cw_client.create_log_stream(
                logGroupName=self.log_group,
                logStreamName=self.log_stream,
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
                raise

    def log(
        self,
        status: str,
        message: str,
        issue_id: str | None = None,
        pr_number: int | None = None,
        duration_seconds: float | None = None,
        error_summary: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Write a structured log entry to CloudWatch.

        Args:
            status: One of STATUS_OK, STATUS_ERROR, STATUS_SKIP, STATUS_ESCALATION.
            message: Human-readable log message.
            issue_id: Related GitHub issue number (e.g., '#42').
            pr_number: Related PR number if applicable.
            duration_seconds: Execution time in seconds.
            error_summary: Short error description (for filtering).
            metadata: Additional key-value pairs.

        Returns:
            The log entry dict (for testing/chaining).
        """
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent": self.agent_name,
            "status": status,
            "message": message,
            "issue_id": issue_id,
            "pr_number": pr_number,
            "duration_seconds": duration_seconds,
            "error_summary": error_summary,
            "metadata": metadata or {},
        }

        # Push to CloudWatch
        event = {
            "timestamp": int(time.time() * 1000),
            "message": json.dumps(log_entry, default=str),
        }

        kwargs = {
            "logGroupName": self.log_group,
            "logStreamName": self.log_stream,
            "logEvents": [event],
        }
        if self._sequence_token:
            kwargs["sequenceToken"] = self._sequence_token

        try:
            response = self._cw_client.put_log_events(**kwargs)
            self._sequence_token = response.get("nextSequenceToken")
        except ClientError as e:
            # If invalid sequence token, retry once with fresh token
            if e.response["Error"]["Code"] == "InvalidSequenceTokenException":
                self._sequence_token = e.response["Error"].get("expectedSequenceToken")
                kwargs["sequenceToken"] = self._sequence_token
                response = self._cw_client.put_log_events(**kwargs)
                self._sequence_token = response.get("nextSequenceToken")
            else:
                # Log to stderr as fallback — don't break the agent
                import sys
                print(f"[WARN] CloudWatch log failed: {e}", file=sys.stderr)

        # Conditional notification
        self._maybe_notify(log_entry)

        return log_entry

    def _maybe_notify(self, log_entry: dict[str, Any]) -> None:
        """Send notification if configured and event type matches."""
        notify_on = self.notif_config.get("notify_on", ["error", "escalation"])

        # Map status to notify_on values
        status_map = {
            STATUS_ERROR: "error",
            STATUS_ESCALATION: "escalation",
            STATUS_OK: "success",
        }

        event_type = status_map.get(log_entry["status"])
        if event_type not in notify_on:
            return

        # Send to Slack if configured
        slack_url = self.notif_config.get("slack_webhook_url", "")
        if slack_url:
            self._send_slack(slack_url, log_entry)

        # Send to Teams if configured
        teams_url = self.notif_config.get("teams_webhook_url", "")
        if teams_url:
            self._send_teams(teams_url, log_entry)

    def _send_slack(self, webhook_url: str, log_entry: dict[str, Any]) -> None:
        """Send Slack notification via incoming webhook."""
        status_emoji = {
            STATUS_OK: ":white_check_mark:",
            STATUS_ERROR: ":x:",
            STATUS_SKIP: ":fast_forward:",
            STATUS_ESCALATION: ":warning:",
        }

        emoji = status_emoji.get(log_entry["status"], ":grey_question:")
        issue_text = f"Issue: {log_entry['issue_id']}" if log_entry["issue_id"] else "No issue"

        payload = {
            "text": f"{emoji} *AccInfra Agent Alert*",
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"{emoji} AccInfra Agent Alert",
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Agent:*\n{log_entry['agent']}"},
                        {"type": "mrkdwn", "text": f"*Status:*\n{log_entry['status']}"},
                        {"type": "mrkdwn", "text": f"*{issue_text}*"},
                        {"type": "mrkdwn", "text": f"*Time:*\n{log_entry['timestamp']}"},
                    ],
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Message:* {log_entry['message']}",
                    },
                },
            ],
        }

        if log_entry.get("error_summary"):
            payload["blocks"].append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":rotating_light: *Error:* `{log_entry['error_summary']}`",
                    },
                }
            )

        self._post_webhook(webhook_url, payload)

    def _send_teams(self, webhook_url: str, log_entry: dict[str, Any]) -> None:
        """Send MS Teams notification via incoming webhook."""
        status_color = {
            STATUS_OK: "00FF00",
            STATUS_ERROR: "FF0000",
            STATUS_SKIP: "FFAA00",
            STATUS_ESCALATION: "FF6600",
        }

        color = status_color.get(log_entry["status"], "808080")
        issue_text = f"Issue: {log_entry['issue_id']}" if log_entry["issue_id"] else "No issue"

        payload = {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "themeColor": color,
            "summary": f"AccInfra Agent Alert - {log_entry['agent']}",
            "sections": [
                {
                    "activityTitle": "AccInfra Agent Alert",
                    "facts": [
                        {"name": "Agent", "value": log_entry["agent"]},
                        {"name": "Status", "value": log_entry["status"]},
                        {"name": "Issue", "value": issue_text},
                        {"name": "Message", "value": log_entry["message"]},
                        {"name": "Time", "value": log_entry["timestamp"]},
                    ],
                    "markdown": True,
                }
            ],
        }

        if log_entry.get("error_summary"):
            payload["sections"][0]["facts"].append(
                {"name": "Error", "value": log_entry["error_summary"]}
            )

        self._post_webhook(webhook_url, payload)

    def _post_webhook(self, url: str, payload: dict[str, Any]) -> None:
        """POST JSON payload to a webhook URL. Fails silently."""
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                _ = resp.read()
        except (urllib.error.URLError, OSError):
            # Notification failure should never break the agent
            pass


# ─── Execution Timer Context Manager ───────────────────────────────────────

class ExecutionTimer:
    """Context manager to time agent executions.

    Usage:
        logger = AgentLogger("issue-watcher")
        with ExecutionTimer() as timer:
            # ... do work ...
        logger.log(STATUS_OK, "Completed", duration_seconds=timer.elapsed)
    """

    def __init__(self):
        self.start_time: float = 0
        self.end_time: float = 0

    @property
    def elapsed(self) -> float:
        """Elapsed time in seconds."""
        return round(self.end_time - self.start_time, 2)

    def __enter__(self):
        self.start_time = time.time()
        return self

    def __exit__(self, *_):
        self.end_time = time.time()


# ─── Convenience function for quick logging ─────────────────────────────────

def log_agent_execution(
    agent_name: str,
    status: str,
    message: str,
    **kwargs,
) -> dict[str, Any]:
    """One-shot log function for simple cases.

    Creates a logger, logs once, and returns the entry.
    For repeated logging in a single execution, instantiate AgentLogger directly.
    """
    logger = AgentLogger(agent_name)
    return logger.log(status, message, **kwargs)
