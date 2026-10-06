# Project Buffer

A private, single-user buffer for co-parenting text messages.

Your co-parent texts an ordinary phone number. The application stores each
message exactly as received, then shows you a neutral, AI-filtered summary:
the logistics, requests, deadlines and allegations, without the abuse. You
reply by saying what you want to tell them; a calm draft is written for you,
and nothing is sent until you approve the exact text.

The original is never altered and never shown unless you deliberately ask for it.

This document is written to be read with a screen reader: no diagrams, no
tables, steps as numbered lists.

## Contents

1. How it works
2. What is and is not built
3. Local setup
4. Everyday commands
5. Configuration
6. Deploying to Render
7. Connecting Twilio
8. First sign-in
9. Operating it
10. Project layout
11. Further reading

## 1. How it works

Inbound:

1. Your co-parent texts the Twilio number.
2. Twilio calls `/webhooks/twilio/messages`. The request signature is checked.
3. The original is encrypted and saved, together with a job to process it, in
   one database transaction. Twilio gets its acknowledgement immediately. No AI
   call happens during the webhook.
4. The background worker picks up the job and asks the AI model for a strict,
   structured analysis. The message is passed as data, never as instructions.
5. Deterministic checks run on the model's output. Output containing abusive
   language, or a long word-for-word copy of the original, is rejected.
6. The sanitized analysis is saved in its own table. You get a text with a short
   summary and a link.

Outbound:

1. You open a message and say what you want to tell them, typed or dictated.
2. A draft is written. You can edit it or have it rewritten.
3. A review page shows the exact text. You press "Approve and send".
4. The text goes out through Twilio once. Delivery updates appear on the message.

If something breaks:

- AI provider down: the original is already saved. The job retries with backoff.
  If it still fails you get a neutral "received, filtering failed" text and a
  retry button. The raw text is not shown.
- Worker down: the web process notices a message sitting unprocessed and texts
  you a neutral "received, filtering delayed" notice.
- Twilio retries a webhook: the duplicate is recognised by its message ID.
- You double-click Send: the second click does nothing.
- A send times out: the app asks Twilio whether the message exists rather than
  sending it again.

## 2. What is and is not built

Built and covered by automated tests:

- Twilio ingestion with signature validation, sender checks and idempotency.
- Encrypted, immutable originals, enforced by a database trigger in Postgres.
- Durable Postgres job queue with retries and crash recovery.
- AI analysis behind a provider interface, with Anthropic and OpenAI adapters.
- Accessible inbox, message detail, timeline, search, drafts, export.
- Explicit-approval outbound sending with delivery callbacks.
- Owner notifications by text, with repeat reminders for emergencies.
- Password plus authenticator-app sign-in, recovery codes, rate limiting.
- MMS attachments: downloaded, encrypted, stored in Postgres, downloadable only
  from the original-message view.
- Record export as a ZIP of JSON and CSV files.

Not built yet:

- The AI does not look inside attachments. A photo or screenshot is stored and
  reported as "attachment, not analyzed". Treat attachments as unfiltered.
- No push notifications. Notifications are text messages.
- No S3 or R2 storage adapter. The interface exists; attachments live in Postgres.
- No polished attorney-ready export. The ZIP export is complete but plain.
- No re-encryption command for key rotation. Old keys keep decrypting old rows.

Not yet verified, because it needs your accounts:

- The AI adapters have never called a real model. Run `scripts/eval_live.py`
  once you have an API key. This is the single most important pre-launch check.
- The Twilio adapter has never sent a real text.
- `render.yaml` has never been applied to a Render account.

## 3. Local setup

You need Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).

If the project folder is inside OneDrive, keep the virtual environment outside
it, or OneDrive will try to sync thousands of files. In PowerShell:

```powershell
$env:UV_PROJECT_ENVIRONMENT = "$HOME\.venvs\project-buffer"
```

Then:

1. Install dependencies.

   ```bash
   uv sync
   ```

2. Create your local configuration.

   ```bash
   cp .env.example .env
   ```

3. Generate secrets and paste the two lines it prints into `.env`.

   ```bash
   uv run python -m project_buffer.cli generate-keys
   ```

4. Create the database tables.

   ```bash
   uv run alembic upgrade head
   ```

5. Load fictional demo data. This also creates a demo sign-in.

   ```bash
   uv run python -m project_buffer.cli seed-demo
   ```

6. Start the web application, then open http://localhost:8000.

   ```bash
   uv run uvicorn project_buffer.web.app:create_app --factory --reload --port 8000
   ```

7. Sign in as `demo` with password `demo-password-123`. For the six-digit code:

   ```bash
   uv run python -m project_buffer.cli demo-code
   ```

The default `.env` uses SQLite, a fake AI provider, and a console SMS gateway
that sends nothing. To use local Postgres instead, run `docker compose up -d`
and set `DATABASE_URL=postgresql://buffer:buffer@localhost:5432/buffer`.

To simulate a text from your co-parent without Twilio:

```bash
uv run python -m project_buffer.cli simulate-inbound "Can you get Riley at 5 on Friday?"
```

## 4. Everyday commands

Run the tests:

```bash
uv run pytest
```

Run the tests against Postgres as well, including the Postgres-only tests for
the immutability trigger and concurrent workers. Point this at a disposable
database; the tests erase it.

```bash
TEST_DATABASE_URL=postgresql://buffer:buffer@localhost:5432/buffer_test uv run pytest
```

Lint, format and type-check:

```bash
uv run ruff check .
```

```bash
uv run ruff format .
```

```bash
uv run mypy project_buffer
```

Run the background worker:

```bash
uv run python -m project_buffer.worker
```

Create a migration after changing `models.py`, then review the generated file:

```bash
uv run alembic revision --autogenerate -m "describe the change"
```

Apply migrations:

```bash
uv run alembic upgrade head
```

After changing dependencies, regenerate the file Render installs from:

```bash
uv export --no-dev --no-hashes --no-emit-project --format requirements-txt -o requirements.txt
```

A `Makefile` wraps the same commands for systems that have `make`.

## 5. Configuration

All configuration is environment variables. `.env.example` lists every one with
a comment. The ones you must set for production:

- `ENVIRONMENT`: `production`. This turns on strict validation: https only,
  Postgres only, a real AI provider, Twilio required.
- `DATABASE_URL`: set automatically by Render.
- `APPLICATION_BASE_URL`: the public https address, with no trailing slash. It
  must match exactly, because Twilio signatures are checked against it.
- `SECRET_KEY`: generated by Render. Changing it signs every device out.
- `RAW_MESSAGE_ENCRYPTION_KEY`: from `generate-keys`. Keep an offline copy.
  If it is lost, stored originals cannot be read by anyone.
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`.
- `COPARENT_PHONE_NUMBER`, `OWNER_PHONE_NUMBER`: in +1 format. All three numbers
  must be different.
- `COPARENT_DISPLAY_NAME`, `OWNER_DISPLAY_NAME`, `CHILDREN_NAMES`: used in
  summaries and drafts. Real names go here, in the environment, never in the code.
- `LLM_PROVIDER`: `anthropic` or `openai`, with `ANTHROPIC_API_KEY` or
  `OPENAI_API_KEY`. `LLM_MODEL` is optional.
- `OWNER_TIMEZONE`: for example `America/New_York`.

Notification behaviour:

- `NOTIFY_MODE`: `summary` sends a short sanitized summary and a link.
  `link_only` sends only "New message" and a link. `off` sends nothing.
- `NOTIFY_EMERGENCY_REPEAT_MINUTES` and `NOTIFY_EMERGENCY_MAX_REPEATS`: an
  emergency is re-sent until you open it.
- `PROCESSING_DELAY_NOTICE_MINUTES`: how long a message may sit unprocessed
  before you get a neutral notice.

Check a configuration without starting anything:

```bash
uv run python -m project_buffer.cli check-config
```

## 6. Deploying to Render

`render.yaml` defines an always-on web service, a background worker and a
managed Postgres database. None of them is on a free plan; free services sleep,
and a sleeping service would miss texts. Creating them costs money, so nothing
here is automatic. You do each step.

1. Push this repository to GitHub. The repository contains no secrets and no
   real names, but consider making it private anyway.
2. In Render, choose New, then Blueprint, and select the repository. Review the
   three resources and their prices, then apply.
3. The first deploy will fail. That is expected: the secrets are not set yet.
4. Open Environment Groups, then `buffer-config`, and add the variables listed
   in the comment in `render.yaml`. Set `APPLICATION_BASE_URL` to the web
   service's address, for example `https://buffer-web-xxxx.onrender.com`.
5. Trigger a manual deploy of `buffer-web`, then of `buffer-worker`.
6. The web service runs `alembic upgrade head` before each deploy takes traffic.
7. Open the Shell tab of `buffer-web` and create your account:

   ```bash
   python -m project_buffer.cli create-owner
   ```

8. Check the three health addresses. Each should answer with status ok.
   - `/health/live`: the process is running.
   - `/health/ready`: the web process can reach the database. Render uses this.
   - `/health/worker`: the worker has checked in within the last two minutes.

Point an outside uptime monitor at `/health/ready` and `/health/worker`, with
alerts going somewhere other than this application.

Migration strategy: migrations are additive and run in the pre-deploy step.
A failed migration stops the deploy and leaves the old version running. Render
takes daily database backups on paid plans; confirm that in the dashboard.

## 7. Connecting Twilio

Twilio's console walks through these in order. As of October 2026:

1. Create a primary compliance profile. Choose "Individual Profile". It asks
   for your name as on your ID, email, phone and home address, and for consent
   to identity verification by Twilio's vendor. Twilio will not sell a number
   without it.
2. Buy a local phone number with SMS and MMS capability (about $1.15 a month).
3. Choose "Messaging" for the number and create a new messaging service. The
   number is assigned to it.
4. Register an A2P 10DLC brand. An individual profile becomes a Sole Proprietor
   brand ($4.50 one-time). Twilio texts a one-time code to your mobile; reply
   within 24 hours or the brand is not approved.
5. Once the brand is approved, register the A2P campaign: what the number is
   for, sample messages, and how recipients agreed to be texted ($15 vetting
   fee plus a small monthly fee). Describe the real use. Approval can take
   days. Until it is approved, sends can fail with error 30034.
6. Point incoming messages at the app. In the messaging service's Integration
   settings choose "Defer to sender's webhook", then in the phone number's
   messaging settings set "A message comes in" to a webhook, method POST,
   address `https://YOUR-ADDRESS/webhooks/twilio/messages`. If the messaging
   service is left on its default, Twilio ignores the number's own webhook.
7. Point voice calls at the app. In the phone number's voice settings set "A
   call comes in" to a webhook, method POST, address
   `https://YOUR-ADDRESS/webhooks/twilio/voice`. The number is text-only: a
   caller hears a short notice (`VOICE_GREETING`) and the call ends. Nothing is
   recorded or forwarded. The call is logged and you get a text saying who
   called and when. Without this step, callers hear Twilio's default demo
   message.
8. The campaign form asks for links to a privacy policy and to terms and
   conditions. The app serves both, publicly, at `/privacy` and `/terms`. Set
   `SMS_BRAND_NAME` to the registered brand name so the pages show it. Read both
   pages before giving the links to Twilio; anyone can open them.
9. Delivery callbacks need no setup. Each outgoing text carries its own callback
   address.
10. Texting STOP: if your co-parent texts STOP to the number, Twilio blocks your
   outgoing texts to them until they text START. The app shows this as a failed
   send with an explanation.

## 8. First sign-in

1. Go to the site and sign in with the username and password you created.
2. You are taken to two-step setup. On your phone, choose "Open in authenticator
   app". No QR code or camera is involved. If the link does nothing, the setup
   key is shown as text to type into the app.
3. Enter the six-digit code the app shows.
4. Save the ten recovery codes. They are shown once.
5. Add the site to your phone's home screen from the browser's share menu.

If you lose both the authenticator and the recovery codes, run this in the
Render shell, then sign in with your password and set it up again:

```bash
python -m project_buffer.cli reset-mfa
```

## 9. Operating it

- Unrecognized senders: a text from any number other than your co-parent's is
  saved but not processed. You get at most one neutral notice an hour. In the
  inbox, "Unrecognized senders" lets you process one if you recognize it.
- Replying to a notification text does nothing. The number answers with a short
  "use the app" message. Nothing you send that way reaches your co-parent.
- Urgency can be wrong. You can change it on any message. Anything that mentions
  hospitals, police, injuries and similar is raised to at least urgent
  automatically, even if the model disagreed.
- Viewing an original, downloading an attachment, searching originals and
  exporting are all recorded in the audit trail.
- Export: choose Export, pick dates. Leave "include originals" off unless you
  need them; with it on, the download contains the unfiltered messages in plain
  text.

## 10. Project layout

- `project_buffer/domain`: enumerations and the strict schemas for model output.
- `project_buffer/services`: the use cases. Ingestion, analysis, guards, jobs,
  notifications, drafts, outbound sending, delivery, auth, export.
- `project_buffer/infrastructure`: database models, encryption, and the adapters
  for AI providers, Twilio and attachment storage.
- `project_buffer/web`: routes, templates, static files.
- `project_buffer/worker.py`: the background worker.
- `project_buffer/cli.py`: operator commands.
- `migrations`: Alembic migrations.
- `tests`: the test suite. Nothing in it contacts Twilio or an AI provider.
- `scripts/eval_live.py`: checks the real AI provider against fictional messages.

## 11. Further reading

- `docs/adr/0001-mvp-architecture.md`: the design decisions and why.
- `docs/LAUNCH_CHECKLIST.md`: what to verify before relying on this.
- `docs/IMPLEMENTATION_CHECKLIST.md`: what was built, in priority order.
- `SECURITY.md`: threat model, what is and is not protected.
- `ACCESSIBILITY.md`: design commitments and manual screen-reader checks.
