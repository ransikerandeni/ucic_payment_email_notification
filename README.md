# UCIC Notifications

Participant-facing email for the UCIC conference system. Today that means one
thing: **a payment receipt**, emailed to the participant after their payment
settles.

It is a *receipt*, not a tax invoice — no Sales Invoice, no Customer, no Item,
no GL entries. The Payment Request already is the order, so the receipt is built
from it directly.

---

## Why this is an app, and why it sweeps

**Why an app.** The rest of this system's logic lives in Server Scripts pasted
into Desk. Those run inside Frappe's `safe_exec` sandbox, where `exec(code,
globals, {"doc": doc})` gives a comprehension its own scope that cannot see
script-level names — a trap that cost this project three separate `NameError`s
in production. An app has none of that: normal Python, real imports, real
templates, a real traceback, and a test suite that runs without a bench.

**Why it sweeps rather than hooking.** Both settlement paths mark a Payment
Request paid with `frappe.db.set_value` / `db_set` — direct database writes that
never run the document lifecycle. So:

- a Frappe **Notification** on "Value Change → Paid" would look configured in
  Desk and silently never fire;
- a **`doc_events`** hook in this app would not fire either.

Rather than reach back into those Server Scripts, a scheduled job finds Payment
Requests that are Paid but not yet receipted and emails them. **Nothing in
ERPNext has to be edited.** A send that fails is simply retried on the next
pass, instead of being lost inside a gateway callback.

The cost is latency — a receipt arrives within one sweep interval (15 minutes by
default) rather than instantly. For a receipt that is the right trade: it
records something that already happened, and the participant has already seen
the success screen.

---

## Install

### Requirements

- A working **Frappe / ERPNext bench** (Frappe >= 14) with an existing site.
- An **outgoing Email Account** on the site, or mail queues and never leaves.
- The **scheduler running** - the sweep is a cron job and receipts are queued,
  not sent inline.
- Git access to this repository, from the machine that runs the bench.

### Steps

Run these from the bench directory (the folder containing `apps/` and `sites/`).

1. **Get the app**

   ```bash
   bench get-app ucic_notifications https://github.com/ransikerandeni/ucic_payment_email_notification.git
   ```

   Add `--branch <name>` to install a branch or tag other than the default.

2. **Install it on the site**

   ```bash
   bench --site <site> install-app ucic_notifications
   ```

3. **Make sure the scheduler is on**

   ```bash
   bench --site <site> enable-scheduler
   ```

4. **Check it worked.** `bench --site <site> list-apps` should show
   `ucic_notifications`, and Desk should now have a **Receipt Email Template**
   form.

5. **Production only:** restart so workers load the new code.

   ```bash
   sudo supervisorctl restart all
   ```

   On a development bench, `bench start` picks it up.

### What `after_install` does

1. **Fills the Receipt Email Template** with the default wording (blanks only).
   A site that already had this app gets the same via a one-off patch on
   `bench migrate`.
2. **Creates `receipt_sent_on`** (Datetime, read only, allow-on-submit) on
   Payment Request. No Customize Form step.
3. **Backfills it on every already-settled payment.** This matters: the sweep
   looks for Paid requests with no `receipt_sent_on`, and on a site with
   existing payments every one of them would match on the first pass. Install
   draws a line - everything settled before that moment counts as handled, and
   the sweep only ever picks up what happens next.

`bench migrate` re-runs the field creation only. The backfill never runs again,
because on a live site that would stamp receipts that are legitimately still
waiting to go out.

---

## Update

Updating never re-runs the backfill, so receipts that are still waiting are not
lost or marked as sent.

1. **Back up first**

   ```bash
   bench --site <site> backup
   ```

2. **Pull the latest code**

   ```bash
   bench update --apps ucic_notifications
   ```

   This pulls the app, runs `bench migrate` (which syncs the DocType, re-ensures
   the `receipt_sent_on` field and runs any new patches) and rebuilds. Add
   `--reset` only if you have local edits you are happy to throw away.

   To do it by hand instead:

   ```bash
   cd apps/ucic_notifications && git pull && cd ../..
   bench --site <site> migrate
   sudo supervisorctl restart all
   ```

3. **Verify.** Check the version in `ucic_notifications/__init__.py` (or
   `bench version`), and confirm the next sweep runs without errors in
   **Error Log** / `logs/worker.error.log`.

Your edits in **Receipt Email Template** are kept: the seed only fills fields
that are blank.

To roll back, check out the previous tag or commit in `apps/ucic_notifications`,
run `bench --site <site> migrate`, and restart. Restore the backup only if the
data itself needs undoing.

---

## Uninstall

Take a backup first:

```bash
bench --site <site> backup
```

1. **Remove the app from the site**

   ```bash
   bench --site <site> uninstall-app ucic_notifications
   ```

   Receipts stop at once - the sweep is no longer scheduled. Bench asks for
   confirmation; add `--yes` to skip it.

2. **Remove the code from the bench** (optional, and only once no other site
   uses it)

   ```bash
   bench remove-app ucic_notifications
   ```

   Then restart in production: `sudo supervisorctl restart all`.

### What stays behind

This app has no uninstall hook, so uninstalling removes its DocTypes and
Receipt Email Template settings but **does not remove the `receipt_sent_on`
custom field** on Payment Request. It is harmless: the field is read only and
nothing else uses it. To remove it, delete it in Desk under **Customize Form →
Payment Request**, or from the console:

```python
frappe.delete_doc("Custom Field", "Payment Request-receipt_sent_on")
frappe.db.commit()
```

If you reinstall later, the backfill runs again and treats every payment
settled before that moment as already receipted.

---

## How it decides what to send

| Question | Answer |
|---|---|
| Which payments? | `status = Paid`, `docstatus = 1`, `party_type = Participant`, and a reference this app owns (Conference Schedule, **Conference** - the conference pass, or the two retired booking DocTypes) |
| To whom? | `Participant.email`, falling back to the Payment Request's `email_to` — the logged-in payer — so a payment is never left unreceipted |
| Slot or whole session? | A Payment Request naming a `slot_allocation_row` bought a slot, and the receipt shows that participant's own window. One naming none bought the whole session, and it shows the session's |
| Conference pass? | A Payment Request against a **Conference** is a conference pass (0.2.0+). The receipt names the package from `conference_package` (Day 1 / Both Days / Day 2) and, when the Conference has a Start Date, the dates it is valid on |
| How often? | Every 15 minutes (`hooks.scheduler_events`) |

### Two independent guards against mass mail

1. `after_install` backfills everything already settled.
2. `sweep()` ignores anything older than `DEFAULT_MAX_AGE_DAYS` (7) whatever its
   receipt state.

Both are covered by tests, including a mutation check that each one actually
fails the suite when removed.

---

## Editing the email

Desk → **Receipt Email Template** (a single form, System Manager / Accounts
Manager). It holds the *wording*; the app holds the *layout*.

| Field | What it controls |
|---|---|
| Subject | The subject line. Blank → default (`Payment receipt - <session>`) |
| Body | The message above the receipt details. Blank → default |
| Footer | Small print at the bottom. **Clear it to send no footer** |
| Header Title | Big text in the coloured header. Blank → the conference name |
| Logo | Shown in the header. Must be a **public** file |
| Brand Colour | Header band, dividers, links. Header text turns dark on a light colour |

Subject, Body and Footer take placeholders - `{{ participant }}`,
`{{ session_title }}`, `{{ amount_display }}` and the rest are listed on the
form itself. Values are HTML-escaped before they reach the organiser's text, so
a participant's name can never inject markup into an email.

**Preview** renders the form as it stands, saved or not, in an isolated frame.
**Send Test Email** mails a sample receipt from the *saved* template to you.

A template that does not render is refused on save. If one somehow gets through,
that part is logged ("Receipt Email Template: … did not render") and replaced by
its default for that send - an organiser's typo never costs a participant their
receipt.

The layout (`templates/emails/payment_receipt.html`) is nested tables and inline
styles, tested to hold up at phone width. Frappe wraps every email in its
standard template; tick **System Settings → Disable Standard Email Footer** to
drop the "Sent via ERPNext" line beneath it.

---

## Re-sending

`receipt_sent_on` is the send-once guard. **Clear it and the next sweep re-sends**
— which is also the answer when a participant says the email never arrived.

Or, from Desk / console:

```python
frappe.call("ucic_notifications.api.resend_receipt", payment_request="ACC-PRQ-2026-00007")
```

That endpoint checks read permission on the Payment Request *and* requires
System Manager or Accounts Manager. Receipts name a payer, a session and an
amount, so an unguarded "mail this to its owner" endpoint would leak all three
to any logged-in user who could guess a name.

To test the wiring without waiting for the cron:

```python
frappe.call("ucic_notifications.api.run_sweep")
```

---

## Tests

```bash
pip install -e ".[dev]"
pytest
```

The suite stubs `frappe` (`tests/frappe_stub.py`), so it runs with no bench, no
site and no MySQL. Templates are rendered with real Jinja, so the tests check the
HTML a participant actually receives. Only what the app actually calls is stubbed — if a change
starts using some other part of frappe, the tests fail loudly there rather than
quietly exercising a mock that agrees with everything.

---

## Layout

| File | What |
|---|---|
| `receipts.py` | Finding unreceipted payments, building the context, sending, sweeping |
| `receipt_email.py` | The editable wording (defaults, fallback, validation) and turning a context into subject + HTML |
| `ucic_notifications/doctype/receipt_email_template/` | The Desk form, with Preview and Send Test Email buttons |
| `install.py` | The custom field, and the one-time backfill |
| `api.py` | Re-send, preview, test email and manual sweep - all role-guarded |
| `hooks.py` | Scheduler entry. Deliberately **no** `doc_events` — see above |
| `templates/emails/payment_receipt.html` | The email layout. No `frappe.*` calls: all formatting happens in Python, where it is tested and where a missing helper cannot fail inside a scheduled job |
