"""FastAPI REST layer exposing deterministic FinOpsLakehouse endpoints."""

from finops_lakehouse.api.app import app, create_app

__all__ = ["app", "create_app"]
