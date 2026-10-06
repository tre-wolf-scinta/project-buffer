"""Initial schema.

Revision ID: 0001
Revises:
Create Date: 2026-10-06

Column types are spelled out with plain SQLAlchemy types so this file does not
change if the application's custom types do.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# Original message records are write-once. This trigger enforces that in the
# database itself, underneath the ORM guard in models.py.
PROTECT_ORIGINALS = """
CREATE FUNCTION protect_message_originals() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'messages rows cannot be deleted';
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.conversation_id IS DISTINCT FROM OLD.conversation_id
        OR NEW.direction IS DISTINCT FROM OLD.direction
        OR NEW.provider IS DISTINCT FROM OLD.provider
        OR NEW.from_number IS DISTINCT FROM OLD.from_number
        OR NEW.to_number IS DISTINCT FROM OLD.to_number
        OR NEW.body_ciphertext IS DISTINCT FROM OLD.body_ciphertext
        OR NEW.body_mac IS DISTINCT FROM OLD.body_mac
        OR NEW.raw_payload_ciphertext IS DISTINCT FROM OLD.raw_payload_ciphertext
        OR NEW.provider_metadata IS DISTINCT FROM OLD.provider_metadata
        OR NEW.num_media IS DISTINCT FROM OLD.num_media
        OR NEW.occurred_at IS DISTINCT FROM OLD.occurred_at
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'original message columns are immutable';
    END IF;
    IF OLD.provider_message_id IS NOT NULL
        AND NEW.provider_message_id IS DISTINCT FROM OLD.provider_message_id
    THEN
        RAISE EXCEPTION 'provider_message_id can only be set once';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER messages_protect_originals
    BEFORE UPDATE OR DELETE ON messages
    FOR EACH ROW EXECUTE FUNCTION protect_message_originals();

CREATE FUNCTION forbid_change() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% rows are append-only', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_events_append_only
    BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION forbid_change();

CREATE TRIGGER delivery_events_no_delete
    BEFORE DELETE ON delivery_events
    FOR EACH ROW EXECUTE FUNCTION forbid_change();

CREATE TRIGGER message_analyses_no_delete
    BEFORE DELETE ON message_analyses
    FOR EACH ROW EXECUTE FUNCTION forbid_change();

CREATE TRIGGER attachments_no_delete
    BEFORE DELETE ON attachments
    FOR EACH ROW EXECUTE FUNCTION forbid_change();
"""


def upgrade() -> None:
    op.create_table('audit_events',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('actor', sa.String(length=32), nullable=False),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('subject_type', sa.String(length=32), nullable=True),
    sa.Column('subject_id', sa.String(length=64), nullable=True),
    sa.Column('detail', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_events'))
    )
    op.create_index(op.f('ix_audit_events_action'), 'audit_events', ['action'], unique=False)
    op.create_index(op.f('ix_audit_events_occurred_at'), 'audit_events', ['occurred_at'], unique=False)
    op.create_index(op.f('ix_audit_events_subject_id'), 'audit_events', ['subject_id'], unique=False)
    op.create_table('auth_attempts',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('ip', sa.String(length=64), nullable=False),
    sa.Column('succeeded', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_auth_attempts'))
    )
    op.create_index(op.f('ix_auth_attempts_created_at'), 'auth_attempts', ['created_at'], unique=False)
    op.create_table('contacts',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('role', sa.String(length=32), nullable=False),
    sa.Column('display_name', sa.String(length=80), nullable=False),
    sa.Column('phone_number', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_contacts')),
    sa.UniqueConstraint('role', name=op.f('uq_contacts_role'))
    )
    op.create_table('media_blobs',
    sa.Column('storage_key', sa.String(length=120), nullable=False),
    sa.Column('ciphertext', sa.LargeBinary(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('storage_key', name=op.f('pk_media_blobs'))
    )
    op.create_table('users',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('username', sa.String(length=64), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('totp_secret_encrypted', sa.LargeBinary(), nullable=True),
    sa.Column('totp_enabled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('totp_last_counter', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('username', name=op.f('uq_users_username'))
    )
    op.create_table('worker_heartbeats',
    sa.Column('worker_id', sa.String(length=64), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('worker_id', name=op.f('pk_worker_heartbeats'))
    )
    op.create_table('conversations',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('contact_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['contact_id'], ['contacts.id'], name=op.f('fk_conversations_contact_id_contacts')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_conversations')),
    sa.UniqueConstraint('contact_id', name=op.f('uq_conversations_contact_id'))
    )
    op.create_table('recovery_codes',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('code_hash', sa.String(length=64), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_recovery_codes_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_recovery_codes')),
    sa.UniqueConstraint('code_hash', name=op.f('uq_recovery_codes_code_hash'))
    )
    op.create_index(op.f('ix_recovery_codes_user_id'), 'recovery_codes', ['user_id'], unique=False)
    op.create_table('sessions',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('csrf_token', sa.String(length=64), nullable=False),
    sa.Column('mfa_verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_sessions_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sessions')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_sessions_token_hash'))
    )
    op.create_index(op.f('ix_sessions_user_id'), 'sessions', ['user_id'], unique=False)
    op.create_table('messages',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('conversation_id', sa.Uuid(), nullable=False),
    sa.Column('direction', sa.String(length=32), nullable=False),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('provider_message_id', sa.String(length=64), nullable=True),
    sa.Column('from_number', sa.String(length=32), nullable=False),
    sa.Column('to_number', sa.String(length=32), nullable=False),
    sa.Column('body_ciphertext', sa.LargeBinary(), nullable=False),
    sa.Column('body_mac', sa.String(length=64), nullable=False),
    sa.Column('raw_payload_ciphertext', sa.LargeBinary(), nullable=True),
    sa.Column('provider_metadata', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('num_media', sa.Integer(), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('sender_status', sa.String(length=32), nullable=False),
    sa.Column('processing_status', sa.String(length=32), nullable=False),
    sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('delivery_status', sa.String(length=32), nullable=False),
    sa.Column('delivery_error_code', sa.String(length=16), nullable=True),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('urgency', sa.String(length=32), nullable=False),
    sa.Column('urgency_overridden', sa.Boolean(), nullable=False),
    sa.Column('requires_response', sa.Boolean(), nullable=False),
    sa.Column('read_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('handled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('in_reply_to_id', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], name=op.f('fk_messages_conversation_id_conversations')),
    sa.ForeignKeyConstraint(['in_reply_to_id'], ['messages.id'], name=op.f('fk_messages_in_reply_to_id_messages')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_messages')),
    sa.UniqueConstraint('provider', 'provider_message_id', name=op.f('uq_messages_provider'))
    )
    op.create_index('ix_messages_conversation_occurred', 'messages', ['conversation_id', 'occurred_at'], unique=False)
    op.create_index('ix_messages_triage', 'messages', ['direction', 'handled_at', 'urgency'], unique=False)
    op.create_table('attachments',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('message_id', sa.Uuid(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('provider_media_url', sa.Text(), nullable=False),
    sa.Column('content_type', sa.String(length=120), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('storage_key', sa.String(length=120), nullable=True),
    sa.Column('size_bytes', sa.Integer(), nullable=True),
    sa.Column('content_mac', sa.String(length=64), nullable=True),
    sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['message_id'], ['messages.id'], name=op.f('fk_attachments_message_id_messages')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_attachments')),
    sa.UniqueConstraint('message_id', 'position', name=op.f('uq_attachments_message_id'))
    )
    op.create_index(op.f('ix_attachments_message_id'), 'attachments', ['message_id'], unique=False)
    op.create_table('drafts',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('conversation_id', sa.Uuid(), nullable=False),
    sa.Column('in_reply_to_id', sa.Uuid(), nullable=True),
    sa.Column('instruction', sa.Text(), nullable=False),
    sa.Column('ai_draft_text', sa.Text(), nullable=True),
    sa.Column('ai_notes', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('sent_message_id', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], name=op.f('fk_drafts_conversation_id_conversations')),
    sa.ForeignKeyConstraint(['in_reply_to_id'], ['messages.id'], name=op.f('fk_drafts_in_reply_to_id_messages')),
    sa.ForeignKeyConstraint(['sent_message_id'], ['messages.id'], name=op.f('fk_drafts_sent_message_id_messages')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_drafts'))
    )
    op.create_index(op.f('ix_drafts_in_reply_to_id'), 'drafts', ['in_reply_to_id'], unique=False)
    op.create_index(op.f('ix_drafts_status'), 'drafts', ['status'], unique=False)
    op.create_table('message_analyses',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('message_id', sa.Uuid(), nullable=False),
    sa.Column('is_current', sa.Boolean(), nullable=False),
    sa.Column('llm_provider', sa.String(length=32), nullable=False),
    sa.Column('llm_model', sa.String(length=64), nullable=False),
    sa.Column('prompt_version', sa.String(length=16), nullable=False),
    sa.Column('short_summary', sa.Text(), nullable=False),
    sa.Column('topic', sa.String(length=120), nullable=False),
    sa.Column('structured', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('search_text', sa.Text(), nullable=False),
    sa.Column('safety_keywords_detected', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['message_id'], ['messages.id'], name=op.f('fk_message_analyses_message_id_messages')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_message_analyses'))
    )
    op.create_index(op.f('ix_message_analyses_message_id'), 'message_analyses', ['message_id'], unique=False)
    op.create_table('notifications',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('message_id', sa.Uuid(), nullable=True),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('dedupe_key', sa.String(length=120), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('provider_message_id', sa.String(length=64), nullable=True),
    sa.Column('delivery_status', sa.String(length=32), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['message_id'], ['messages.id'], name=op.f('fk_notifications_message_id_messages')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_notifications')),
    sa.UniqueConstraint('dedupe_key', name=op.f('uq_notifications_dedupe_key'))
    )
    op.create_index(op.f('ix_notifications_created_at'), 'notifications', ['created_at'], unique=False)
    op.create_index(op.f('ix_notifications_message_id'), 'notifications', ['message_id'], unique=False)
    op.create_index(op.f('ix_notifications_provider_message_id'), 'notifications', ['provider_message_id'], unique=False)
    op.create_table('processing_jobs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('message_id', sa.Uuid(), nullable=True),
    sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('dedupe_key', sa.String(length=120), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('max_attempts', sa.Integer(), nullable=False),
    sa.Column('run_after', sa.DateTime(timezone=True), nullable=False),
    sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('locked_by', sa.String(length=64), nullable=True),
    sa.Column('last_error', sa.String(length=200), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['message_id'], ['messages.id'], name=op.f('fk_processing_jobs_message_id_messages')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_processing_jobs')),
    sa.UniqueConstraint('dedupe_key', name=op.f('uq_processing_jobs_dedupe_key'))
    )
    op.create_index('ix_processing_jobs_claim', 'processing_jobs', ['status', 'run_after'], unique=False)
    op.create_index(op.f('ix_processing_jobs_message_id'), 'processing_jobs', ['message_id'], unique=False)
    op.create_table('delivery_events',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('message_id', sa.Uuid(), nullable=True),
    sa.Column('notification_id', sa.Uuid(), nullable=True),
    sa.Column('provider_message_id', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('error_code', sa.String(length=16), nullable=True),
    sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['message_id'], ['messages.id'], name=op.f('fk_delivery_events_message_id_messages')),
    sa.ForeignKeyConstraint(['notification_id'], ['notifications.id'], name=op.f('fk_delivery_events_notification_id_notifications')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_delivery_events'))
    )
    op.create_index(op.f('ix_delivery_events_message_id'), 'delivery_events', ['message_id'], unique=False)
    op.create_index(op.f('ix_delivery_events_notification_id'), 'delivery_events', ['notification_id'], unique=False)
    op.create_index(op.f('ix_delivery_events_provider_message_id'), 'delivery_events', ['provider_message_id'], unique=False)
    if op.get_bind().dialect.name == "postgresql":
        op.execute(PROTECT_ORIGINALS)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table, trigger in (
            ("attachments", "attachments_no_delete"),
            ("message_analyses", "message_analyses_no_delete"),
            ("delivery_events", "delivery_events_no_delete"),
            ("audit_events", "audit_events_append_only"),
            ("messages", "messages_protect_originals"),
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
        op.execute("DROP FUNCTION IF EXISTS forbid_change()")
        op.execute("DROP FUNCTION IF EXISTS protect_message_originals()")
    op.drop_index(op.f('ix_delivery_events_provider_message_id'), table_name='delivery_events')
    op.drop_index(op.f('ix_delivery_events_notification_id'), table_name='delivery_events')
    op.drop_index(op.f('ix_delivery_events_message_id'), table_name='delivery_events')
    op.drop_table('delivery_events')
    op.drop_index(op.f('ix_processing_jobs_message_id'), table_name='processing_jobs')
    op.drop_index('ix_processing_jobs_claim', table_name='processing_jobs')
    op.drop_table('processing_jobs')
    op.drop_index(op.f('ix_notifications_provider_message_id'), table_name='notifications')
    op.drop_index(op.f('ix_notifications_message_id'), table_name='notifications')
    op.drop_index(op.f('ix_notifications_created_at'), table_name='notifications')
    op.drop_table('notifications')
    op.drop_index(op.f('ix_message_analyses_message_id'), table_name='message_analyses')
    op.drop_table('message_analyses')
    op.drop_index(op.f('ix_drafts_status'), table_name='drafts')
    op.drop_index(op.f('ix_drafts_in_reply_to_id'), table_name='drafts')
    op.drop_table('drafts')
    op.drop_index(op.f('ix_attachments_message_id'), table_name='attachments')
    op.drop_table('attachments')
    op.drop_index('ix_messages_triage', table_name='messages')
    op.drop_index('ix_messages_conversation_occurred', table_name='messages')
    op.drop_table('messages')
    op.drop_index(op.f('ix_sessions_user_id'), table_name='sessions')
    op.drop_table('sessions')
    op.drop_index(op.f('ix_recovery_codes_user_id'), table_name='recovery_codes')
    op.drop_table('recovery_codes')
    op.drop_table('conversations')
    op.drop_table('worker_heartbeats')
    op.drop_table('users')
    op.drop_table('media_blobs')
    op.drop_table('contacts')
    op.drop_index(op.f('ix_auth_attempts_created_at'), table_name='auth_attempts')
    op.drop_table('auth_attempts')
    op.drop_index(op.f('ix_audit_events_subject_id'), table_name='audit_events')
    op.drop_index(op.f('ix_audit_events_occurred_at'), table_name='audit_events')
    op.drop_index(op.f('ix_audit_events_action'), table_name='audit_events')
    op.drop_table('audit_events')
