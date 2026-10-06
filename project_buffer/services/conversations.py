"""The single co-parent conversation and its contacts, kept in sync with configuration."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from project_buffer.config import Settings
from project_buffer.domain.enums import ContactRole
from project_buffer.infrastructure.db.models import Contact, Conversation


def _sync_contact(session: Session, role: ContactRole, name: str, phone: str) -> Contact:
    contact = session.scalar(select(Contact).where(Contact.role == role))
    if contact is None:
        contact = Contact(role=role, display_name=name, phone_number=phone)
        session.add(contact)
    elif (contact.display_name, contact.phone_number) != (name, phone):
        contact.display_name, contact.phone_number = name, phone
    return contact


def ensure_conversation(session: Session, settings: Settings) -> Conversation:
    """Return the conversation, creating it and its contacts on first use."""
    conversation = session.scalar(select(Conversation))
    if conversation is not None:
        return conversation
    try:
        with session.begin_nested():
            coparent = _sync_contact(
                session,
                ContactRole.COPARENT,
                settings.coparent_display_name,
                settings.coparent_phone_number,
            )
            _sync_contact(
                session, ContactRole.OWNER, settings.owner_display_name, settings.owner_phone_number
            )
            session.flush()
            conversation = Conversation(contact_id=coparent.id)
            session.add(conversation)
            session.flush()
        return conversation
    except IntegrityError:
        # Another process created it first.
        existing = session.scalar(select(Conversation))
        if existing is None:
            raise
        return existing


def sync_contacts(session: Session, settings: Settings) -> None:
    """Apply renamed contacts or changed numbers from configuration at startup."""
    ensure_conversation(session, settings)
    _sync_contact(
        session,
        ContactRole.COPARENT,
        settings.coparent_display_name,
        settings.coparent_phone_number,
    )
    _sync_contact(
        session, ContactRole.OWNER, settings.owner_display_name, settings.owner_phone_number
    )
