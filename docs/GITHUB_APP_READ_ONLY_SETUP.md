# DevPilot GitHub App: read-only setup

This is the next identity milestone for DevPilot, owned by `Levi710`. It is
separate from Codex's GitHub connection. DevPilot validates the configured
App, then may mint a short-lived installation token in memory to list the
single installed repository. It does **not** clone, branch, push, create a
pull request, merge, deploy, or persist a GitHub credential.

## Create the App under `Levi710`

In GitHub: **Settings → Developer settings → GitHub Apps → New GitHub App**.

- Name: `DevPilot Local Read Only` (or another unique personal name).
- Homepage URL: your DevPilot repository URL or `http://127.0.0.1` while local.
- User authorization callback: leave empty.
- Setup URL: leave empty.
- Where installed: **Only on this account**.
- Webhooks: leave **inactive** until a later HTTPS tunnel/reverse-proxy phase.
  GitHub cannot deliver to `127.0.0.1` directly.

Select only these repository permissions:

- **Contents: Read-only**
- **Pull requests: Read-only**
- **Metadata: Read-only** (provided automatically)

Do not grant Actions, administration, deployments, environments, secrets,
variables, issues write access, or workflow access. Save the App, then install
it on **one selected repository only**, never all repositories.

## Generate and protect the private key

On the App settings page, generate a private key. Store the downloaded `.pem`
file outside `C:\Users\ayush\DevPilot`, for example in a user-only directory
such as `C:\Users\ayush\DevPilot-Secrets\`. Do not commit it, paste it into
chat, copy it into `.env`, or place it in any repository/workspace.

Record the numeric **App ID** from the App settings page and numeric
**Installation ID** after installing it on the chosen repository.

## Local configuration

Add these references—not the private-key content—to the ignored `.env` file:

```dotenv
DEVPILOT_GITHUB_APP_ENABLED=true
DEVPILOT_GITHUB_APP_ID=123456
DEVPILOT_GITHUB_INSTALLATION_ID=12345678
DEVPILOT_GITHUB_APP_PRIVATE_KEY_PATH=C:\\Users\\ayush\\DevPilot-Secrets\\devpilot-github-app.pem
```

After adding them, reply `GitHub App config ready`. DevPilot will perform only
a local prerequisite validation first. The next reviewed check creates a
short-lived JWT locally and performs only `GET https://api.github.com/app` to
confirm the App identity. It does not mint an installation token or access a
repository.

The subsequent access check mints a short-lived installation token in memory,
uses it only to list the installation's repositories, and refuses to continue
unless exactly one repository is visible. It does not save the token, clone the
repository, or make any GitHub write.

GitHub recommends minimum permissions and installation tokens for automations;
those tokens are repository-scoped and expire after one hour. [GitHub App best
practices](https://docs.github.com/en/apps/creating-github-apps/about-creating-github-apps/best-practices-for-creating-a-github-app)
[GitHub installation access tokens](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app)
