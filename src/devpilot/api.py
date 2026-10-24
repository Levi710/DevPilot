
"""FastAPI endpoints for DevPilot."""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from agent import run_investigation
from repository_registry import get_repository, list_repositories
from investigation_store import (
    get_investigation,
    initialize_store,
    list_investigations,
    save_investigation,
)


# --------------------------------------------------
# Application setup
# --------------------------------------------------

app = FastAPI(
    title="DevPilot API",
    version="0.2.0",
)

# Allow requests from the local Vite development server.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


# --------------------------------------------------
# Startup
# --------------------------------------------------

@app.on_event("startup")
def startup() -> None:
    """Initialize persistent investigation storage."""

    initialize_store()


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


# --------------------------------------------------
# Repository endpoints
# --------------------------------------------------

@app.get("/api/repositories")
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


# --------------------------------------------------
# Create and save an investigation
# --------------------------------------------------

@app.post("/api/investigations")
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

@app.get("/api/investigations")
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

@app.get("/api/investigations/{investigation_id}")
def investigation_detail(investigation_id: int):
    """Return a saved investigation and its evidence."""

    record = get_investigation(investigation_id)

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="Investigation not found.",
        )

    return record
