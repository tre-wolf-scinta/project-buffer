# Launch checklist

Do not block your co-parent's current route to your personal number until every
item in sections 1 to 6 is done. Until then, treat this as a system on trial.

## 1. Accounts and registration

1. Turn on two-factor sign-in for GitHub, Render, Twilio and the AI provider.
2. Decide whether the GitHub repository should be private. It is currently
   public. It holds no secrets and no real names, but it does describe how your
   setup works.
3. Buy the Twilio number. Start A2P 10DLC registration, or toll-free
   verification, straight away. Approval can take days.
4. Set a spending limit or alert on Twilio and on the AI provider.

## 2. Deploy

1. Generate keys with `python -m project_buffer.cli generate-keys`. Store the
   encryption key offline before using it.
2. Apply `render.yaml`. Confirm each resource is on a paid, always-on plan.
3. Add the secrets to the `buffer-config` environment group. Deploy both
   services.
4. Run `python -m project_buffer.cli check-config` in the Render shell.
5. Run `python -m project_buffer.cli create-owner`, sign in, set up two-step
   sign-in, save the recovery codes.
6. Confirm `/health/live`, `/health/ready` and `/health/worker` all answer ok.
7. Confirm database backups are on in the Render dashboard.

## 3. Check the AI before trusting it

1. In the Render shell, run `python scripts/eval_live.py`. Every line should
   say PASS. Read the printed summaries yourself and judge them.
2. If anything fails, do not launch. The prompts or model need work first.

## 4. Real-world acceptance test

Use a second phone as a stand-in for your co-parent: set
`COPARENT_PHONE_NUMBER` to that phone for the test.

1. Text a plain logistics message. A summary text reaches your phone within
   about a minute. The link opens the right message.
2. Text a hostile message with one real request in it. The summary carries the
   request and none of the hostility.
3. Text something with a safety word, such as "took him to the hospital". The
   notification says URGENT or EMERGENCY.
4. Reply from the app. The second phone receives exactly the approved text,
   once. The message page later says "Delivered".
5. Text from a third, unknown number. You get the neutral unrecognized-sender
   notice and the message waits in its own filter.
6. Send a picture message. The summary mentions an attachment. The file
   downloads from the original view.
7. AI outage: set `ANTHROPIC_API_KEY` to a wrong value, redeploy the worker,
   text a message. You get "automatic filtering failed" and no raw text. Restore
   the key, press "Retry filtering", and the summary appears.
8. Worker outage: suspend `buffer-worker`, text a message, wait six minutes.
   You get "filtering is delayed". Resume the worker; the summary follows.
9. Sign in on your phone over cellular data with Wi-Fi off. Open a message from
   a notification link.
10. Work through the manual screen-reader checks in `ACCESSIBILITY.md`.
11. Export the test period with originals included. Confirm the originals file
    has the exact texts you sent.
12. Set `COPARENT_PHONE_NUMBER` back to the real number and redeploy.

## 5. Monitoring

1. Point an outside uptime monitor at `/health/ready` and `/health/worker`.
   Send its alerts to email or another channel that does not depend on this app.
2. Know where to look when something seems wrong: Render logs for both services,
   and the Twilio console's message log.

## 6. Fallbacks and agreements

1. Agree a route for real emergencies that does not depend on this app, for
   example a phone call or a trusted third person. Software fails; a child's
   emergency should not wait on it.
2. Check with your attorney that routing communication through a new number is
   consistent with any custody order or parenting plan, especially one that
   names a required communication method.
3. Tell your co-parent the new number in whatever way your attorney advises.

## 7. After launch

1. For the first two weeks, open the original of a few messages each week, or
   ask someone you trust to, and compare with the summary. This is how you learn
   whether to trust the filter.
2. Watch for "filtering failed" notices. More than an occasional one means the
   prompt or the guards need tuning.
3. Export once a month and keep the file somewhere safe and separate.
4. Re-run `scripts/eval_live.py` whenever you change the model or the prompts.
