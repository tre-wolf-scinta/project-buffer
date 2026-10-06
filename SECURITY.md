# Security

This application holds private family communications, some of them abusive.
This document says plainly what it protects, what it does not, and what you
have to do yourself.

## Threat model

Who might attack it, and what stops them:

1. Someone on the internet who finds the site.
   - Every page except sign-in and the health checks requires a full session:
     password and authenticator code.
   - Passwords are hashed with Argon2id. Sign-in is rate limited per address and
     overall. Wrong username and wrong password are indistinguishable.
   - Sessions are random tokens. Only a keyed hash is stored. Cookies are
     HttpOnly, Secure, SameSite=Lax and use the `__Host-` prefix.
   - Every form post needs a per-session CSRF token and a matching Origin.
   - Strict Content-Security-Policy with no inline script or style, no framing,
     no referrer, no caching of pages.

2. Someone who sends forged requests to the Twilio webhook.
   - Requests without a valid Twilio signature are rejected before anything is
     read or stored. A valid signature cannot be reused with different content.

3. The co-parent trying to manipulate the AI through message text.
   - The message is passed to the model as delimited data with a random boundary.
     The system prompt says it is never an instruction.
   - The model has one output channel: a single structured tool call validated
     against a strict schema that forbids extra fields and caps lengths.
   - The model cannot send messages, view other data, or take any action.
   - Output is then checked by code: abusive language or a long verbatim copy of
     the original is rejected. After a second rejection the message is marked
     "filtering failed" and you get a neutral notice.
   - The drafting model sees only the sanitized summary, never the original.

4. Someone who obtains a copy of the database, for example a leaked backup.
   - Original message text, the full provider payload, attachments and the
     authenticator secret are encrypted with AES-256-GCM. The key is not in the
     database. Each ciphertext is bound to its row, so it cannot be moved to
     another message.
   - Not encrypted: sanitized summaries, your own drafts, phone numbers,
     timestamps, the audit trail. A database copy reveals who texted whom and
     when, and what the summaries say.

5. Tampering with the record.
   - In Postgres a trigger rejects any update to original columns and any delete
     of a message, whatever issued it. The audit trail is append-only by trigger.
   - Each message stores a keyed digest of its original text. The export reports
     whether each original still matches.

## What this does not protect against

- Someone with access to your Render account. They can read the encryption key
  and the database together. Protect that account with its own two-factor
  sign-in. This is the most important thing on this page.
- The AI provider and Twilio. Message text is sent to the AI provider for
  analysis, and all texts pass through Twilio and the phone carriers. Review
  each provider's data retention terms. SMS itself is not private.
- Notification texts. Sanitized summaries are sent to your phone by SMS and may
  show on your lock screen. Set `NOTIFY_MODE=link_only` to send no content.
- A device you left signed in. Sessions last up to 30 days, or 7 days idle. Use
  "Sign out all other devices" on the Account page if a device is lost.
- A wrong summary. The AI can miss or misjudge things. That is why the original
  viewer, the manual urgency control and the safety keyword tripwire exist.
- Attachments. They are stored encrypted but are never filtered or analyzed.

## Keys and secrets

- `RAW_MESSAGE_ENCRYPTION_KEY` encrypts originals. Generate it with
  `python -m project_buffer.cli generate-keys`. Keep a copy offline, for example
  in a password manager. If it is lost, the originals are unreadable for good.
- To rotate it: set the new key as `RAW_MESSAGE_ENCRYPTION_KEY` and move the old
  one into `RAW_MESSAGE_ENCRYPTION_KEYS_OLD`. New data uses the new key; old
  rows still decrypt. Re-encrypting old rows is not implemented.
- `SECRET_KEY` keys the session-token hashes. Changing it signs everyone out.
- No secret is committed. `.env` is ignored by Git. `.env.example` holds
  placeholders only. Fixtures use invented names and reserved 555-01xx numbers,
  and a test fails if any other number appears in the code.

## Logging

The rule: logs carry identifiers and states, never content.

- Message bodies, prompts, model output, passwords, tokens and API keys are
  never logged. A test sends a marked message through ingestion, analysis,
  failure paths, the original viewer and search, and fails if the marker appears
  in any log line.
- Database errors have parameter values stripped.
- Errors from outside libraries are reduced to their class name.
- Phone numbers are masked to their last four digits.
- Search uses POST so search terms stay out of URLs and access logs.

## Audit trail

Recorded with a timestamp: message received, analyzed, failed, quarantined or
released; original viewed; attachment downloaded; originals searched; export
created; draft created or discarded; message approved, accepted, rejected or
undelivered; urgency changed; handled state changed; sign-in events; password
and two-step changes. Audit records never contain message text.

## Your part

1. Turn on two-factor sign-in for Render, GitHub, Twilio and the AI provider.
2. Use a long unique password here and store the recovery codes safely.
3. Keep an offline copy of the encryption key.
4. Restrict the database to Render's private network, as `render.yaml` does.
5. Set up outside monitoring of `/health/ready` and `/health/worker`.

## Reporting a problem

This is a private single-user project. If you find a security problem, tell the
repository owner privately rather than opening a public issue.
