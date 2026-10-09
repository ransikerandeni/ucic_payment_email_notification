# UCIC Notifications

Email for the UCIC conference system:

- **to participants** - a payment receipt after their payment settles, or a
  "payment unsuccessful" notice when it fails;
- **to the organising team** (0.5.0+) - every Help & Support request, and an
  alert for every failed payment, sent to addresses set in **UCIC Notification
  Settings**.

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

The cost is latency — a receipt arrives within one sweep interval (1 minute by
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

Updating never re-runs the install backfills, so receipts that are still
waiting are not lost or marked as sent. Patches for the new version (listed in
`ucic_notifications/patches.txt`) run once each, during `migrate`.

Run everything below from the **bench directory** (the folder containing
`apps/` and `sites/`), as the user that owns the bench (usually `frappe`), not
as root.

### 1. Back up first

```bash
bench --site <site> backup
```

The backup lands in `sites/<site>/private/backups/`.

### 2a. Update with `bench update` (simplest)

```bash
bench update --apps ucic_notifications
```

This pulls the app, runs `bench migrate` and rebuilds assets in one go. Add
`--reset` only if you have local edits in `apps/ucic_notifications` that you are
happy to throw away. Then go to step 3.

### 2b. Update by hand with `git pull`

Use this when you want to see each step, pull a specific branch, or when
`bench update` would also touch other apps you do not want to update now.

1. **Go into the app and check its state**

   ```bash
   cd apps/ucic_notifications
   git status
   git branch --show-current
   ```

   `git status` should say *nothing to commit, working tree clean*. If it lists
   changed files, someone edited the code on the server: keep them with
   `git stash`, or throw them away with `git checkout -- .` before pulling.

2. **Pull the latest code**

   ```bash
   git pull origin <branch>
   ```

   `<branch>` is the one shown by `git branch --show-current` (usually `main`).
   To switch to another branch or a tag instead:

   ```bash
   git fetch origin
   git checkout <branch-or-tag>
   git pull origin <branch-or-tag>
   ```

   Check you have the version you expected:

   ```bash
   cat ucic_notifications/__init__.py
   git log --oneline -3
   ```

3. **Back to the bench directory**

   ```bash
   cd ../..
   ```

4. **Install Python requirements** (only needed if `pyproject.toml`'s
   `dependencies` changed - this app has none today, so it is safe to skip,
   and harmless to run)

   ```bash
   bench setup requirements --python
   ```

5. **Migrate the site** - creates and updates DocTypes (e.g. **UCIC
   Notification Settings**), the custom fields on Payment Request, and runs any
   new patches

   ```bash
   bench --site <site> migrate
   ```

   Repeat for each site the app is installed on.

6. **Build the app's assets** - copies `public/` (the app logo) into
   `sites/assets` so Desk can serve it

   ```bash
   bench build --app ucic_notifications
   ```

   A DocType's own form script (`*.js` next to its JSON, such as the **Send
   Test Emails** button) is served from the DocType, not the build - it is the
   cache clear below that makes Desk pick up a new version.

7. **Clear the cache**

   ```bash
   bench --site <site> clear-cache
   bench --site <site> clear-website-cache
   ```

8. **Restart**, so the web server, background workers and scheduler load the
   new Python code

   - Production (supervisor):

     ```bash
     sudo supervisorctl restart all
     ```

     or, equivalently, `bench restart`.

   - Development: stop `bench start` (Ctrl+C) and run it again.

   Skipping this is the most common reason an update "did nothing": workers keep
   running the old code until they restart.

### 3. Verify

- `bench version` (or `bench --site <site> list-apps`) shows the new version
  of `ucic_notifications`.
- Desk has the forms you expect: **Receipt Email Template**, and from 0.5.0
  **UCIC Notification Settings**. Hard-refresh the browser (Ctrl/Cmd+Shift+R)
  if a form or button is missing.
- The scheduler is on: `bench --site <site> scheduler status` (if it is off,
  `bench --site <site> enable-scheduler`).
- No new errors in Desk → **Error Log**, or in `logs/worker.error.log` and
  `logs/scheduler.log`, after a minute or two.

Your edits in **Receipt Email Template** and the addresses in **UCIC
Notification Settings** are kept: updates only fill fields that are blank.

### Updating to 0.5.0 specifically

- `migrate` creates **UCIC Notification Settings** and the
  `staff_failure_alert_on` field on Payment Request, and the
  `stamp_existing_failures_for_staff_alerts` patch marks every payment that had
  already failed as handled - so the team is not alerted about old failures.
- No Server Script or Client Script needs pasting or changing.
- After restarting, fill in the form (see *Emails to the organising team*) and
  press **Send Test Emails**.

### Rolling back

```bash
cd apps/ucic_notifications
git log --oneline            # find the commit or tag you were on before
git checkout <previous-commit-or-tag>
cd ../..
bench --site <site> migrate
bench build --app ucic_notifications
bench --site <site> clear-cache
sudo supervisorctl restart all
```

Restore the backup only if the data itself needs undoing:

```bash
bench --site <site> restore sites/<site>/private/backups/<file>.sql.gz
```

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
its settings (Receipt Email Template, UCIC Notification Settings) but **does
not remove its custom fields** on Payment Request - `receipt_sent_on`,
`failure_notified_on` and `staff_failure_alert_on`. They are harmless: read
only, and nothing else uses them. To remove them, delete them in Desk under
**Customize Form → Payment Request**, or from the console:

```python
for field in ("receipt_sent_on", "failure_notified_on", "staff_failure_alert_on"):
	frappe.delete_doc("Custom Field", "Payment Request-" + field, ignore_missing=True)
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
| How often? | Every minute (`hooks.scheduler_events`) |

### Two independent guards against mass mail

1. `after_install` backfills everything already settled.
2. `sweep()` ignores anything older than `DEFAULT_MAX_AGE_DAYS` (7) whatever its
   receipt state.

Both are covered by tests, including a mutation check that each one actually
fails the suite when removed.

---

## Failed payments

A Payment Request that reaches **Failed** gets a "payment unsuccessful" email
(`receipts.send_failure_notice`), sent once, guarded by `failure_notified_on`
(clear it to re-send). Same recipient rules, same minute sweep, and the same
header, logo and colour as the receipt. Its wording is fixed in
`receipt_email.FAILURE_BODY`. Updating to 0.4.0 stamps every payment that
failed before the update as already handled, so nobody is emailed about an old
failure.

---

## Emails to the organising team (0.5.0+)

Set the addresses in Desk → **UCIC Notification Settings** (System Manager). A
blank address means "send nothing", so installing or updating changes nothing
until someone fills the form in. **Send Test Emails** mails a sample of each to
the saved addresses.

| Field | Gets |
|---|---|
| Support Email 1, Support Email 2 | Every **Help & Support** request from the app, the same email to both. Reply goes straight to the participant (`reply_to` is their address) |
| Payment Failure Email | One alert per **Failed** payment - conference pass and slot payments alike - within a minute of the failure |

**Help & Support** uses a `doc_events` `after_insert` hook on **Support
Request**. That works here, unlike for payments, because the
`save_support_request` Server Script creates the request with `insert()`. The
mail is enqueued *after commit* (so a rolled-back request is never mailed and a
slow mail server never slows the app), and the hook never raises - a mail
problem is logged to **Error Log** and the participant's request still saves.
The Server Script itself is unchanged.

**Payment failures** are found by a second minute sweep
(`staff_alerts.sweep`), guarded by its own `staff_failure_alert_on` field on
Payment Request - separate from the participant's `failure_notified_on`, so the
team is told even when the participant has no email address, and either email
can be re-sent alone (clear the field). Updating to 0.5.0 stamps every payment
that had already failed, so the team is only alerted about new failures. Note
that failures which happen *after* the update but *before* an address is set
are still picked up (up to 7 days old) once it is set.

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
| `staff_alerts.py` | Emails to the organising team: Help & Support requests and failed payments |
| `ucic_notifications/doctype/ucic_notification_settings/` | The Desk form holding the team's addresses, with Send Test Emails |
| `templates/emails/staff_alert.html` | The team email layout |
| `install.py` | The custom fields, and the one-time backfills |
| `api.py` | Re-send, preview, test email and manual sweep - all role-guarded |
| `hooks.py` | Scheduler entries, and one `doc_events` hook on Support Request. Deliberately none on Payment Request — see above |
| `templates/emails/payment_receipt.html` | The email layout. No `frappe.*` calls: all formatting happens in Python, where it is tested and where a missing helper cannot fail inside a scheduled job |
