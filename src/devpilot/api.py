
"""FastAPI endpoints for DevPilot."""

import hmac
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

try:
    from production_config import validate_runtime_configuration
except ImportError:
    from devpilot.production_config import validate_runtime_configuration

# Production mode is intentionally fail-closed. Development remains the
# default because the current desktop application has not yet migrated its
# database or worker execution to production services.
RUNTIME_CONFIGURATION = validate_runtime_configuration()

from agent import run_investigation
from repository_registry import (
    clone_public_github_repository,
    get_repository,
    list_repositories,
)
from investigation_store import (
    get_investigation,
    initialize_store,
    list_investigations,
    save_investigation,
)

try:
    from task_store import PostgresTaskStore, TaskStoreError
    from webhook_service import MAX_WEBHOOK_BODY_BYTES, WebhookIngressError, accept_pull_request_webhook, read_bounded_body
    from task_orchestrator import TaskOrchestrator, TaskOrchestratorError
    from task_policy import RepositoryPolicy, TaskMode
except ImportError:
    from devpilot.task_store import PostgresTaskStore, TaskStoreError
    from devpilot.webhook_service import MAX_WEBHOOK_BODY_BYTES, WebhookIngressError, accept_pull_request_webhook, read_bounded_body
    from devpilot.task_orchestrator import TaskOrchestrator, TaskOrchestratorError
    from devpilot.task_policy import RepositoryPolicy, TaskMode


# --------------------------------------------------
# Application setup
# --------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize persistent investigation storage on application startup."""
    initialize_store()
    yield

app = FastAPI(
    title="DevPilot API",
    version="0.2.0",
    lifespan=lifespan,
)

_API_TOKEN = os.getenv("DEVPILOT_API_TOKEN", "")
_GITHUB_WEBHOOK_SECRET = os.getenv("DEVPILOT_GITHUB_WEBHOOK_SECRET", "")
_TASK_DATABASE_URL = os.getenv("DEVPILOT_DATABASE_URL", "")
_LAN_ENABLED = os.getenv("DEVPILOT_LAN_ENABLED", "").strip().lower() == "true"
if _LAN_ENABLED and len(_API_TOKEN) < 32:
    raise RuntimeError(
        "DEVPILOT_LAN_ENABLED requires a DEVPILOT_API_TOKEN of at least 32 characters."
    )
_LOCAL_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]
_configured_origins = [
    origin.strip()
    for origin in os.getenv("DEVPILOT_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]

# By default only the local Vite development server is allowed.  Additional
# origins require an explicit environment configuration.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[*_LOCAL_ORIGINS, *_configured_origins],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def require_api_token(
    x_devpilot_token: str | None = Header(default=None),
) -> None:
    """Require the opt-in token for API actions when one is configured."""
    if _API_TOKEN and not hmac.compare_digest(x_devpilot_token or "", _API_TOKEN):
        raise HTTPException(status_code=401, detail="A valid DevPilot API token is required.")


# --------------------------------------------------
# Request models
# --------------------------------------------------

class InvestigationRequest(BaseModel):
    """Validate the data required to investigate a bug."""

    repository_id: str = Field(
        min_length=1,
        max_length=200,
    )

    description: str = Field(
        min_length=1,
        max_length=10000,
    )

    max_iterations: int = Field(
        default=5,
        ge=1,
        le=10,
    )


class PublicGitHubImportRequest(BaseModel):
    """Request body for cloning a public GitHub repository locally."""

    repository_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    github_url: str = Field(min_length=1, max_length=500)





# --------------------------------------------------
# Health check
# --------------------------------------------------

@app.get("/api/health")
def health():
    """Check whether the API is responding."""

    return {
        "status": "ok",
        "service": "DevPilot API",
    }


@app.post("/api/github/webhooks")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
):
    """Accept a bounded, signed, pull-request webhook into the durable queue.

    This endpoint does not call GitHub or execute a task. It is disabled unless
    explicit local configuration provides both a webhook secret and PostgreSQL.
    """

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_WEBHOOK_BODY_BYTES:
                raise HTTPException(status_code=413, detail="GitHub webhook body exceeds the 1 MB limit.")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header.") from exc
    try:
        body = await read_bounded_body(request.stream())
        result = accept_pull_request_webhook(
            body=body,
            signature_header=x_hub_signature_256,
            delivery_id=x_github_delivery,
            event_name=x_github_event,
            webhook_secret=_GITHUB_WEBHOOK_SECRET,
            store=PostgresTaskStore(_TASK_DATABASE_URL),
        )
    except WebhookIngressError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except Exception:
        # Never return database URLs, token fragments, payloads, or tracebacks.
        raise HTTPException(status_code=503, detail="GitHub webhook ingress is unavailable.")
    return {
        "status": result.status,
        "delivery_was_new": result.delivery_was_new,
        "task_id": result.task_id,
    }


@app.get("/api/tasks/{task_id}", dependencies=[Depends(require_api_token)])
def get_durable_task(task_id: str):
    """Read a bounded, non-secret task summary and its audit events."""
    if not _TASK_DATABASE_URL:
        raise HTTPException(status_code=503, detail="Durable task storage is not configured.")
    try:
        store = PostgresTaskStore(_TASK_DATABASE_URL)
        task = store.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Durable task was not found.")
        events = store.list_task_audit_events(task_id)
    except HTTPException:
        raise
    # api.py supports both top-level ``--app-dir`` and package imports. Catch
    # the stable RuntimeError base so either import identity of TaskStoreError
    # receives the same safe, non-diagnostic response.
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Durable task storage is unavailable.")
    return {
        "task": {
            "task_id": task.task_id,
            "source": task.source,
            "repository_node_id": task.repository_node_id,
            "base_sha": task.base_sha,
            "state": task.state,
            "created_at": task.created_at.isoformat(),
        },
        "audit_events": [
            # Metadata may contain delivery correlation data. It remains in the
            # database audit trail but is intentionally not an API response.
            {"event_id": event.event_id, "event_type": event.event_type,
             "created_at": event.created_at.isoformat()}
            for event in events
        ],
    }


@app.post("/api/tasks/{task_id}/process", dependencies=[Depends(require_api_token)])
def process_durable_task(task_id: str):
    """Process a queued durable task through its lifecycle steps."""
    if not _TASK_DATABASE_URL:
        raise HTTPException(status_code=503, detail="Durable task storage is not configured.")
    try:
        store = PostgresTaskStore(_TASK_DATABASE_URL)
        task = store.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Durable task was not found.")

        def _policy_lookup(node_id: str) -> RepositoryPolicy | None:
            return RepositoryPolicy(
                repository_node_id=node_id,
                allowed_paths=("src", "tests"),
                test_command=("pytest", "-q"),
                dependency_recipe="requirements.txt",
                allowed_mode=TaskMode.VALIDATE_PATCH,
            )

        orchestrator = TaskOrchestrator(
            task_store=store,
            policy_lookup=_policy_lookup,
        )
        updated_task = orchestrator.process_task(task_id)
        return {
            "task": {
                "task_id": updated_task.task_id,
                "state": updated_task.state,
                "source": updated_task.source,
                "repository_node_id": updated_task.repository_node_id,
                "base_sha": updated_task.base_sha,
                "created_at": updated_task.created_at.isoformat(),
            }
        }
    except HTTPException:
        raise
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Durable task processing is unavailable.")


# --------------------------------------------------
# Repository endpoints
# --------------------------------------------------

@app.get("/api/repositories", dependencies=[Depends(require_api_token)])
def repositories():
    """Return registered repositories and their availability."""

    try:
        return {
            "repositories": list_repositories(),
        }
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not load repositories: {exc}",
        ) from exc


@app.post(
    "/api/repositories/github-public",
    status_code=201,
    dependencies=[Depends(require_api_token)],
)
def import_public_github_repository(request: PublicGitHubImportRequest):
    """Clone and register a public GitHub repository without storing credentials."""
    try:
        repository = clone_public_github_repository(
            repository_id=request.repository_id,
            name=request.name,
            github_url=request.github_url,
        )
        return {"repository": repository}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# --------------------------------------------------
# Create and save an investigation
# --------------------------------------------------

@app.post("/api/investigations", dependencies=[Depends(require_api_token)])
def create_investigation(request: InvestigationRequest):
    """Run an investigation and persist the resulting report."""

    # Remove leading and trailing whitespace from the description.
    description = request.description.strip()

    if not description:
        raise HTTPException(
            status_code=400,
            detail="Describe the bug before starting an investigation.",
        )

    # Resolve the repository using the trusted local registry.
    try:
        repository = get_repository(request.repository_id)

        if repository is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Repository '{request.repository_id}' "
                    "is not registered."
                ),
            )

        if repository.get("status") != "connected":
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Repository '{request.repository_id}' "
                    "is unavailable."
                ),
            )

        # Run the existing AI investigation agent.
        result = run_investigation(
            repository_id=request.repository_id,
            task=description,
            max_iterations=request.max_iterations,
        )

    except HTTPException:
        raise

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    except RuntimeError as exc:
        # Save the failed investigation so history and database reflect the failure.
        try:
            save_investigation(
                repository_id=request.repository_id,
                repository_name=repository.get(
                    "name",
                    request.repository_id,
                ) if repository else request.repository_id,
                description=description,
                result={
                    "status": "failed",
                    "answer": f"Investigation failed: {exc}",
                    "evidence": getattr(exc, "evidence", []),
                    "iterations": getattr(exc, "iterations", None),
                },
            )
        except Exception:
            pass

        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc

    # Save the report using trusted repository metadata.
    try:
        saved = save_investigation(
            repository_id=request.repository_id,
            repository_name=repository.get(
                "name",
                request.repository_id,
            ),
            description=description,
            result=result,
        )

    except Exception as exc:
        # The investigation may have completed even if saving failed.
        raise HTTPException(
            status_code=500,
            detail=(
                "The investigation ran, but its report "
                f"could not be saved: {exc}"
            ),
        ) from exc

    # Return the saved report, including its ID and timestamp.
    return saved


# --------------------------------------------------
# Investigation history
# --------------------------------------------------

@app.get("/api/investigations", dependencies=[Depends(require_api_token)])
def investigations(
    limit: int = Query(
        default=50,
        ge=1,
        le=100,
    ),
):
    """Return recent saved investigations."""

    try:
        records = list_investigations(limit=limit)

        return {
            "investigations": records,
        }

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not load investigation history: {exc}",
        ) from exc


# --------------------------------------------------
# Retrieve one investigation
# --------------------------------------------------

@app.get("/api/investigations/{investigation_id}", dependencies=[Depends(require_api_token)])
def investigation_detail(investigation_id: int):
    """Return a saved investigation and its evidence."""

    record = get_investigation(investigation_id)

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="Investigation not found.",
        )

    return record
