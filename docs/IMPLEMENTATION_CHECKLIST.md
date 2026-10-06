# Implementation checklist

In priority order. "Done" means implemented and covered by automated tests.
"Needs your accounts" means the code exists but has only run against fakes.

## Priority 1: never lose or expose a message

- Done: webhook persists the encrypted original before any AI work.
- Done: Twilio signature validation; forged and altered requests rejected.
- Done: duplicate webhooks are no-ops, including simultaneous ones.
- Done: originals immutable through the ORM and by database trigger.
- Done: original text absent from inbox, detail, search, titles, notifications
  and logs, each asserted by a test with a marker string.
- Done: opt-in original viewer with warning, confirmation and audit record.
- Done: unrecognized senders quarantined rather than dropped.

## Priority 2: reliable processing

- Done: Postgres job queue with skip-locked claims, backoff and reclaim after a
  worker crash.
- Done: analysis failure keeps the original, notifies neutrally, offers retry.
- Done: web-side watchdog notice when a message sits unprocessed.
- Done: worker heartbeat and `/health/worker`.

## Priority 3: trustworthy filtering

- Done: provider interface; Anthropic and OpenAI adapters; fake for development.
- Done: strict schema, lengths capped, extra fields forbidden.
- Done: message passed as delimited data; prompt forbids obeying it.
- Done: guards for abusive language and verbatim copying, failing closed.
- Done: safety keyword tripwire that can only raise urgency.
- Done: manual urgency override that survives reprocessing.
- Needs your accounts: `scripts/eval_live.py` against the real model.

## Priority 4: outbound with explicit approval

- Done: instruction, AI draft, manual edit, redraft, discard.
- Done: review page; send requires approval field, CSRF token and a digest of
  the reviewed text.
- Done: at-most-once sending, including six simultaneous approvals.
- Done: delivery callbacks, out-of-order tolerant; early callbacks adopted.
- Done: uncertain sends reconciled against the provider instead of re-sent.
- Done: owner notified when a sent message fails to deliver.
- Needs your accounts: a real send and a real delivery callback.

## Priority 5: accessible interface

- Done: server-rendered pages that work without JavaScript.
- Done: inbox with filters, message detail, timeline, search, drafts, export,
  account pages.
- Done: structural accessibility tests on every page state.
- Done: checked in a browser at desktop and phone widths.
- Not done: full manual pass with NVDA, VoiceOver and TalkBack.

## Priority 6: security

- Done: Argon2id passwords, mandatory authenticator code, recovery codes.
- Done: server-side sessions, secure cookies, CSRF, Origin checks, rate limits.
- Done: AES-256-GCM for originals, payloads, attachments, authenticator secret.
- Done: security headers and a strict Content-Security-Policy.
- Done: audit trail, append-only in Postgres.
- Done: production configuration refuses unsafe settings.

## Priority 7: notifications

- Done: sanitized summary texts with a link; link-only and off modes.
- Done: urgent and emergency prefixes; emergency reminders until read.
- Done: neutral notices for failed filtering, delayed filtering, unrecognized
  senders and failed delivery.

## Priority 8: attachments and export

- Done: attachment metadata, download from Twilio, encrypted storage in
  Postgres, download only from the original view.
- Done: ZIP export with originals and summaries in separate files.
- Not done: AI description of attachments.
- Not done: S3 or R2 storage adapter.

## Priority 9: deployment

- Done: `render.yaml`, migrations, health checks, CI workflow.
- Done: migration applied from an empty Postgres 16 database, drift check,
  downgrade and re-upgrade.
- Done: full test suite passing on both SQLite and Postgres 16.
- Needs your accounts: applying the Blueprint; Twilio number and registration.

## Later

- Push notifications as an alternative to text.
- Vision-model description of image attachments, behind the same guards.
- Re-encryption command for retiring an old key completely.
- Attorney-oriented export, for example a paginated PDF.
- Grouping the timeline by child or topic.
