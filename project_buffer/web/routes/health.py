"""Health endpoints. They reveal status only, never data."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from project_buffer.clock import utcnow
from project_buffer.infrastructure.db.models import WorkerHeartbeat
from project_buffer.services.container import Services
from project_buffer.web.deps import get_db, get_services

router = APIRouter(prefix="/health")


@router.get("/live")
def live() -> dict[str, str]:
    """The process is up. No dependencies are checked."""
    return {"status": "ok"}


@router.get("/ready")
def ready(db: Session = Depends(get_db)) -> JSONResponse:
    """The web process can reach its database. Used as the platform health check."""
    try:
        db.execute(text("SELECT 1"))
        db.execute(select(func.count()).select_from(WorkerHeartbeat))
    except SQLAlchemyError:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok"})


@router.get("/worker")
def worker(
    db: Session = Depends(get_db), services: Services = Depends(get_services)
) -> JSONResponse:
    """The background worker has checked in recently. For an external uptime monitor.

    Kept separate from /health/ready on purpose: a dead worker must not make the
    platform take the web service (and with it the Twilio webhook) out of rotation.
    """
    try:
        latest = db.scalar(select(func.max(WorkerHeartbeat.last_seen_at)))
    except SQLAlchemyError:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    stale = timedelta(seconds=services.settings.worker_stale_seconds)
    if latest is None or utcnow() - latest > stale:
        return JSONResponse({"status": "stale"}, status_code=503)
    return JSONResponse({"status": "ok"})
