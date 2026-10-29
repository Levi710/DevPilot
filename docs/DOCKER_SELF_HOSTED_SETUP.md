# Local Docker Desktop setup

This prepares the durable PostgreSQL dependency for DevPilot's future task and
webhook records. It does **not** expose DevPilot or PostgreSQL to the Internet,
does not install a GitHub App, and does not grant GitHub write access.

## Preconditions

- Docker Desktop is running with Linux containers enabled.
- The signed-in Windows user may access the Docker Desktop engine. If Docker
  reports an engine-pipe permission error, restart Docker Desktop and sign out/
  back in if Docker Desktop's setup requires it; do not run Docker as admin
  merely to bypass that boundary.

The Compose file pins the official PostgreSQL 16.15 Alpine 3.24 multi-platform
image digest reviewed on 2026-10-29. Treat image updates as a reviewed change:
inspect the replacement digest and security advisories, update the documented
version, then recreate and test the local database.

## Configure the local secrets

Create or update the ignored project-root `.env` file. Generate a unique
password locally; do not paste it into a GitHub issue, chat, source file, or
commit.

```powershell
$dbPassword = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 40 | ForEach-Object {[char]$_})
Add-Content .env "DEVPILOT_POSTGRES_PASSWORD=$dbPassword"
Add-Content .env "DEVPILOT_DATABASE_URL=postgresql://devpilot:$dbPassword@127.0.0.1:55432/devpilot"
```

Keep `DEVPILOT_RUNTIME_MODE=development` until the API is intentionally moved
behind an authenticated reverse proxy. Production mode still fails closed when
its API token, PostgreSQL URL, or worker runtime are absent.

## Build the local validation-worker image

The base `forge-sandbox:0.1` image intentionally contains only a minimal
Python test environment. Build DevPilot's derived worker image once after
reviewing `Dockerfile.worker`; it installs the pinned project requirements and
is the image used for isolated `pytest` validation.

```powershell
docker build --pull=false --tag devpilot-worker:0.1 --file Dockerfile.worker .
```

DevPilot resolves this tag to its immutable local image ID before each task.
It does not pull an image while executing a repository task. Treat rebuilding
the worker image as a reviewed local-administration action.

## Start and verify PostgreSQL

```powershell
docker compose up -d postgres
docker compose ps
docker compose logs --tail 50 postgres
```

`docker compose ps` must show `healthy`. PostgreSQL is bound only to
`127.0.0.1:55432`; it is not reachable from phones or other LAN devices. The
non-default port avoids colliding with an existing local PostgreSQL installation.

## Stop versus remove

```powershell
# Stops the local database and retains the named volume.
docker compose down

# Irreversibly destroys the local database. Use only when you intend to reset it.
docker compose down -v
```

## Next implementation gate

After Docker reports a healthy database, DevPilot can accept verified webhook
records into PostgreSQL. Automated branch, commit, and pull-request actions
remain intentionally unavailable until their explicit approval workflow is
implemented.
