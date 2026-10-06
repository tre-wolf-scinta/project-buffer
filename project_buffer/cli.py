"""Operator commands. Run ``python -m project_buffer.cli --help``.

Prompts are plain text on standard input/output so they work with a screen reader.
"""

from __future__ import annotations

import argparse
import getpass
import secrets
import sys

from sqlalchemy import select

from project_buffer.clock import utcnow
from project_buffer.config import Settings, get_settings
from project_buffer.domain.phone import mask_phone
from project_buffer.infrastructure.crypto import generate_key
from project_buffer.infrastructure.db.models import User
from project_buffer.logging_setup import configure_logging
from project_buffer.services import audit, auth
from project_buffer.services.container import Services, build_services
from project_buffer.services.conversations import sync_contacts


def _services() -> Services:
    settings = get_settings()
    configure_logging(settings.log_level)
    return build_services(settings)


def _prompt_password() -> str:
    while True:
        first = getpass.getpass(f"New password (at least {auth.MIN_PASSWORD_LENGTH} characters): ")
        if len(first) < auth.MIN_PASSWORD_LENGTH:
            print("Too short. Try again.")
            continue
        if first != getpass.getpass("Repeat password: "):
            print("The two passwords did not match. Try again.")
            continue
        return first


def _refuse_in_production(settings: Settings, command: str) -> None:
    if settings.is_production:
        sys.exit(f"'{command}' is disabled when ENVIRONMENT=production.")


def cmd_generate_keys(_args: argparse.Namespace) -> None:
    print("Paste these into your secret store. They are not saved anywhere else.")
    print(f"SECRET_KEY={secrets.token_urlsafe(48)}")
    print(f"RAW_MESSAGE_ENCRYPTION_KEY={generate_key()}")
    print("Keep an offline copy of RAW_MESSAGE_ENCRYPTION_KEY. Without it, stored")
    print("original messages cannot be read.")


def cmd_check_config(_args: argparse.Namespace) -> None:
    settings = get_settings()
    print("Configuration is valid.")
    print(f"  environment:     {settings.environment}")
    print(f"  base url:        {settings.application_base_url}")
    print(f"  database:        {settings.database_url.split('://')[0]}")
    print(f"  sms provider:    {settings.sms_provider}")
    print(f"  llm provider:    {settings.llm_provider} ({settings.resolved_llm_model})")
    print(f"  twilio number:   {mask_phone(settings.twilio_phone_number)}")
    print(f"  co-parent:       {mask_phone(settings.coparent_phone_number)}")
    print(f"  owner:           {mask_phone(settings.owner_phone_number)}")
    print(f"  notify mode:     {settings.notify_mode}")
    print(f"  timezone:        {settings.owner_timezone}")


def cmd_create_owner(args: argparse.Namespace) -> None:
    services = _services()
    username = args.username or input("Username: ").strip()
    password = _prompt_password()
    with services.session_factory() as session:
        sync_contacts(session, services.settings)
        try:
            auth.create_owner(session, username, password)
        except auth.AuthError as exc:
            sys.exit(str(exc))
        session.commit()
    print(f"Owner account '{username.lower()}' created.")
    print("Sign in on the website now to set up two-step sign-in.")


def cmd_reset_password(_args: argparse.Namespace) -> None:
    services = _services()
    with services.session_factory() as session:
        user = session.scalar(select(User))
        if user is None:
            sys.exit("No owner account exists. Run create-owner first.")
        auth.set_password(session, user, _prompt_password(), actor="cli")
        session.commit()
    print("Password changed. All sessions were signed out.")


def cmd_reset_mfa(_args: argparse.Namespace) -> None:
    services = _services()
    with services.session_factory() as session:
        user = session.scalar(select(User))
        if user is None:
            sys.exit("No owner account exists.")
        user.totp_secret_encrypted = None
        user.totp_enabled_at = None
        user.totp_last_counter = None
        auth.revoke_all_sessions(session, user)
        audit.record(
            session, actor="cli", action="mfa_reset", subject_type="user", subject_id=user.id
        )
        session.commit()
    print("Two-step sign-in was removed. Sign in with your password to set it up again.")


def cmd_seed_demo(_args: argparse.Namespace) -> None:
    services = _services()
    _refuse_in_production(services.settings, "seed-demo")
    from project_buffer.seed import seed_demo

    with services.session_factory() as session:
        created = seed_demo(session, services, utcnow())
        session.commit()
    print(f"Demo data created: {created} messages.")
    print("Demo sign-in: username 'demo', password 'demo-password-123'.")
    print("For the 6-digit code, run: python -m project_buffer.cli demo-code")


def cmd_demo_code(_args: argparse.Namespace) -> None:
    _refuse_in_production(get_settings(), "demo-code")
    from project_buffer.seed import demo_totp_code

    print(demo_totp_code())


def cmd_simulate_inbound(args: argparse.Namespace) -> None:
    services = _services()
    _refuse_in_production(services.settings, "simulate-inbound")
    from project_buffer.services.ingestion import ingest_inbound_sms
    from project_buffer.worker import drain

    params = {
        "MessageSid": f"SMsim{secrets.token_hex(14)}",
        "From": services.settings.coparent_phone_number,
        "To": services.settings.twilio_phone_number,
        "Body": args.text,
        "NumMedia": "0",
    }
    with services.session_factory() as session:
        result = ingest_inbound_sms(session, services, params, utcnow())
        session.commit()
    print(f"Ingested: {result.outcome.value}, message id {result.message_id}")
    if not args.no_process:
        print(f"Processed {drain(services)} jobs.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="project_buffer.cli", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("generate-keys", help="print new random secrets").set_defaults(
        func=cmd_generate_keys
    )
    commands.add_parser("check-config", help="validate environment configuration").set_defaults(
        func=cmd_check_config
    )
    create = commands.add_parser("create-owner", help="create the single owner account")
    create.add_argument("--username")
    create.set_defaults(func=cmd_create_owner)
    commands.add_parser("reset-password", help="set a new owner password").set_defaults(
        func=cmd_reset_password
    )
    commands.add_parser(
        "reset-mfa", help="remove two-step sign-in so it can be set up again"
    ).set_defaults(func=cmd_reset_mfa)
    commands.add_parser(
        "seed-demo", help="load fictional demo data (not in production)"
    ).set_defaults(func=cmd_seed_demo)
    commands.add_parser(
        "demo-code", help="print the demo account's current 6-digit code"
    ).set_defaults(func=cmd_demo_code)
    simulate = commands.add_parser(
        "simulate-inbound", help="ingest a fake co-parent text (not in production)"
    )
    simulate.add_argument("text")
    simulate.add_argument("--no-process", action="store_true", help="leave jobs for the worker")
    simulate.set_defaults(func=cmd_simulate_inbound)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
