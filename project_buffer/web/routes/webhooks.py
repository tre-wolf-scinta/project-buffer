"""Twilio webhooks. Authenticated by request signature, not by session."""

from __future__ import annotations

import logging
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from project_buffer.clock import utcnow
from project_buffer.services import delivery, ingestion
from project_buffer.services.container import Services
from project_buffer.web.deps import get_db, get_services

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks/twilio")

EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def _twiml(body: str = EMPTY_TWIML, status_code: int = 200) -> Response:
    return Response(content=body, media_type="text/xml", status_code=status_code)


async def signed_params(
    request: Request, services: Services = Depends(get_services)
) -> dict[str, str] | None:
    """The form parameters if the Twilio signature is valid, otherwise None.

    The signed URL is rebuilt from APPLICATION_BASE_URL because the platform
    terminates TLS, so the URL this process sees is not the one Twilio signed.
    """
    form = await request.form()
    params = {key: value for key, value in form.multi_items() if isinstance(value, str)}
    url = services.settings.application_base_url + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    signature = request.headers.get("x-twilio-signature", "")
    if not services.sms.validate_signature(url, params, signature):
        logger.warning("rejected webhook with invalid signature path=%s", request.url.path)
        return None
    return params


@router.post("/messages")
def inbound_message(
    params: dict[str, str] | None = Depends(signed_params),
    db: Session = Depends(get_db),
    services: Services = Depends(get_services),
) -> Response:
    """Fast ingestion only: persist the original, queue the work, acknowledge."""
    if params is None:
        return _twiml(status_code=403)
    result = ingestion.ingest_inbound_sms(db, services, params, utcnow())
    db.commit()
    if result.outcome == ingestion.IngestOutcome.INVALID:
        return _twiml(status_code=400)
    if result.outcome == ingestion.IngestOutcome.OWNER_IGNORED:
        link = escape(f"{services.settings.application_base_url}/inbox")
        return _twiml(
            '<?xml version="1.0" encoding="UTF-8"?><Response><Message>'
            f"This number does not accept replies. Open the app to respond: {link}"
            "</Message></Response>"
        )
    return _twiml()


@router.post("/status")
def delivery_status(
    params: dict[str, str] | None = Depends(signed_params),
    db: Session = Depends(get_db),
    services: Services = Depends(get_services),
) -> Response:
    if params is None:
        return _twiml(status_code=403)
    delivery.handle_status_callback(db, services, params, utcnow())
    db.commit()
    return _twiml()
