"""Record export."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, Form, Request, Response
from sqlalchemy.orm import Session

from project_buffer.clock import utcnow
from project_buffer.services import export as export_service
from project_buffer.services.container import Services
from project_buffer.web.deps import AuthContext, csrf_protect, get_db, get_services, require_owner
from project_buffer.web.templating import render

router = APIRouter(prefix="/export")


def _form(
    request: Request,
    db: Session,
    services: Services,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    today = utcnow().astimezone(services.settings.timezone).date()
    return render(
        request,
        db,
        "export.html",
        {
            "error": error,
            "default_start": (today - timedelta(days=30)).isoformat(),
            "default_end": today.isoformat(),
        },
        status_code=status_code,
    )


@router.get("")
def export_form(
    request: Request,
    _context: AuthContext = Depends(require_owner),
    db: Session = Depends(get_db),
    services: Services = Depends(get_services),
) -> Response:
    return _form(request, db, services)


@router.post("", dependencies=[Depends(csrf_protect)])
def export_download(
    request: Request,
    start_date: str = Form(""),
    end_date: str = Form(""),
    include_originals: str = Form(""),
    _context: AuthContext = Depends(require_owner),
    db: Session = Depends(get_db),
    services: Services = Depends(get_services),
) -> Response:
    try:
        start_day, end_day = date.fromisoformat(start_date), date.fromisoformat(end_date)
    except ValueError:
        return _form(
            request,
            db,
            services,
            error="Enter both dates as year-month-day, for example 2026-01-31.",
            status_code=400,
        )
    if end_day < start_day:
        return _form(
            request, db, services, error="The end date is before the start date.", status_code=400
        )
    tz = services.settings.timezone
    start = datetime.combine(start_day, time.min, tzinfo=tz)
    end = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=tz)
    now = utcnow()
    archive = export_service.build_export(
        db, services, start=start, end=end, include_originals=include_originals == "1", now=now
    )
    db.commit()
    filename = f"buffer-export-{start_day.isoformat()}-to-{end_day.isoformat()}.zip"
    return Response(
        content=archive,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
