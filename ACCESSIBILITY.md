# Accessibility

The owner of this application is blind. Accessibility is a design constraint
here, not a finishing step. Target: WCAG 2.2 level AA, with NVDA on Windows and
VoiceOver or TalkBack on a phone as the reference screen readers.

## Honest status

- Automated structural checks: in place and passing (see below).
- Checked in a real browser: page structure through the accessibility tree,
  focus on load, the double-submit guard, and the 375-pixel phone layout.
- Not yet done: a full pass with NVDA, VoiceOver and TalkBack by a person. The
  checklists at the end of this file are for that. Automated checks cannot tell
  you whether a page is pleasant or efficient to use by ear.

## Design commitments

Structure:

- Server-rendered pages. Every action is an ordinary link or form button and
  works with JavaScript turned off.
- One `h1` per page. Headings never skip a level. In the inbox each message is
  an `article` named by its heading, so heading navigation moves message to
  message.
- Landmarks: one banner, one main, one footer. The two navigation regions are
  labelled "Main" and "Inbox filters".
- A "Skip to main content" link is the first focusable element on every page.
- The current page and current filter carry `aria-current`.

Names:

- No bare "Open", "View" or "Reply". Hidden text completes each name, for
  example "Reply to message about School transportation from Jordan, Today
  7:42 PM". A test fails if two controls on a page share a name but do different
  things.
- Every form field has a visible `label`. Hints are attached with
  `aria-describedby`. Sign-in fields carry `autocomplete` values so password
  managers and one-time-code autofill work.

Feedback:

- After an action the page title starts with the result, for example "Message
  sent.", because screen readers read the title first. Focus moves to the
  result message.
- Errors appear in an alert region at the top of the form, receive focus, and
  the field at fault is marked `aria-invalid`.
- Slow actions, such as drafting, announce "Writing a draft…" through a polite
  live region and ignore repeat presses.
- The inbox checks for new messages once a minute and announces "1 new message
  has arrived" with a reload link. It never changes the list under you.

No reliance on sight:

- Urgency is the first word of the heading: "Emergency: …", "Urgent: …". Colour
  and border are extra.
- "Unread", "Handled" and delivery status are words.
- Two-step sign-in setup uses a tappable link and a typed key. There is no QR
  code.
- Recovery codes are a list and also one text box for copying.

Protection from unwanted content:

- Original text never appears in a page title, heading, link name, button name,
  notification, or search result.
- The original is reachable only through a warning page and a second deliberate
  press. The page that shows it has a neutral title, "Original message".
- Attachments are never displayed inline. Each downloads only when chosen.

Touch and low vision:

- Buttons and links are at least 44 CSS pixels tall. Measured at 375 pixels
  wide, where there is also no sideways scrolling.
- Text is 18 pixels by default, scales with browser zoom, and reflows.
- Colours meet AA contrast in both light and dark schemes: text is at least
  6 to 1 against its background, borders and focus outlines at least 4.5 to 1.
  Focus has a 3-pixel outline. Nothing animates.

## Automated checks

`tests/test_accessibility.py` renders 30-plus page states and checks each for:

- `lang`, a meaningful title, a viewport tag;
- exactly one `main`, exactly one `h1`, a working skip link;
- heading levels that never skip;
- labelled, distinct navigation landmarks;
- unique `id` values, and `aria-labelledby` and `aria-describedby` pointing at
  ids that exist;
- a label on every form control and a name on every button and link;
- no two controls with the same name and different actions;
- no positive `tabindex`, no inline event handlers.

Run them with:

```bash
uv run pytest tests/test_accessibility.py
```

## Manual checks before launch

Do these on the deployed site with demo or real data. Each line is something to
confirm by ear.

### NVDA with Firefox or Chrome on Windows

1. Load the sign-in page. NVDA says "Sign in, Buffer". Tab reaches Username,
   Password, Sign in, in that order.
2. Sign in with a wrong password. Focus lands on "There is a problem" and the
   reason is read without searching for it.
3. Sign in correctly. The code field is announced as "6-digit code from your
   authenticator app".
4. In the inbox, press H repeatedly. You move through "Inbox", then each
   message heading. An urgent message starts with "Urgent" or "Emergency".
5. Press D to move between landmarks. You hear "Main navigation", "main",
   "Inbox filters navigation".
6. On one message, Tab through its actions. Each name says which message it
   belongs to.
7. Press "Mark handled". After the reload NVDA says "Marked as handled".
8. Open "View original". You hear the warning before any message text. Choose
   "Go back" and confirm no original text was read.
9. Reply to a message. After "Write a draft" you hear "Writing a draft". On the
   draft page the text box is labelled "Text to send".
10. Choose "Review before sending". The exact text is read as part of the
    "Approve and send" button's description.
11. Send. The title and first announcement are "Message sent."
12. Use Search. After submitting, focus lands on the results count heading.
13. Turn JavaScript off in the browser and repeat steps 4, 7 and 9 to 11.
    Everything still works; only the live announcements are missing.

### VoiceOver on iPhone or TalkBack on Android, over cellular data

1. Open the site from a notification text link. After sign-in you arrive at the
   message the link pointed to.
2. Use the rotor or reading controls to move by heading through the inbox.
3. Double-tap "Reply". Dictate into "What do you want to say?" with the
   keyboard's dictation key.
4. Confirm every button can be activated without precise aiming.
5. Add the site to the home screen and open it from there. It opens to the
   inbox, or to sign-in.
6. In authenticator setup, double-tap "Open in authenticator app" and confirm
   the app offers to add the account.

### Keyboard only, no screen reader

1. Tab from the top of each page. Focus is always visible and follows reading
   order.
2. No page traps focus.
3. Every action can be completed with Tab, Shift+Tab, Enter and Space.

## Known gaps

- The main navigation is seven links at the top of every page. The skip link
  bypasses it, but it is long on a phone.
- The date fields on the Export page use the browser's date control. Its
  screen-reader behaviour varies by browser. Typing the date as year, month, day
  works everywhere.
- Attachments are not described. A photo of a school notice is reported only as
  "1 attachment, not shown". Reading it needs sighted help or an OCR tool after
  download.
- Session expiry is not announced in advance. An expired session sends you to
  sign-in; a draft you saved is kept, but unsaved typing in a form is lost.
