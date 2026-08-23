"""
AccInfra GitHub Client
──────────────────────
Handles GitHub App authentication and common API operations.
Uses short-lived installation tokens (not PATs) for security.
"""

import json
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

# JWT library for GitHub App auth
import jwt


GITHUB_API_BASE = "https://api.github.com"


class GitHubClientError(Exception):
    """Raised on GitHub API failures."""
    pass


class GitHubClient:
    """GitHub API client using App-based authentication.

    Generates short-lived installation tokens on each instantiation.
    Tokens expire after 1 hour — agents run for seconds, so this is fine.
    """

    def __init__(self, config: dict[str, Any]):
        """
        Args:
            config: The full accinfra config dict.
        """
        gh = config["github"]
        self.repo_owner = gh["repo_owner"]
        self.repo_name = gh["repo_name"]
        self.app_id = gh["app_id"]
        self.installation_id = gh["app_installation_id"]
        self.issue_prefix = gh["issue_prefix"]
        self.labels = gh.get("labels", {})
        self.approver = gh.get("approver_username", "")
        self.branch_prefix = gh.get("branch_prefix", "infra/issue-")

        # Load private key and get installation token
        key_path = Path(gh["app_private_key_path"]).expanduser()
        if not key_path.is_file():
            raise GitHubClientError(f"GitHub App private key not found: {key_path}")

        self._private_key = key_path.read_text(encoding="utf-8")
        self._token = self._get_installation_token()

    @property
    def repo_full_name(self) -> str:
        return f"{self.repo_owner}/{self.repo_name}"

    def _generate_jwt(self) -> str:
        """Generate a JWT signed with the App's private key."""
        now = int(time.time())
        payload = {
            "iat": now - 60,  # Issued at (60s in past for clock drift)
            "exp": now + (10 * 60),  # Expires in 10 minutes
            "iss": self.app_id,
        }
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    def _get_installation_token(self) -> str:
        """Exchange JWT for a short-lived installation access token."""
        app_jwt = self._generate_jwt()
        url = f"{GITHUB_API_BASE}/app/installations/{self.installation_id}/access_tokens"
        data = self._api_request("POST", url, headers_override={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
        })
        return data["token"]

    def _api_request(
        self,
        method: str,
        url: str,
        body: dict[str, Any] | None = None,
        headers_override: dict[str, str] | None = None,
    ) -> Any:
        """Make a GitHub API request.

        Args:
            method: HTTP method (GET, POST, PUT, PATCH, DELETE).
            url: Full URL.
            body: JSON body (for POST/PUT/PATCH).
            headers_override: Custom headers (skips default token auth).

        Returns:
            Parsed JSON response.
        """
        headers = headers_override or {
            "Authorization": f"token {self._token}",
            "Accept": "application/vnd.github+json",
        }
        headers["User-Agent"] = "accinfra-agents/1.0"

        data = json.dumps(body).encode("utf-8") if body else None
        if data:
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                response_body = resp.read().decode("utf-8")
                if response_body:
                    return json.loads(response_body)
                return {}
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8") if e.fp else ""
            raise GitHubClientError(
                f"GitHub API {method} {url} returned {e.code}: {error_body}"
            ) from e
        except urllib.error.URLError as e:
            raise GitHubClientError(f"GitHub API request failed: {e}") from e

    # ─── Issue Operations ───────────────────────────────────────────────

    def list_open_issues(self, label_filter: str | None = None) -> list[dict[str, Any]]:
        """List open issues in the repo, optionally filtered by label.

        Args:
            label_filter: If set, only return issues with this label.

        Returns:
            List of issue dicts from GitHub API.
        """
        url = f"{GITHUB_API_BASE}/repos/{self.repo_full_name}/issues?state=open&per_page=100"
        if label_filter:
            url += f"&labels={label_filter}"
        return self._api_request("GET", url)

    def get_new_accinfra_issues(self) -> list[dict[str, Any]]:
        """Get open issues with the accinfra prefix that haven't been triaged yet.

        Returns:
            Issues matching prefix WITHOUT the triaged label.
        """
        all_issues = self.list_open_issues()
        triaged_label = self.labels.get("triaged", "infra-automation/triaged")

        matching = []
        for issue in all_issues:
            # Skip PRs (GitHub returns them in issues endpoint too)
            if "pull_request" in issue:
                continue
            title = issue.get("title", "")
            if not title.lower().startswith(self.issue_prefix.lower()):
                continue
            # Skip already-triaged issues
            issue_labels = [lbl["name"] for lbl in issue.get("labels", [])]
            if triaged_label in issue_labels:
                continue
            matching.append(issue)

        return matching

    def add_label(self, issue_number: int, label: str) -> None:
        """Add a label to an issue."""
        url = f"{GITHUB_API_BASE}/repos/{self.repo_full_name}/issues/{issue_number}/labels"
        self._api_request("POST", url, body={"labels": [label]})

    def add_comment(self, issue_number: int, body: str) -> dict[str, Any]:
        """Add a comment to an issue."""
        url = f"{GITHUB_API_BASE}/repos/{self.repo_full_name}/issues/{issue_number}/comments"
        return self._api_request("POST", url, body={"body": body})

    def close_issue(self, issue_number: int, comment: str | None = None) -> None:
        """Close an issue, optionally with a closing comment."""
        if comment:
            self.add_comment(issue_number, comment)
        url = f"{GITHUB_API_BASE}/repos/{self.repo_full_name}/issues/{issue_number}"
        self._api_request("PATCH", url, body={"state": "closed"})

    # ─── Search Operations ──────────────────────────────────────────────

    def search_closed_issues(self, query: str, max_results: int = 20) -> list[dict[str, Any]]:
        """Search closed issues in the repo for knowledge reuse.

        Args:
            query: Search keywords.
            max_results: Max results to return.

        Returns:
            List of matching closed issues.
        """
        search_query = f"{query} repo:{self.repo_full_name} is:issue is:closed"
        url = f"{GITHUB_API_BASE}/search/issues?q={urllib.request.quote(search_query)}&per_page={max_results}"
        result = self._api_request("GET", url)
        return result.get("items", [])

    # ─── Branch & PR Operations ─────────────────────────────────────────

    def get_default_branch(self) -> str:
        """Get the repo's default branch name."""
        url = f"{GITHUB_API_BASE}/repos/{self.repo_full_name}"
        repo = self._api_request("GET", url)
        return repo.get("default_branch", "main")

    def get_branch_sha(self, branch: str) -> str:
        """Get the latest commit SHA of a branch."""
        url = f"{GITHUB_API_BASE}/repos/{self.repo_full_name}/git/ref/heads/{branch}"
        ref = self._api_request("GET", url)
        return ref["object"]["sha"]

    def create_branch(self, branch_name: str, from_sha: str) -> None:
        """Create a new branch from a given SHA."""
        url = f"{GITHUB_API_BASE}/repos/{self.repo_full_name}/git/refs"
        self._api_request("POST", url, body={
            "ref": f"refs/heads/{branch_name}",
            "sha": from_sha,
        })

    def create_or_update_file(
        self,
        branch: str,
        file_path: str,
        content: str,
        commit_message: str,
        existing_sha: str | None = None,
    ) -> dict[str, Any]:
        """Create or update a file in the repo.

        Args:
            branch: Branch to commit to.
            file_path: File path within the repo.
            content: File content (will be base64-encoded).
            commit_message: Commit message.
            existing_sha: SHA of existing file (for updates). None for new files.
        """
        import base64
        url = f"{GITHUB_API_BASE}/repos/{self.repo_full_name}/contents/{file_path}"
        body: dict[str, Any] = {
            "message": commit_message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if existing_sha:
            body["sha"] = existing_sha
        return self._api_request("PUT", url, body=body)

    def create_pull_request(
        self,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str | None = None,
        reviewers: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a pull request.

        Args:
            title: PR title.
            body: PR description.
            head_branch: Branch with changes.
            base_branch: Target branch (defaults to repo default).
            reviewers: List of GitHub usernames to request review from.

        Returns:
            Created PR dict.
        """
        base = base_branch or self.get_default_branch()
        url = f"{GITHUB_API_BASE}/repos/{self.repo_full_name}/pulls"
        pr = self._api_request("POST", url, body={
            "title": title,
            "body": body,
            "head": head_branch,
            "base": base,
        })

        # Request reviewers if specified
        if reviewers and pr.get("number"):
            review_url = f"{GITHUB_API_BASE}/repos/{self.repo_full_name}/pulls/{pr['number']}/requested_reviewers"
            self._api_request("POST", review_url, body={"reviewers": reviewers})

        return pr

    def list_open_prs(self, head_prefix: str | None = None) -> list[dict[str, Any]]:
        """List open PRs, optionally filtered by branch prefix.

        Args:
            head_prefix: If set, only return PRs whose head branch starts with this.
        """
        url = f"{GITHUB_API_BASE}/repos/{self.repo_full_name}/pulls?state=open&per_page=100"
        prs = self._api_request("GET", url)

        if head_prefix:
            prs = [pr for pr in prs if pr["head"]["ref"].startswith(head_prefix)]

        return prs

    def get_pr_reviews(self, pr_number: int) -> list[dict[str, Any]]:
        """Get reviews for a PR."""
        url = f"{GITHUB_API_BASE}/repos/{self.repo_full_name}/pulls/{pr_number}/reviews"
        return self._api_request("GET", url)

    def get_pr_review_comments(self, pr_number: int) -> list[dict[str, Any]]:
        """Get review comments (inline comments) for a PR."""
        url = f"{GITHUB_API_BASE}/repos/{self.repo_full_name}/pulls/{pr_number}/comments"
        return self._api_request("GET", url)

    def get_pr_status(self, pr_number: int) -> dict[str, Any]:
        """Get a PR's current status (open/closed/merged)."""
        url = f"{GITHUB_API_BASE}/repos/{self.repo_full_name}/pulls/{pr_number}"
        return self._api_request("GET", url)

    # ─── Actions / Workflow Runs ────────────────────────────────────────

    def get_workflow_runs_for_branch(self, branch: str) -> list[dict[str, Any]]:
        """Get recent workflow runs for a specific branch."""
        url = (
            f"{GITHUB_API_BASE}/repos/{self.repo_full_name}/actions/runs"
            f"?branch={branch}&per_page=5"
        )
        result = self._api_request("GET", url)
        return result.get("workflow_runs", [])
