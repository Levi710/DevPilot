"""Explicit configuration contracts for DevPilot's future self-hosted service.

This module deliberately does not turn the desktop application into a public
service.  It makes unsafe production configuration fail early, so the durable
task and GitHub-App phases have a stable, testable contract to build on.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlparse


class RuntimeMode(StrEnum):
    """Supported operating modes, ordered from local to self-hosted service."""

    DEVELOPMENT = "development"
    SELF_HOSTED_PRODUCTION = "self_hosted_production"


class ConfigurationError(ValueError):
    """Raised when a configuration would make a service unsafe to start."""


@dataclass(frozen=True)
class ProductionConfiguration:
    """Validated configuration required before production features are enabled.

    No GitHub private key, installation token, or webhook secret belongs in this
    object.  Their storage and retrieval is a later secret-manager integration.
    """

    mode: RuntimeMode
    api_token: str | None
    database_url: str | None
    worker_runtime: str | None

    @property
    def is_production(self) -> bool:
        return self.mode is RuntimeMode.SELF_HOSTED_PRODUCTION


def _required(value: str | None, variable: str) -> str:
    if not value or not value.strip():
        raise ConfigurationError(f"{variable} is required in self-hosted production mode.")
    return value.strip()


def _validate_postgres_url(database_url: str) -> None:
    parsed = urlparse(database_url)
    if parsed.scheme not in {"postgresql", "postgres"} or not parsed.hostname:
        raise ConfigurationError(
            "DEVPILOT_DATABASE_URL must be a PostgreSQL URL in self-hosted production mode."
        )


def validate_runtime_configuration(environ: dict[str, str] | None = None) -> ProductionConfiguration:
    """Validate the runtime mode at service startup.

    Kept as a named entry point so API startup and tests use the exact same
    fail-closed configuration path.
    """

    return load_production_configuration(environ)


def load_production_configuration(environ: dict[str, str] | None = None) -> ProductionConfiguration:
    """Load configuration without changing process state or reading secrets from disk."""

    values = os.environ if environ is None else environ
    raw_mode = values.get("DEVPILOT_RUNTIME_MODE", RuntimeMode.DEVELOPMENT.value).strip().lower()
    try:
        mode = RuntimeMode(raw_mode)
    except ValueError as exc:
        supported = ", ".join(item.value for item in RuntimeMode)
        raise ConfigurationError(f"DEVPILOT_RUNTIME_MODE must be one of: {supported}.") from exc

    api_token = values.get("DEVPILOT_API_TOKEN", "").strip() or None
    database_url = values.get("DEVPILOT_DATABASE_URL", "").strip() or None
    worker_runtime = values.get("DEVPILOT_WORKER_RUNTIME", "").strip() or None

    if mode is RuntimeMode.SELF_HOSTED_PRODUCTION:
        token = _required(api_token, "DEVPILOT_API_TOKEN")
        if len(token) < 32:
            raise ConfigurationError(
                "DEVPILOT_API_TOKEN must contain at least 32 characters in self-hosted production mode."
            )
        database_url = _required(database_url, "DEVPILOT_DATABASE_URL")
        _validate_postgres_url(database_url)
        worker_runtime = _required(worker_runtime, "DEVPILOT_WORKER_RUNTIME")
        if worker_runtime not in {"container", "microvm"}:
            raise ConfigurationError(
                "DEVPILOT_WORKER_RUNTIME must be 'container' or 'microvm' in self-hosted production mode."
            )

    return ProductionConfiguration(
        mode=mode,
        api_token=api_token,
        database_url=database_url,
        worker_runtime=worker_runtime,
    )
