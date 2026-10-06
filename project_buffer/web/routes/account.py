"""Account security: authenticator setup, recovery codes, password, sessions."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from project_buffer.clock import utcnow
from project_buffer.services import audit, auth
from project_buffer.services.container import Services
from project_buffer.web.deps import (
    AuthContext,
    csrf_protect,
    get_db,
    get_services,
    require_owner,
    require_password_session,
)
from project_buffer.web.templating import render

router = APIRouter(prefix="/account")


def _grouped(secret: str) -> str:
    return " ".join(secret[i : i + 4] for i in range(0, len(secret), 4))


def _setup_page(
    request: Request,
    services: Services,
    context: AuthContext,
    secret: str,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    return render(
        request,
        None,
        "mfa_setup.html",
        {
            "secret_grouped": _grouped(secret),
            "otpauth_uri": auth.provisioning_uri(context.user, secret),
            "error": error,
        },
        status_code=status_code,
    )


@router.get("/mfa/setup")
def mfa_setup_form(
    request: Request,
    context: AuthContext = Depends(require_password_session),
    db: Session = Depends(get_db),
    services: Services = Depends(get_services),
) -> Response:
    if context.user.mfa_enabled:
        return RedirectResponse("/account", status_code=303)
    secret = auth.pending_totp_secret(services.crypto, context.user)
    if secret is None:
        secret = auth.begin_totp_enrollment(db, services.crypto, context.user)
        db.commit()
    return _setup_page(request, services, context, secret)


@router.post("/mfa/setup", dependencies=[Depends(csrf_protect)])
def mfa_setup(
    request: Request,
    code: str = Form(""),
    context: AuthContext = Depends(require_password_session),
    db: Session = Depends(get_db),
    services: Services = Depends(get_services),
) -> Response:
    if context.user.mfa_enabled:
        return RedirectResponse("/account", status_code=303)
    now = utcnow()
    secret = auth.pending_totp_secret(services.crypto, context.user)
    if secret is None:
        return RedirectResponse("/account/mfa/setup", status_code=303)
    codes = auth.confirm_totp_enrollment(db, services.crypto, context.user, code, now)
    if codes is None:
        db.rollback()
        return _setup_page(
            request,
            services,
            context,
            secret,
            error="That code was not accepted. Enter the current 6-digit code from your "
            "authenticator app.",
            status_code=400,
        )
    context.session.mfa_verified_at = now
    db.commit()
    return render(request, db, "recovery_codes.html", {"codes": codes})


@router.get("")
def account_page(
    request: Request,
    context: AuthContext = Depends(require_owner),
    db: Session = Depends(get_db),
) -> Response:
    return render(
        request,
        db,
        "account.html",
        {"recovery_remaining": auth.remaining_recovery_codes(db, context.user), "error": None},
    )


def _account_error(request: Request, db: Session, context: AuthContext, error: str) -> Response:
    return render(
        request,
        db,
        "account.html",
        {"recovery_remaining": auth.remaining_recovery_codes(db, context.user), "error": error},
        status_code=400,
    )


def _password_ok(db: Session, context: AuthContext, password: str) -> bool:
    return auth.verify_password(db, context.user.username, password) is not None


@router.post("/password", dependencies=[Depends(csrf_protect)])
def change_password(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    context: AuthContext = Depends(require_owner),
    db: Session = Depends(get_db),
) -> Response:
    if not _password_ok(db, context, current_password):
        return _account_error(request, db, context, "Your current password is incorrect.")
    if new_password != confirm_password:
        return _account_error(request, db, context, "The two new passwords do not match.")
    try:
        context.user.password_hash = auth.hash_password(new_password)
    except auth.AuthError as exc:
        return _account_error(request, db, context, str(exc))
    auth.revoke_all_sessions(db, context.user, except_id=context.session.id)
    audit.record(
        db,
        actor="owner",
        action="password_changed",
        subject_type="user",
        subject_id=context.user.id,
    )
    db.commit()
    return RedirectResponse("/account?notice=password_changed", status_code=303)


@router.post("/recovery-codes", dependencies=[Depends(csrf_protect)])
def new_recovery_codes(
    request: Request,
    current_password: str = Form(""),
    context: AuthContext = Depends(require_owner),
    db: Session = Depends(get_db),
) -> Response:
    if not _password_ok(db, context, current_password):
        return _account_error(request, db, context, "Your current password is incorrect.")
    codes = auth.regenerate_recovery_codes(db, context.user)
    db.commit()
    return render(request, db, "recovery_codes.html", {"codes": codes})


@router.post("/mfa/reset", dependencies=[Depends(csrf_protect)])
def reset_mfa(
    request: Request,
    current_password: str = Form(""),
    context: AuthContext = Depends(require_owner),
    db: Session = Depends(get_db),
    services: Services = Depends(get_services),
) -> Response:
    """Start over with a new authenticator. Requires the password again."""
    if not _password_ok(db, context, current_password):
        return _account_error(request, db, context, "Your current password is incorrect.")
    auth.begin_totp_enrollment(db, services.crypto, context.user)
    audit.record(
        db, actor="owner", action="mfa_reset", subject_type="user", subject_id=context.user.id
    )
    db.commit()
    return RedirectResponse("/account/mfa/setup", status_code=303)


@router.post("/sessions/revoke", dependencies=[Depends(csrf_protect)])
def revoke_other_sessions(
    context: AuthContext = Depends(require_owner), db: Session = Depends(get_db)
) -> Response:
    auth.revoke_all_sessions(db, context.user, except_id=context.session.id)
    audit.record(db, actor="owner", action="other_sessions_revoked")
    db.commit()
    return RedirectResponse("/account?notice=sessions_revoked", status_code=303)
