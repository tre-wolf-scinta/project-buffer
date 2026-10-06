# ADR 0001 — MVP architecture

Status: accepted · Date: 2026-10-06

## Context

A private, single-user intermediary for co-parenting text messages. Inbound
messages reach a dedicated Twilio number, are archived exactly as received, and
are shown to the owner only as an AI-sanitized representation. Outbound messages
are drafted with AI help and sent only after explicit owner approval. The owner
is blind; the service must run without any home hardware.

## Decisions

### 1. One codebase, two processes, one database

- **Web**: FastAPI + Jinja2, server-rendered, a few lines of progressive
  JavaScript. Synchronous SQLAlchemy 2.x sessions in threadpool routes.
- **Worker**: a plain Python loop that claims jobs from Postgres.
- **PostgreSQL** is the only stateful component. No Redis, Celery, or S3.

Layering: `domain` (enums, Pydantic schemas) → `services` (use cases) →
`infrastructure` (DB, crypto, LLM, SMS, media adapters) → `web` (routes,
templates). External systems sit behind `Protocol` interfaces.

### 2. Ingest first, think later

The Twilio webhook validates the signature, writes the encrypted original and an
`analyze_message` job **in one transaction**, and returns empty TwiML. No LLM
call happens in the request. A unique `(provider, provider_message_id)`
constraint makes Twilio retries no-ops.

### 3. Postgres-backed job queue

`processing_jobs` rows are claimed with `SELECT … FOR UPDATE SKIP LOCKED`. A job
whose worker died is reclaimed after a visibility timeout. Failures retry with
exponential backoff up to `JOB_MAX_ATTEMPTS`. Handlers are idempotent.

### 4. Two records, never merged

- `messages.body_ciphertext` holds the exact original, AES-256-GCM encrypted,
  with the message ID as associated data. A Postgres trigger and an ORM guard
  reject updates to original columns and all deletes.
- `message_analyses` holds AI output. Reprocessing appends a new analysis and
  marks the old one non-current; nothing is overwritten.

The original is reachable only via an explicit warning-then-confirm flow that is
audit-logged. Raw text never appears in list views, titles, accessible names,
notifications, search snippets, or logs.

### 5. LLM output is untrusted too

The message is passed as delimited data with a per-request random boundary; the
model's only output channel is one forced tool call validated by a strict
Pydantic schema. Deterministic guards then run on the result:

- profanity screen (retry, then fail closed);
- verbatim-run guard (a long word-for-word copy of the original fails closed);
- safety keyword tripwire on the original that can only *raise* urgency.

If analysis fails for good, the owner gets a neutral "received, filtering
failed" notice and a retry button. The raw text is never shown automatically.

### 6. Outbound is at-most-once and owner-approved

Draft → edit → review page showing the exact text → "Approve and send". The
approve request carries a digest of the reviewed text; the draft row is flipped
`ready → approved` atomically, so a double-click sends once. The outbound row is
committed as `sending` *before* calling Twilio. A definite rejection becomes
`failed`; an ambiguous network failure becomes `uncertain` and a reconcile job
looks the message up at Twilio rather than re-sending.

### 7. Unrecognised senders are quarantined, not dropped

A message from any number other than `COPARENT_PHONE_NUMBER` is stored encrypted
with status `quarantined`, is not sent to the LLM, and triggers at most one
neutral notice per hour. The owner can choose to process it. Rationale: the
co-parent may text from another phone; silently discarding it would lose a
parenting message. Texts from `OWNER_PHONE_NUMBER` (replies to notifications)
are not stored and get a short "use the app" auto-reply.

### 8. Media stored in Postgres

MMS attachments are downloaded by a job and stored encrypted in `media_blobs`
behind a `MediaStorage` interface. This keeps attachments in the same backup as
the messages and avoids another service. An S3/R2 adapter can replace it later.
Attachments are treated as unfiltered content: same warning flow as originals.
The MVP does not run vision models on attachments.

### 9. Authentication

Single owner account created from the CLI. Argon2id passwords, mandatory TOTP
with recovery codes, server-side sessions (hashed token in a `__Host-` cookie),
synchronizer CSRF tokens plus Origin checks, DB-backed login throttling. TOTP
enrolment offers an `otpauth://` link and the text secret rather than relying on
a QR code.

### 10. Owner notifications and watchdog

Sanitized SMS summaries go to `OWNER_PHONE_NUMBER` via a `notify_owner` job,
deduplicated by `(message_id, kind)`. Emergencies repeat until read. A watchdog
in the **web** process sends a neutral "processing delayed" notice if a message
sits unprocessed, so a dead worker cannot silently hide a message.
`/health/worker` exposes worker heartbeat staleness for an external monitor.

### 11. Deployment

Render: always-on web service, background worker, managed Postgres, defined in
`render.yaml`. Migrations run as the web service's pre-deploy command.

## Consequences

- SQLite is supported for tests and quick local runs; Postgres-only features
  (SKIP LOCKED, immutability trigger) are exercised when `TEST_DATABASE_URL`
  points at Postgres, as CI does.
- "Include originals" search decrypts and scans in Python. Fine at single-user
  volume; revisit past roughly 50k messages.
- Losing `RAW_MESSAGE_ENCRYPTION_KEY` makes originals unrecoverable. The launch
  checklist requires an offline copy.
