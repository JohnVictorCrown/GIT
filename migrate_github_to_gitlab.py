#!/usr/bin/env python3
"""
GitHub → GitLab Migration Script

Migrates all repositories from your GitHub account to GitLab via the GitLab
Import API, then updates your local git remotes:
  - `origin` → points to StellariumFoundation on GitHub (or your org of choice)
  - `gitlab` → points to the new project on GitLab
  - Configures the `gh` CLI (if installed) to use the new GitHub org/repo

Usage:
    1. Set required environment variables (or the script will prompt you):
       export GITHUB_USERNAME="your_github_username"
       export GITHUB_TOKEN="ghp_..."
       export GITLAB_TOKEN="glpat-..."
       export GITLAB_NAMESPACE="your-gitlab-namespace"

    2. (Optional) customize further:
       export GITLAB_URL="https://gitlab.com"              # default
       export NEW_GITHUB_ORG="StellariumFoundation"        # default
       export LOCAL_REPO_BASE_PATH="/path/to/projects"      # search for local clones
       export POLL_INTERVAL=10                               # seconds between import status checks (default)
       export IMPORT_TIMEOUT=600                             # max seconds to wait per import (default)

    3. Run with --dry-run first:
       python migrate_github_to_gitlab.py --dry-run

    4. Then run for real:
       python migrate_github_to_gitlab.py

Requires: Python 3.8+, `requests` library (pip install requests)
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
    """Holds all configuration, sourced from environment variables."""

    def __init__(self) -> None:
        self.github_username: str = self._get_env("GITHUB_USERNAME", prompt="GitHub username")
        self.github_token: str = self._get_env("GITHUB_TOKEN", prompt="GitHub personal access token (with `repo` scope)")
        self.gitlab_token: str = self._get_env("GITLAB_TOKEN", prompt="GitLab personal access token (with `api` scope)")
        self.gitlab_namespace: str = self._get_env("GITLAB_NAMESPACE", prompt="GitLab target namespace (user or group path)")
        self.gitlab_url: str = os.getenv("GITLAB_URL", "https://gitlab.com").rstrip("/")
        self.new_github_org: str = os.getenv("NEW_GITHUB_ORG", "StellariumFoundation")
        self.local_repo_base_path: Optional[str] = os.getenv("LOCAL_REPO_BASE_PATH")
        self.poll_interval: int = int(os.getenv("POLL_INTERVAL", "10"))
        self.import_timeout: int = int(os.getenv("IMPORT_TIMEOUT", "600"))
        self.origin_target: str = os.getenv("ORIGIN_TARGET", "github").strip().lower()
        # origin_target can be "github" (origin → StellariumFoundation GitHub) or
        # "gitlab" (origin → GitLab, and a `github` remote for StellariumFoundation)

    @staticmethod
    def _get_env(name: str, prompt: str) -> str:
        value = os.getenv(name)
        if not value:
            value = input(f"  {prompt}: ").strip()
            if not value:
                print(f"Error: {name} is required.")
                sys.exit(1)
            print()  # spacing after prompt
        return value


# ─── GitHub API helpers ──────────────────────────────────────────────────────

GITHUB_API = "https://api.github.com"

# Rate-limit tracking — check before each GitHub call
_github_remaining_requests: int = 5000
_github_reset_time: float = 0.0


def _check_github_rate_limit(response: requests.Response) -> None:
    """Update internal rate-limit tracking from response headers."""
    global _github_remaining_requests, _github_reset_time
    remaining = response.headers.get("X-RateLimit-Remaining")
    reset = response.headers.get("X-RateLimit-Reset")
    if remaining is not None:
        _github_remaining_requests = int(remaining)
    if reset is not None:
        _github_reset_time = int(reset)

    if _github_remaining_requests < 100:
        wait_time = max(0, _github_reset_time - time.time()) + 5
        print(f"  ⚠️  GitHub API rate limit low ({_github_remaining_requests} remaining). "
              f"Rate limit resets in ~{int(wait_time)}s.")


def github_get(url: str, token: str, params: Optional[dict[str, Any]] = None) -> requests.Response:
    """Make a GET request to the GitHub API with rate-limit awareness."""
    global _github_remaining_requests
    if _github_remaining_requests < 10:
        wait = max(0, _github_reset_time - time.time()) + 5
        print(f"  ⏳ Waiting {int(wait)}s for GitHub rate limit to reset...")
        time.sleep(wait)

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    _check_github_rate_limit(resp)
    return resp


def get_all_github_repos(username: str, token: str) -> list[dict[str, Any]]:
    """Return all repositories owned by the user (handles pagination)."""
    repos: list[dict[str, Any]] = []
    page = 1
    per_page = 100

    print(f"\n🔍 Fetching repositories for user '{username}' from GitHub...")
    while True:
        url = f"{GITHUB_API}/user/repos"
        params: dict[str, Any] = {
            "per_page": per_page,
            "page": page,
            "type": "owner",
            "sort": "full_name",
            "direction": "asc",
        }
        resp = github_get(url, token, params=params)
        if resp.status_code != 200:
            print(f"  ❌ GitHub API error ({resp.status_code}): {resp.text}")
            sys.exit(1)

        batch = resp.json()
        if not batch:
            break
        repos.extend(batch)
        print(f"  Fetched page {page} ({len(batch)} repos, {_github_remaining_requests} API calls remaining)...")
        page += 1

    # Filter to only repos owned by this user (exclude org repos, forks of others, etc.)
    user_repos = [r for r in repos if r["owner"]["login"].lower() == username.lower()]
    print(f"  ✅ Found {len(user_repos)} repos owned by '{username}' "
          f"({len(repos) - len(user_repos)} belonging to orgs/others skipped)")
    return user_repos


# ─── GitLab API helpers ──────────────────────────────────────────────────────

def gitlab_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def trigger_gitlab_import(
    gitlab_url: str,
    gitlab_token: str,
    github_token: str,
    repo_id: int,
    target_namespace: str,
    new_name: str,
) -> Optional[dict[str, Any]]:
    """Trigger a GitHub→GitLab import via the Import API.

    Returns the response JSON (which includes the new project ID) or None on failure.
    """
    url = f"{gitlab_url}/api/v4/import/github"
    payload: dict[str, Any] = {
        "personal_access_token": github_token,
        "repo_id": repo_id,
        "target_namespace": target_namespace,
        "new_name": new_name,
    }

    resp = requests.post(url, headers=gitlab_headers(gitlab_token), json=payload, timeout=30)

    if resp.status_code in (201, 200):
        return resp.json()
    elif resp.status_code == 409:
        # 409 Conflict - likely a project with that name already exists in the namespace
        try:
            detail = resp.json()
            msg = detail.get("message", resp.text)
            print(f"    ⚠️  Project may already exist ({resp.status_code}): {msg}")
            # Try to extract existing project ID from the error message
            return {"id": None, "already_exists": True, "message": msg}
        except Exception:
            print(f"    ⚠️  Conflict (409) when triggering import: {resp.text}")
            return {"id": None, "already_exists": True}
    else:
        print(f"    ❌ GitLab import trigger failed ({resp.status_code}): {resp.text}")
        return None


def check_import_status(gitlab_url: str, gitlab_token: str, project_id: int) -> dict[str, Any]:
    """Check the import status of a GitLab project."""
    url = f"{gitlab_url}/api/v4/projects/{project_id}/import"
    resp = requests.get(url, headers=gitlab_headers(gitlab_token), timeout=30)
    if resp.status_code == 200:
        return resp.json()
    # The dedicated /import endpoint may return 404 when import is finished.
    # Fall back to the project detail endpoint.
    url = f"{gitlab_url}/api/v4/projects/{project_id}"
    resp = requests.get(url, headers=gitlab_headers(gitlab_token), timeout=30)
    if resp.status_code == 200:
        return {"import_status": "finished", "project": resp.json()}
    return {"import_status": "unknown", "error": f"HTTP {resp.status_code}"}


def wait_for_import(
    gitlab_url: str,
    gitlab_token: str,
    project_id: int,
    repo_name: str,
    poll_interval: int = 10,
    timeout: int = 600,
) -> bool:
    """Poll import status until finished or failed.

    Returns True if import succeeded, False otherwise.
    """
    start = time.time()
    last_status = ""
    while time.time() - start < timeout:
        status_data = check_import_status(gitlab_url, gitlab_token, project_id)
        import_status = status_data.get("import_status", "unknown")

        if import_status != last_status:
            print(f"    ⏳ Import status: {import_status}")
            last_status = import_status

        if import_status == "finished":
            print(f"    ✅ Import complete!")
            return True
        elif import_status == "failed":
            error = status_data.get("import_error", "Unknown error")
            print(f"    ❌ Import failed: {error}")
            return False
        elif import_status in ("none", "unknown"):
            # Import may not have started yet; keep polling
            time.sleep(poll_interval)
            continue

        time.sleep(poll_interval)

    print(f"    ⚠️  Import timed out after {timeout}s for '{repo_name}'")
    return False


def get_or_find_project_id(
    gitlab_url: str,
    gitlab_token: str,
    namespace: str,
    repo_name: str,
) -> Optional[int]:
    """Try to find a GitLab project by namespace/name when the import trigger
    returned a 409 (already exists)."""
    url = f"{gitlab_url}/api/v4/projects"
    params: dict[str, Any] = {"search": repo_name, "per_page": 50}
    resp = requests.get(url, headers=gitlab_headers(gitlab_token), params=params, timeout=30)
    if resp.status_code == 200:
        for proj in resp.json():
            if proj.get("path_with_namespace", "").lower() == f"{namespace}/{repo_name}".lower():
                return proj.get("id")
    return None


# ─── Git & gh CLI helpers ────────────────────────────────────────────────────

def find_local_repo(repo_name: str, base_path: Optional[str]) -> Optional[Path]:
    """Search for a local git clone of the given repo name.

    Recursively searches (up to 4 levels deep) for directories named
    `<repo_name>` containing a `.git` subdirectory.

    If base_path is not set, returns None (skips local operations).
    """
    if not base_path:
        return None

    search_path = Path(base_path).expanduser().resolve()
    if not search_path.is_dir():
        print(f"    ⚠️  Local repo search path '{search_path}' does not exist")
        return None

    found: Optional[Path] = None
    visited = 0

    for root, dirs, _ in os.walk(search_path, topdown=True):
        # Limit depth: control by pruning deep directories
        rel_depth = Path(root).relative_to(search_path).parts
        if len(rel_depth) >= 4:
            dirs.clear()  # Don't descend further
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
            # Safety limit: stop searching if we've checked too many dirs
            break

    return found


def get_current_remote_url(repo_path: Path, remote_name: str = "origin") -> Optional[str]:
    """Get the current fetch URL for a given remote."""
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
        print(f"    ⚠️  Failed to update remote '{remote_name}': {e.stderr.strip()}")
        return False
    except subprocess.TimeoutExpired:
        print(f"    ⚠️  Timeout updating remote '{remote_name}'")
        return False


def add_or_update_remote(repo_path: Path, remote_name: str, url: str) -> bool:
    """Add a new git remote or update an existing one."""
    try:
        existing = get_current_remote_url(repo_path, remote_name)
        if existing:
            if existing == url:
                return True  # Already correct
            return set_remote_url(repo_path, remote_name, url)

        subprocess.run(
            ["git", "-C", str(repo_path), "remote", "add", remote_name, url],
            capture_output=True, text=True, check=True, timeout=15,
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"    ⚠️  Failed to add remote '{remote_name}': {e.stderr.strip()}")
        return False
    except subprocess.TimeoutExpired:
        print(f"    ⚠️  Timeout adding remote '{remote_name}'")
        return False


def configure_gh_cli(repo_path: Path, org: str, repo_name: str) -> bool:
    """Configure the `gh` CLI to use the new GitHub repo as the default remote."""
    try:
        subprocess.run(
            ["gh", "repo", "set-default", f"{org}/{repo_name}"],
            cwd=str(repo_path),
            capture_output=True, text=True, timeout=15,
        )
        return True
    except FileNotFoundError:
        # gh CLI not installed — skip silently
        return False
    except subprocess.TimeoutExpired:
        return False
    except Exception:
        return False


def update_local_remotes(
    repo_path: Path,
    repo_name: str,
    old_github_username: str,
    new_github_org: str,
    gitlab_url: str,
    gitlab_namespace: str,
    origin_target: str = "github",
) -> None:
    """Update local git remotes based on the chosen origin strategy.

    origin_target="github" (default):
      - `origin` → StellariumFoundation GitHub
      - `gitlab` → GitLab project

    origin_target="gitlab":
      - `origin` → GitLab project (the primary remote)
      - `github` → StellariumFoundation GitHub
    """
    old_github_url = f"https://github.com/{old_github_username}/{repo_name}.git"
    new_github_url = f"https://github.com/{new_github_org}/{repo_name}.git"
    gitlab_project_url = f"{gitlab_url}/{gitlab_namespace}/{repo_name}.git"

    current_origin = get_current_remote_url(repo_path, "origin")
    if not current_origin:
        print(f"    ⚠️  No 'origin' remote found in '{repo_path}'")
        return

    print(f"    📍 Current origin: {current_origin}")

    if origin_target == "gitlab":
        # origin → GitLab, github → StellariumFoundation
        if current_origin != gitlab_project_url:
            print(f"    🔄 Changing origin → {gitlab_project_url}")
            if set_remote_url(repo_path, "origin", gitlab_project_url):
                print(f"    ✅ Origin now points to GitLab")
        else:
            print(f"    ✅ Origin already points to GitLab")

        # Add/update `github` remote for StellariumFoundation
        if add_or_update_remote(repo_path, "github", new_github_url):
            print(f"    ✅ 'github' remote → StellariumFoundation")
        else:
            print(f"    ❌ Failed to set 'github' remote")
    else:
        # origin → StellariumFoundation GitHub, gitlab → GitLab project
        if current_origin != new_github_url:
            print(f"    🔄 Changing origin → {new_github_url}")
            if set_remote_url(repo_path, "origin", new_github_url):
                print(f"    ✅ Origin now points to StellariumFoundation GitHub")
        else:
            print(f"    ✅ Origin already points to StellariumFoundation GitHub")

        # Add/update `gitlab` remote
        if add_or_update_remote(repo_path, "gitlab", gitlab_project_url):
            print(f"    ✅ 'gitlab' remote → GitLab")
        else:
            print(f"    ❌ Failed to set 'gitlab' remote")

    # Configure `gh` CLI if available
    if configure_gh_cli(repo_path, new_github_org, repo_name):
        print(f"    ✅ `gh` CLI configured to {new_github_org}/{repo_name}")


# ─── Migration orchestration ─────────────────────────────────────────────────

def migrate_repo(
    repo: dict[str, Any],
    config: Config,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Migrate a single repository from GitHub to GitLab and update local remotes.

    Returns a dict with the result status.
    """
    repo_name: str = repo["name"]
    repo_full_name: str = repo["full_name"]
    repo_id: int = repo["id"]
    default_branch: str = repo.get("default_branch", "main")
    repo_url: str = repo.get("clone_url", f"https://github.com/{repo_full_name}.git")

    print(f"\n{'='*60}")
    print(f"📦 Migrating: {repo_full_name}")
    print(f"   Repo ID: {repo_id}")
    print(f"   Default branch: {default_branch}")
    print(f"{'='*60}")

    result: dict[str, Any] = {
        "repo": repo_name,
        "github_id": repo_id,
        "gitlab_project_id": None,
        "import_status": "not_started",
        "local_updates": [],
    }

    # ── Step 1: Trigger GitLab import ──
    print(f"\n  1️⃣  Triggering GitLab import...")
    if dry_run:
        print(f"     [DRY RUN] Would trigger import: repo_id={repo_id}, "
              f"namespace={config.gitlab_namespace}, new_name={repo_name}")
        result["import_status"] = "dry_run"
    else:
        import_result = trigger_gitlab_import(
            gitlab_url=config.gitlab_url,
            gitlab_token=config.gitlab_token,
            github_token=config.github_token,
            repo_id=repo_id,
            target_namespace=config.gitlab_namespace,
            new_name=repo_name,
        )

        if import_result is None:
            result["import_status"] = "trigger_failed"
            return result

        already_exists = import_result.get("already_exists", False)
        project_id = import_result.get("id")

        if already_exists and project_id is None:
            # 409 Conflict - project may already exist. Try to find its ID.
            print(f"     ⚠️  Project may already exist. Searching for it...")
            project_id = get_or_find_project_id(
                config.gitlab_url, config.gitlab_token,
                config.gitlab_namespace, repo_name,
            )
            if project_id:
                print(f"     ✅ Found existing project ID: {project_id}")
                result["import_status"] = "already_exists"
                result["gitlab_project_id"] = project_id
            else:
                print(f"     ❌ Could not find project in namespace "
                      f"'{config.gitlab_namespace}/{repo_name}'")
                result["import_status"] = "already_exists_not_found"
                return result
        elif project_id:
            result["gitlab_project_id"] = project_id
            print(f"     ✅ Import triggered! GitLab project ID: {project_id}")

            # ── Step 2: Wait for import to finish ──
            print(f"\n  2️⃣  Waiting for import to complete...")
            success = wait_for_import(
                gitlab_url=config.gitlab_url,
                gitlab_token=config.gitlab_token,
                project_id=project_id,
                repo_name=repo_name,
                poll_interval=config.poll_interval,
                timeout=config.import_timeout,
            )
            result["import_status"] = "finished" if success else "failed"
        else:
            result["import_status"] = "trigger_failed"
            return result

    # ── Step 3: Update local git remotes ──
    print(f"\n  3️⃣  Updating local git remotes...")
    repo_path = find_local_repo(repo_name, config.local_repo_base_path)
    if repo_path:
        print(f"     Found local clone at: {repo_path}")
        if dry_run:
            if config.origin_target == "gitlab":
                print(f"     [DRY RUN] origin → {config.gitlab_url}/{config.gitlab_namespace}/{repo_name}.git")
                print(f"     [DRY RUN] github → https://github.com/{config.new_github_org}/{repo_name}.git")
            else:
                print(f"     [DRY RUN] origin → https://github.com/{config.new_github_org}/{repo_name}.git")
                print(f"     [DRY RUN] gitlab → {config.gitlab_url}/{config.gitlab_namespace}/{repo_name}.git")
            result["local_updates"].append("dry_run")
        else:
            update_local_remotes(
                repo_path=repo_path,
                repo_name=repo_name,
                old_github_username=config.github_username,
                new_github_org=config.new_github_org,
                gitlab_url=config.gitlab_url,
                gitlab_namespace=config.gitlab_namespace,
                origin_target=config.origin_target,
            )
            result["local_updates"].append("remotes_updated")
    else:
        print(f"     ℹ️  No local clone found (or LOCAL_REPO_BASE_PATH not set)")
        result["local_updates"].append("no_local_clone_found")

    return result


# ─── Main ────────────────────────────────────────────────────────────────────

BANNER = r"""
╔══════════════════════════════════════════════════════════════╗
║         GitHub → GitLab Migration Script                     ║
║                                                              ║
║  • Migrates repos via GitLab Import API                      ║
║  • Updates local git remotes                                 ║
║  • Configures `gh` CLI (if installed)                        ║
╚══════════════════════════════════════════════════════════════╝
"""


def parse_args() -> tuple[bool, bool]:
    """Parse CLI flags."""
    args = set(sys.argv[1:])
    dry_run = bool(args & {"--dry-run", "-n"})
    skip_confirm = bool(args & {"--yes", "-y"})
    if args - {"--dry-run", "-n", "--yes", "-y"}:
        print(f"Unknown flags: {args - {'--dry-run', '-n', '--yes', '-y'}}")
        print("Usage: python migrate_github_to_gitlab.py [--dry-run|-n] [--yes|-y]")
        sys.exit(1)
    return dry_run, skip_confirm


def main() -> None:
    print(BANNER)

    dry_run, skip_confirm = parse_args()
    if dry_run:
        print("🧪 DRY RUN MODE: No changes will be made.\n")

    # ── Configuration ──
    config = Config()

    # Display config
    s = " (dry run)" if dry_run else ""
    print(f"📋 Configuration:{s}")
    print(f"   GitHub username:         {config.github_username}")
    print(f"   New GitHub org:          {config.new_github_org}")
    print(f"   GitLab instance:         {config.gitlab_url}")
    print(f"   GitLab namespace:        {config.gitlab_namespace}")
    target_desc = "GitLab (origin=gitlab, github=StellariumFoundation)" if config.origin_target == "gitlab" \
        else "GitHub (origin=StellariumFoundation, gitlab=GitLab)"
    print(f"   Remote strategy:         {target_desc}")
    print(f"   Local repo search path:  {config.local_repo_base_path or '(not set → skip local updates)'}")
    print()

    # ── Confirm ──
    if not skip_confirm:
        confirm = input("⚠️  This will migrate ALL your repos to GitLab. Continue? (yes/no): ").strip().lower()
        if confirm != "yes":
            print("Aborted.")
            sys.exit(0)

    # ── Fetch repos ──
    repos = get_all_github_repos(config.github_username, config.github_token)
    if not repos:
        print("No repositories found. Nothing to migrate.")
        sys.exit(0)

    # Separate archived repos
    active_repos = [r for r in repos if not r.get("archived")]
    archived_count = len(repos) - len(active_repos)
    if archived_count:
        print(f"ℹ️  Skipping {archived_count} archived repo(s).")
    if not active_repos:
        print("No active repositories to migrate.")
        sys.exit(0)

    # ── Migrate each repo ──
    total = len(active_repos)
    results: list[dict[str, Any]] = []

    for i, repo in enumerate(active_repos, 1):
        print(f"\n\n─── Repo {i}/{total} ───")
        result = migrate_repo(repo, config, dry_run=dry_run)
        results.append(result)

    # ── Summary ──
    print(f"\n\n{'='*60}")
    print("📊 MIGRATION SUMMARY")
    print(f"{'='*60}")

    status_counts: dict[str, int] = {}
    for r in results:
        s = r["import_status"]
        status_counts[s] = status_counts.get(s, 0) + 1

    print(f"  Total repos processed: {len(results)}")
    for status, count in sorted(status_counts.items()):
        icon = {"finished": "✅", "failed": "❌", "dry_run": "🧪",
                "trigger_failed": "❌", "already_exists": "⏭️",
                "already_exists_not_found": "❌", "not_started": "⏭️"}.get(status, "❓")
        print(f"  {icon}  {status}: {count}")

    if not dry_run:
        succeeded = status_counts.get("finished", 0)
        print(f"\n💡 Next steps:")
        if succeeded:
            print(f"  • Verify imported repos: {config.gitlab_url}/{config.gitlab_namespace}")
        remote_advice = (
            "git push origin <branch>" if config.origin_target == "gitlab"
            else "git push origin <branch> (GitHub) / git push gitlab <branch> (GitLab)"
        )
        print(f"  • Local remotes updated — to push: {remote_advice}")
        if succeeded != len(results):
            print(f"  • {len(results) - succeeded} repo(s) had issues — check the details above.")
        print(f"  • If you used SSH remotes before, you may want to manually convert "
              f"the new URLs to SSH format.")
    else:
        print(f"\n🧪 Dry run complete. Run again without --dry-run to execute.")


if __name__ == "__main__":
    main()
