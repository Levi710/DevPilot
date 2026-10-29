# DevPilot self-hosted production foundation

This document is the approved boundary for the first production build. It does
not make the existing desktop FastAPI process safe to expose to the Internet.

## Locked decisions

- DevPilot is a service identity, separate from Codex, owned by GitHub account
  `Levi710`.
- The first deployment is self-hosted on the owner's machine and remains
  loopback-only unless a later, reviewed design adds TLS and remote access.
- DevPilot may eventually create a PR, but the owner initially approves every
  PR, merge, deployment, rollback, and emergency stop.
- Version one supports a selected Python repository with an administrator
  authored dependency recipe and explicit test command.
- Merge and deployment are out of scope until the PR-only pilot has evidence.

## Trust boundary

Issue bodies, issue comments, pull-request text, repository files, READMEs,
test output, CI logs, package metadata, and model responses are **untrusted
evidence**. They cannot grant a capability, select a secret, choose a network
destination, override policy, or trigger deployment.

Only an authenticated owner action or a stored administrator-authored repository
policy may grant a task capability. The policy is evaluated outside the model.

## Phase 0 deliverables

1. Typed runtime validation in `production_config.py`.
2. Repository policy validation in `task_policy.py`.
3. A durable-task design that will replace synchronous investigation requests.
4. Tests proving unsafe production settings and write-capability requests fail.

## Required later components

The following are deliberately not represented by local `.env` files or the
current JSON repository registry:

- GitHub App private key and webhook secret, held in OS-protected secret storage.
- PostgreSQL task/audit database.
- Object storage for sanitized logs, diffs, and test artifacts.
- Disposable non-root Linux worker per task, with no host mounts, Docker socket,
  or default network egress.
- GitHub App installation tokens minted per task and scoped to one repository.

## Task lifecycle

`queued → policy_checked → worker_provisioning → checkout_ready → investigating
→ patch_validating → awaiting_approval | failed | cancelled`

The future orchestrator exclusively owns transitions. Each transition will use
an idempotency key so webhook retries and worker failures cannot duplicate work.

## First GitHub App phase

The owner must create and install the GitHub App on one selected repository.
Phase 1 uses only `Metadata: read`, `Contents: read`, `Issues: read`, and
`Pull requests: read`. It validates webhook signatures, deduplicates GitHub
delivery IDs, records an immutable base SHA, and performs a read-only checkout.
It has no branch, push, PR, merge, workflow, secret, or deployment permission.

`github_webhook.py` provides the pure signature-verification boundary now. It
does not expose a webhook endpoint yet: accepting a webhook requires a durable
PostgreSQL delivery-receipt store and queue, so a process restart cannot forget
that it has already received a delivery. This is intentionally blocked rather
than silently using in-memory deduplication.

The initial `task_store.py` now supplies the PostgreSQL schema and atomic
delivery-record primitive (`INSERT ... ON CONFLICT DO NOTHING`). It creates
read-only queued tasks and audit records only; it has no worker dispatch,
GitHub API client, token handling, or GitHub write operation.

The HTTP webhook receiver accepts only signed pull-request events with an
immutable base SHA, has a 1 MB body cap, and creates a queued read-only task
through that single atomic store method. It remains disabled until an explicit
webhook secret and `DEVPILOT_DATABASE_URL` are configured. It does not fetch
GitHub data, create branches, execute code, or perform any GitHub write.

GitHub App configuration is fail-closed and opt-in. It accepts only positive
numeric App/installation IDs and an existing absolute `.pem` path outside the
DevPilot repository. The configuration layer itself cannot read the key, mint
a JWT, call GitHub, clone code, or enable a write capability.

`GET /api/tasks/{task_id}` returns only a task summary and bounded audit events
from PostgreSQL. It has no worker-control action and follows the existing local
API token guard when one is configured.
