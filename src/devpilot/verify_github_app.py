"""Live verification script for DevPilot's read-only GitHub App identity.

Performs three strictly read-only checks:
1. Validates local configuration prerequisites outside the repository.
2. Mints a short-lived RS256 JWT in memory and calls GET /app to verify identity.
3. Mints a short-lived installation token in memory to verify that exactly
   one repository is installed, and resolves the current default branch SHA.

Does not clone, branch, push, write, or persist any credential.
"""

from __future__ import annotations

import sys
from pathlib import Path
from dotenv import load_dotenv

# Ensure devpilot package is importable
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src" / "devpilot"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from github_app_config import load_github_app_configuration, GitHubAppConfigurationError
from github_app_auth import (
    get_app_identity,
    verify_single_installation_repository,
    resolve_default_branch_sha,
    GitHubAppAuthenticationError,
)


def verify_github_app() -> int:
    """Execute live read-only verification checks."""
    print("=" * 60)
    print("DEVPILOT GITHUB APP LIVE READ-ONLY VERIFICATION")
    print("=" * 60)

    # 1. Local configuration check
    load_dotenv()
    print("\n[Step 1/3] Validating local configuration prerequisites...")
    try:
        config = load_github_app_configuration()
        if config is None:
            print("FAILED: DEVPILOT_GITHUB_APP_ENABLED is not enabled in .env.")
            return 1
        print("  - Configuration: Enabled")
        print(f"  - App ID: {config.app_id}")
        print(f"  - Installation ID: {config.installation_id}")
        print(f"  - Key Path: {config.private_key_path} (exists, outside repo)")
        print("PASS: Local prerequisites are valid.")
    except GitHubAppConfigurationError as exc:
        print(f"FAILED: Local configuration error: {exc}")
        return 1

    # 2. Live GitHub App Identity Check
    print("\n[Step 2/3] Calling GET https://api.github.com/app with RS256 JWT...")
    try:
        identity = get_app_identity(config)
        print("  - Verified App ID:", identity.app_id)
        print("  - App Slug:       ", identity.slug)
        print("  - App Name:       ", identity.name)
        print("PASS: GitHub App identity confirmed by GitHub API.")
    except GitHubAppAuthenticationError as exc:
        print(f"FAILED: GitHub App identity verification failed: {exc}")
        return 1

    # 3. Live Single Installation Repository Check
    print("\n[Step 3/3] Verifying installation access (strictly 1 repository)...")
    try:
        repo = verify_single_installation_repository(config)
        print("  - Repository ID:  ", repo.repository_id)
        print("  - Full Name:      ", repo.full_name)
        print("  - Node ID:        ", repo.node_id)
        print("  - Default Branch: ", repo.default_branch)

        sha = resolve_default_branch_sha(config, repo)
        print("  - Latest Commit:  ", sha)
        print("PASS: Single repository scope verified. Read-only access operational.")
    except GitHubAppAuthenticationError as exc:
        print(f"FAILED: Installation repository verification failed: {exc}")
        return 1

    print("\n" + "=" * 60)
    print("SUCCESS: All GitHub App read-only boundaries verified cleanly!")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(verify_github_app())
