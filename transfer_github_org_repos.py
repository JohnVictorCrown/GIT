#!/usr/bin/env python3
"""
GitHub Organization → Personal Account Repository Transfer Script

Transfers ALL repositories from a source GitHub organization (or user)
to a target GitHub user account using the GitHub Transfer API.

This is useful when you've accumulated repos in an org but want to
consolidate them under your personal account.

API: POST /repos/{owner}/{repo}/transfer
Docs: https://docs.github.com/en/rest/repos/repos#transfer-a-repository

Usage:
    1. Set required environment variables (or the script will prompt you):
       export GITHUB_TOKEN="ghp_..."
       export SOURCE_ORG="Stellarium-Foundation"
       export TARGET_USER="JohnVictorCrown"

    2. (Optional) customize:
       export LOCAL_REPO_BASE_PATH="C:/Users/John Victor/Documents/Development"

    3. Dry run first:
       python transfer_github_org_repos.py --dry-run

    4. Run for real:
       python transfer_github_org_repos.py

Requires: Python 3.8+, requests library (pip install requests)
"""

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

try:
    import requests
except ImportError:
    print("Error: 'requests' library is required. Install with: pip install requests")
    sys.exit(1)


# ─── Configuration ───────────────────────────────────────────────────────────

class Config:
    """Configuration sourced from environment variables."""

    def __init__(self) -> None:
        self.token: str = self._get_env("GITHUB_TOKEN", prompt="GitHub personal access token (needs repo scope + org admin)")
        self.source_org: str = self._get_env("SOURCE_ORG", prompt="Source GitHub org/user (e.g. Stellarium-Foundation)")
        self.target_user: str = self._get_env("TARGET_USER", prompt="Target GitHub username (e.g. JohnVictorCrown)")
        self.local_repo_base_path: Optional[str] = os.getenv("LOCAL_REPO_BASE_PATH")

    @staticmethod
    def _get_env(name: str, prompt: str) -> str:
        value = os.getenv(name)
        if not value:
            value = input(f"  {prompt}: ").strip()
            if not value:
                print(f"Error: {name} is required.")
                sys.exit(1)
            print()
        return value


# ─── GitHub API helpers ──────────────────────────────────────────────────────

GITHUB_API = "https://api.github.com"

_github_remaining: int = 5000
_github_reset: float = 0.0


def _track_rate_limit(resp: requests.Response) -> None:
    """Track GitHub API rate limits from response headers."""
    global _github_remaining, _github_reset
    remaining = resp.headers.get("X-RateLimit-Remaining")
    reset = resp.headers.get("X-RateLimit-Reset")
    if remaining is not None:
        _github_remaining = int(remaining)
    if reset is not None:
        _github_reset = int(reset)

    if _github_remaining < 50:
        wait_time = max(0, _github_reset - time.time()) + 5
        print(f"  Rate limit low ({_github_remaining} remaining). Resets in ~{int(wait_time)}s.")
        if _github_remaining < 5:
            print(f"  Waiting {int(wait_time)}s for rate limit to reset...")
            time.sleep(wait_time)


def github_get(url: str, token: str, params: Optional[dict[str, Any]] = None) -> requests.Response:
    """Make a GET request to GitHub API with rate-limit awareness."""
    global _github_remaining
    if _github_remaining < 5:
        wait = max(0, _github_reset - time.time()) + 5
        print(f"  Waiting {int(wait)}s for GitHub rate limit to reset...")
        time.sleep(wait)

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    _track_rate_limit(resp)
    return resp


def github_post(url: str, token: str, json_body: dict[str, Any]) -> requests.Response:
    """Make a POST request to GitHub API with rate-limit awareness."""
    global _github_remaining
    if _github_remaining < 5:
        wait = max(0, _github_reset - time.time()) + 5
        print(f"  Waiting {int(wait)}s for GitHub rate limit to reset...")
        time.sleep(wait)

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    resp = requests.post(url, headers=headers, json=json_body, timeout=30)
    _track_rate_limit(resp)
    return resp


# ─── List repos in an organization ───────────────────────────────────────────

def list_org_repos(org: str, token: str) -> list[dict[str, Any]]:
    """Return all repos in the given organization (handles pagination)."""
    repos: list[dict[str, Any]] = []
    page = 1
    per_page = 100

    print(f"\nFetching repositories from organization '{org}'...")
    while True:
        url = f"{GITHUB_API}/orgs/{org}/repos"
        params: dict[str, Any] = {
            "per_page": per_page,
            "page": page,
            "type": "all",
            "sort": "full_name",
            "direction": "asc",
        }
        resp = github_get(url, token, params=params)
        if resp.status_code != 200:
            print(f"  GitHub API error ({resp.status_code}): {resp.text}")
            print(f"     Make sure '{org}' exists and your token has access.")
            sys.exit(1)

        batch = resp.json()
        if not batch:
            break
        repos.extend(batch)
        print(f"  Page {page}: {len(batch)} repos ({_github_remaining} API calls remaining)")
        page += 1

    print(f"  Found {len(repos)} repos in '{org}'")
    return repos


# ─── Transfer a repository ───────────────────────────────────────────────────

def transfer_repo(
    owner: str,
    repo_name: str,
    new_owner: str,
    token: str,
) -> bool:
    """Trigger a transfer for a single repo via the Transfer API.

    Returns True if transfer was accepted (202), False otherwise.
    """
    url = f"{GITHUB_API}/repos/{owner}/{repo_name}/transfer"
    payload = {"new_owner": new_owner}

    print(f"    Transferring '{owner}/{repo_name}' -> '{new_owner}'...")
    resp = github_post(url, token, payload)

    if resp.status_code == 202:
        print(f"    Transfer accepted!")
        return True
    elif resp.status_code == 403:
        print(f"    Forbidden (403). Check that your token has admin access to '{owner}/{repo_name}'.")
        try:
            detail = resp.json()
            print(f"       Message: {detail.get('message', resp.text)}")
        except Exception:
            print(f"       {resp.text}")
        return False
    elif resp.status_code == 404:
        print(f"    Not found (404). Repo '{owner}/{repo_name}' may not exist or token lacks access.")
        return False
    elif resp.status_code == 422:
        try:
            detail = resp.json()
            msg = detail.get("message", resp.text)
            errors = detail.get("errors", [])
            print(f"    Validation error (422): {msg}")
            for err in errors:
                print(f"       - {err.get('message', str(err))}")
        except Exception:
            print(f"    Validation error (422): {resp.text}")
        return False
    else:
        print(f"    Unexpected error ({resp.status_code}): {resp.text}")
        return False


def verify_transfer(
    repo_name: str,
    target_user: str,
    token: str,
    max_wait: int = 120,
    poll_interval: int = 5,
) -> bool:
    """Poll to confirm the repo exists under the new owner.

    After a transfer is accepted (202), GitHub processes it asynchronously.
    This polls GET /repos/{target_user}/{repo} until it returns 200.
    """
    url = f"{GITHUB_API}/repos/{target_user}/{repo_name}"
    start = time.time()

    while time.time() - start < max_wait:
        resp = github_get(url, token)
        if resp.status_code == 200:
            return True
        elif resp.status_code == 404:
            # Not yet moved — keep polling
            time.sleep(poll_interval)
            continue
        elif resp.status_code == 403:
            # Could be rate-limited mid-verify; wait and retry
            time.sleep(poll_interval * 2)
            continue
        else:
            time.sleep(poll_interval)
            continue

    print(f"    Transfer verification timed out after {max_wait}s for '{target_user}/{repo_name}'")
    return False


# ─── Local repo helpers ──────────────────────────────────────────────────────

def find_local_repo(repo_name: str, base_path: Optional[str]) -> Optional[Path]:
    """Search for a local git clone of the given repo name (up to 4 levels deep)."""
    if not base_path:
        return None

    search_path = Path(base_path).expanduser().resolve()
    if not search_path.is_dir():
        print(f"    Search path '{search_path}' does not exist")
        return None

    found: Optional[Path] = None
    visited = 0

    for root, dirs, _ in os.walk(search_path, topdown=True):
        rel_depth = Path(root).relative_to(search_path).parts
        if len(rel_depth) >= 4:
            dirs.clear()
            continue

        for dname in dirs:
            if dname.lower() == repo_name.lower():
                candidate = Path(root) / dname
                if (candidate / ".git").is_dir():
                    found = candidate
                    break
        if found:
            break

        visited += 1
        if visited > 10000:
            break

    return found


def get_current_remote_url(repo_path: Path, remote_name: str = "origin") -> Optional[str]:
    """Get the current fetch URL for a remote."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "remote", "get-url", remote_name],
            capture_output=True, text=True, check=True, timeout=15,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def set_remote_url(repo_path: Path, remote_name: str, new_url: str) -> bool:
    """Update a git remote URL."""
    try:
        subprocess.run(
            ["git", "-C", str(repo_path), "remote", "set-url", remote_name, new_url],
            capture_output=True, text=True, check=True, timeout=15,
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"    Failed to update remote '{remote_name}': {e.stderr.strip()}")
        return False
    except subprocess.TimeoutExpired:
        print(f"    Timeout updating remote")
        return False


def add_or_update_remote(repo_path: Path, remote_name: str, url: str) -> bool:
    """Add or update a git remote."""
    try:
        existing = get_current_remote_url(repo_path, remote_name)
        if existing:
            if existing == url:
                return True
            return set_remote_url(repo_path, remote_name, url)

        subprocess.run(
            ["git", "-C", str(repo_path), "remote", "add", remote_name, url],
            capture_output=True, text=True, check=True, timeout=15,
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"    Failed to add remote '{remote_name}': {e.stderr.strip()}")
        return False
    except subprocess.TimeoutExpired:
        return False


def update_local_remotes(
    repo_path: Path,
    repo_name: str,
    old_org: str,
    new_owner: str,
) -> None:
    """Update local git remotes to point to the new owner."""
    old_url = f"https://github.com/{old_org}/{repo_name}.git"
    new_url = f"https://github.com/{new_owner}/{repo_name}.git"

    current = get_current_remote_url(repo_path, "origin")
    if not current:
        print(f"    No 'origin' remote found")
        return

    print(f"    Current origin: {current}")

    if current == new_url:
        print(f"    Origin already points to {new_url}")
        return

    if current != old_url and old_url not in current:
        print(f"    Current origin ({current}) doesn't match expected old URL ({old_url}).")
        print(f"    Preserving old URL as 'old-org' remote: {current}")
        add_or_update_remote(repo_path, "old-org", current)

    print(f"    Updating origin -> {new_url}")
    if set_remote_url(repo_path, "origin", new_url):
        print(f"    Origin now points to '{new_owner}/{repo_name}'")


# ─── Main ────────────────────────────────────────────────────────────────────

BANNER = r"""
╔══════════════════════════════════════════════════════════════╗
║    GitHub Organization -> Personal Account Transfer Script    ║
║                                                              ║
║  Transfers all repos from a GitHub org/user to another       ║
║  GitHub user using the Transfer API.                         ║
╚══════════════════════════════════════════════════════════════╝
"""


def parse_args() -> tuple[bool, bool]:
    args = set(sys.argv[1:])
    if "--help" in args or "-h" in args:
        print(__doc__)
        sys.exit(0)
    dry_run = bool(args & {"--dry-run", "-n"})
    skip_confirm = bool(args & {"--yes", "-y"})
    unknown = args - {"--dry-run", "-n", "--yes", "-y", "--help", "-h"}
    if unknown:
        print(f"Unknown flags: {unknown}")
        print("Usage: python transfer_github_org_repos.py [--dry-run|-n] [--yes|-y]")
        sys.exit(1)
    return dry_run, skip_confirm


def main() -> None:
    print(BANNER)

    dry_run, skip_confirm = parse_args()
    if dry_run:
        print("DRY RUN MODE: No changes will be made.\n")

    # ── Configuration ──
    config = Config()

    label = " (dry run)" if dry_run else ""
    print(f"Configuration:{label}")
    print(f"   Source:          {config.source_org}")
    print(f"   Target:          {config.target_user}")
    print(f"   Local repo path: {config.local_repo_base_path or '(not set -> skip local updates)'}")
    print()

    # ── Confirm ──
    if not skip_confirm:
        print(f"This will transfer ALL repos from '{config.source_org}' to '{config.target_user}'.")
        print(f"The repos will NO LONGER belong to '{config.source_org}' after transfer.")
        print(f"Make sure you have admin access to '{config.source_org}'.")
        confirm = input("   Continue? (yes/no): ").strip().lower()
        if confirm != "yes":
            print("Aborted.")
            sys.exit(0)

    # ── Fetch repos ──
    repos = list_org_repos(config.source_org, config.token)
    if not repos:
        print("No repositories found. Nothing to transfer.")
        sys.exit(0)

    # Separate archived and forks
    active_repos = [r for r in repos if not r.get("archived")]
    archived = [r for r in repos if r.get("archived")]
    if archived:
        print(f"Skipping {len(archived)} archived repo(s): {', '.join(r['name'] for r in archived)}")

    if not active_repos:
        print("No active repositories to transfer.")
        sys.exit(0)

    # Check for forks and warn
    forks = [r for r in active_repos if r.get("fork")]
    if forks:
        print(f"Note: {len(forks)} repo(s) are forks — they will be disconnected from upstream after transfer.")
        for f in forks:
            parent = f.get("parent", {})
            parent_full = parent.get("full_name", "unknown") if parent else "unknown"
            print(f"       {f['name']} (forked from {parent_full})")

    # ── Transfer each repo ──
    total = len(active_repos)
    results: list[dict[str, Any]] = []

    for i, repo in enumerate(active_repos, 1):
        repo_name: str = repo["name"]
        is_fork = repo.get("fork", False)
        is_private = repo.get("private", False)

        print(f"\n--- Repo {i}/{total}: {repo_name} ---")
        print(f"   Type: {'Private' if is_private else 'Public'}{' (fork)' if is_fork else ''}")

        result: dict[str, Any] = {
            "repo": repo_name,
            "status": "not_started",
        }

        if dry_run:
            print(f"   [DRY RUN] Would transfer '{config.source_org}/{repo_name}' -> '{config.target_user}'")
            result["status"] = "dry_run"
        else:
            accepted = transfer_repo(
                owner=config.source_org,
                repo_name=repo_name,
                new_owner=config.target_user,
                token=config.token,
            )

            if accepted:
                # Transfer is async (202 Accepted). Verify it completed.
                print(f"    Verifying transfer completed...")
                verified = verify_transfer(
                    repo_name=repo_name,
                    target_user=config.target_user,
                    token=config.token,
                    max_wait=120,
                )
                if verified:
                    print(f"    Transfer verified -- repo is now under '{config.target_user}/{repo_name}'")
                    result["status"] = "transferred"
                else:
                    print(f"    Transfer triggered but not yet confirmed. Check manually later.")
                    result["status"] = "pending"
            else:
                result["status"] = "failed"

        # ── Update local remotes (only if transfer confirmed) ──
        if result["status"] == "transferred":
            repo_path = find_local_repo(repo_name, config.local_repo_base_path)
            if repo_path:
                print(f"   Found local clone at: {repo_path}")
                if dry_run:
                    print(f"   [DRY RUN] Would update origin -> https://github.com/{config.target_user}/{repo_name}.git")
                    result["local"] = "dry_run"
                else:
                    update_local_remotes(repo_path, repo_name, config.source_org, config.target_user)
                    result["local"] = "updated"
            else:
                print(f"   No local clone found")
                result["local"] = "not_found"
        else:
            print(f"   Skipped local remote update (transfer {result['status']})")
            result["local"] = "skipped"

        results.append(result)

    # ── Summary ──
    print(f"\n\n{'='*60}")
    print("TRANSFER SUMMARY")
    print(f"{'='*60}")

    transferred = sum(1 for r in results if r["status"] == "transferred")
    pending = sum(1 for r in results if r["status"] == "pending")
    failed = sum(1 for r in results if r["status"] == "failed")
    dry_run_count = sum(1 for r in results if r["status"] == "dry_run")

    print(f"  Total active repos:   {total}")
    if dry_run:
        print(f"  Dry run:              {dry_run_count}")
    else:
        print(f"  Transferred:          {transferred}")
        print(f"  Pending:              {pending}")
        print(f"  Failed:               {failed}")

    if not dry_run:
        print(f"\nNext steps:")
        print(f"  - Verify repos at: https://github.com/{config.target_user}")
        print(f"  - Update any CI/CD, docs, or webhooks pointing to the old org URLs")
        print(f"  - Local remotes have been updated to point to the new location")
        if pending:
            print(f"  - {pending} transfer(s) still pending -- run the script again to verify or check manually.")
        if failed:
            print(f"  - {failed} transfer(s) failed -- check errors above and retry individually if needed.")
        print(f"  - Transferred repos will be private if they were private. Make them public if needed.")
    else:
        print(f"\nDry run complete. Run without --dry-run to execute.")

    print()


if __name__ == "__main__":
    main()
