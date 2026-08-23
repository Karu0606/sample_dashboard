"""
AccInfra Config Loader
Loads and validates the client-adaptable configuration file.
"""

import json
import os
from pathlib import Path
from typing import Any


# Default config path (relative to project root)
_DEFAULT_CONFIG_PATH = os.environ.get(
    "ACCINFRA_CONFIG_PATH",
    str(Path(__file__).parent.parent / "config" / "accinfra-config.json"),
)


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""
    pass


def load_config(config_path: str | None = None) -> dict[str, Any]:
    """Load and return the accinfra configuration.

    Args:
        config_path: Optional override path. Falls back to ACCINFRA_CONFIG_PATH
                     env var, then to the default relative path.

    Returns:
        Parsed configuration dictionary.

    Raises:
        ConfigError: If the file is missing or contains invalid JSON.
    """
    path = config_path or _DEFAULT_CONFIG_PATH

    if not os.path.isfile(path):
        raise ConfigError(f"Config file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        try:
            config = json.load(f)
        except json.JSONDecodeError as e:
            raise ConfigError(f"Invalid JSON in config file: {e}") from e

    _validate_required_fields(config)
    return config


def _validate_required_fields(config: dict[str, Any]) -> None:
    """Validate that essential fields are present."""
    required = [
        ("github", "repo_owner"),
        ("github", "repo_name"),
        ("github", "issue_prefix"),
        ("aws", "region"),
        ("aws", "agent_role_arn"),
        ("aws", "cloudwatch_log_group_prefix"),
    ]

    for section, field in required:
        if section not in config:
            raise ConfigError(f"Missing config section: {section}")
        if field not in config[section] or not config[section][field]:
            raise ConfigError(f"Missing required config: {section}.{field}")


def get_github_config(config: dict[str, Any]) -> dict[str, Any]:
    """Extract GitHub-specific configuration."""
    return config["github"]


def get_aws_config(config: dict[str, Any]) -> dict[str, Any]:
    """Extract AWS-specific configuration."""
    return config["aws"]


def get_notification_config(config: dict[str, Any]) -> dict[str, Any]:
    """Extract notification configuration."""
    return config.get("notifications", {})


def get_agent_config(config: dict[str, Any], agent_name: str) -> dict[str, Any]:
    """Extract per-agent configuration.

    Args:
        config: Full config dict.
        agent_name: One of: issue_watcher, solution_architect, review_handler, deploy_closer

    Returns:
        Agent-specific settings dict.
    """
    agents = config.get("agents", {})
    return agents.get(agent_name, {})


def get_security_config(config: dict[str, Any]) -> dict[str, Any]:
    """Extract security configuration (allowed/blocked resources)."""
    return config.get("security", {})


def is_notifications_enabled(config: dict[str, Any]) -> bool:
    """Check if any notification channel is configured."""
    notif = get_notification_config(config)
    slack_url = notif.get("slack_webhook_url", "")
    teams_url = notif.get("teams_webhook_url", "")
    return bool(slack_url) or bool(teams_url)
