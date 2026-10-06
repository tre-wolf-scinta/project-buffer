"""FastAPI application factory.

Run with ``uvicorn project_buffer.web.app:create_app --factory``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from project_buffer.clock import utcnow
from project_buffer.config import get_settings
from project_buffer.logging_setup import configure_logging
from project_buffer.services import notifications
from project_buffer.services.container import Services, build_services
from project_buffer.services.conversations import sync_contacts
from project_buffer.web.deps import RedirectTo
from project_buffer.web.routes import (
    account,
    auth,
    drafts,
    export,
    health,
    inbox,
    messages,
    webhooks,
)
from project_buffer.web.security import SecurityHeadersMiddleware
from project_buffer.web.templating import WEB_DIR, render

logger = logging.getLogger(__name__)

_ERROR_TITLES = {
    403: "Request refused",
    404: "Page not found",
    405: "Page not found",
    422: "That request could not be understood",
    429: "Too many attempts",
}


def _startup_sync(services: Services) -> None:
    with services.session_factory() as session:
        sync_contacts(session, services.settings)
        session.commit()


def _watchdog_pass(services: Services) -> None:
    with services.session_factory() as session:
        notifications.notify_delayed_messages(session, services, utcnow())
        session.commit()


async def _watchdog(services: Services) -> None:
    """Independent of the worker: tells the owner when a message is stuck unprocessed."""
    while True:
        await asyncio.sleep(60)
        try:
            await run_in_threadpool(_watchdog_pass, services)
        except Exception as exc:
            logger.error("watchdog error: %s", type(exc).__name__)


def create_app(services: Services | None = None) -> FastAPI:
    if services is None:
        settings = get_settings()
        configure_logging(settings.log_level)
        services = build_services(settings)
    settings = services.settings

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task = None
        if settings.environment != "test":
            try:
                await run_in_threadpool(_startup_sync, services)
            except Exception as exc:
                # Usually "migrations not applied yet". Readiness will report it.
                logger.error("startup sync failed: %s", type(exc).__name__)
            task = asyncio.create_task(_watchdog(services))
        yield
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(
        title="Project Buffer", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.services = services
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.cookie_secure)
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    for module in (health, webhooks, auth, account, inbox, messages, drafts, export):
        app.include_router(module.router)

    @app.exception_handler(RedirectTo)
    async def _redirect(_request: Request, exc: RedirectTo) -> Response:
        return RedirectResponse(exc.location, status_code=303)

    def _error_page(request: Request, status_code: int, detail: str | None) -> Response:
        if request.url.path.startswith(("/webhooks/", "/health/")):
            return PlainTextResponse("error", status_code=status_code)
        title = _ERROR_TITLES.get(status_code, "Something went wrong")
        return render(
            request,
            None,
            "error.html",
            {"title": title, "detail": detail, "status_code": status_code},
            status_code=status_code,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
        detail = exc.detail if isinstance(exc.detail, str) and exc.status_code != 404 else None
        return _error_page(request, exc.status_code, detail)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, _exc: RequestValidationError) -> Response:
        # Malformed IDs in URLs and missing form fields land here.
        return _error_page(request, 404 if request.method == "GET" else 422, None)

    @app.exception_handler(Exception)
    async def _server_error(request: Request, exc: Exception) -> Response:
        logger.error("unhandled error on %s: %s", request.url.path, type(exc).__name__)
        return _error_page(request, 500, None)

    return app
