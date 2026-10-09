"""Payment receipts for participants.

WHY THIS IS AN APP AND NOT A SERVER SCRIPT
------------------------------------------
The two settlement paths mark a Payment Request paid with
`frappe.db.set_value` / `db_set` - direct database writes that never run the
document lifecycle. So a Frappe Notification on "Value Change -> Paid" would
look configured in Desk and silently never fire, and a `doc_events` hook in
this app would not fire either.

Rather than reach back into those Server Scripts, this app SWEEPS: a scheduled
job finds Payment Requests that are Paid but not yet receipted, and emails
them. Nothing in ERPNext has to be edited, and a send that fails is simply
retried on the next pass instead of being lost inside a gateway callback.

The cost is latency - a receipt arrives within one sweep interval rather than
instantly. For a receipt that is the right trade: it is a record of something
that already happened, and the participant has already seen the success screen.
"""

import frappe

from ucic_notifications import receipt_email

# The Payment Request IS the order for every current sale - see
# create_gateway_payment. The two retired booking DocTypes are still listed
# because requests raised against them before the change are real purchases
# whose participants deserve a receipt just as much.
#
# "Conference" is the conference pass (Day 1 / Both Days / Day 2) - sold as a
# Payment Request against the Conference itself, with the package named in
# `conference_package`. See conference_pass/SETUP.md in the scripts repo.
RECEIPTABLE_REFERENCE_DOCTYPES = (
	"Conference Schedule",
	"Conference",
	"Slot Allocation",
	"Conference Session Payment",
)

SCHEDULE_DOCTYPE = "Conference Schedule"
CONFERENCE_DOCTYPE = "Conference"
SLOT_ROW_DOCTYPE = "Slot Allocations"

# Frappe prefixes a Custom Field added to a STANDARD DocType through Customize
# Form with `custom_`, while this app's own installer creates it unprefixed.
# Both spellings are accepted so it does not matter which way a site was set up.
RECEIPT_FIELD_CANDIDATES = ("receipt_sent_on", "custom_receipt_sent_on")
SLOT_ROW_FIELD_CANDIDATES = ("slot_allocation_row", "custom_slot_allocation_row")
FAILURE_FIELD_CANDIDATES = ("failure_notified_on", "custom_failure_notified_on")
PACKAGE_FIELD_CANDIDATES = ("conference_package", "custom_conference_package")
TRANSACTION_ID_FIELD_CANDIDATES = ("gateway_transaction_id", "custom_gateway_transaction_id")
PAYMENT_STATUS_FIELD_CANDIDATES = ("payment_status", "custom_payment_status")

# Payment Request statuses a failed checkout is left in. The gateway return
# script records a failure (a decline, or the payer pressing Cancel) on the
# request's `payment_status` field and leaves the standard `status` at
# "Requested", so the same request can be reused when they try again.
OPEN_STATUSES = ("Requested", "Initiated")

# What each Conference Package buys, split the way the conference pass Server
# Script splits it (CA_PACKAGE_DAYS / CA_TECH_PACKAGE_DAYS / CA_COMBO_PACKAGES -
# keep in step): `pass` days the participant may attend, `tech` days they may
# book Technical Session slots on. Day 1 is the conference's start date, Day 2
# the day after.
PACKAGES = {
	"Day 1": {"pass": (1,)},
	"Day 2": {"pass": (2,)},
	"Both Days": {"pass": (1, 2)},
	"Technical Sessions - Day 1": {"tech": (1,)},
	"Technical Sessions - Day 2": {"tech": (2,)},
	"Day 1 + Technical Sessions - Day 2": {"pass": (1,), "tech": (2,)},
}

# Which conference days each package covers, either way.
PACKAGE_DAYS = {
	package: tuple(sorted(set(parts.get("pass", ()) + parts.get("tech", ()))))
	for package, parts in PACKAGES.items()
}

# payment_type values - what kind of thing a Payment Request paid for. The
# emails choose their subject and wording by it.
TYPE_PASS = "Conference Pass"
TYPE_TECH = "Technical Sessions"
TYPE_PASS_AND_TECH = "Conference Pass + Technical Sessions"
TYPE_SLOT = "Session Slot"
TYPE_SESSION = "Session"

# How far back a sweep will look. This is a SAFETY RAIL, not a tuning knob:
# without it, installing this app on a site with years of paid requests would
# email every participant who has ever paid. `after_install` also backfills
# `receipt_sent_on` on everything already settled, so this is the second of two
# independent guards against that.
DEFAULT_MAX_AGE_DAYS = 7

# Per sweep, so one pass cannot flood the outgoing mail queue.
DEFAULT_LIMIT = 200


def _resolve_field(doctype, candidates):
	"""The real column for a logical field name, or None."""
	for candidate in candidates:
		if frappe.db.exists("DocField", {"parent": doctype, "fieldname": candidate}) or frappe.db.exists(
			"Custom Field", {"dt": doctype, "fieldname": candidate}
		):
			return candidate

	return None


def receipt_field():
	return _resolve_field("Payment Request", RECEIPT_FIELD_CANDIDATES)


def failure_field():
	return _resolve_field("Payment Request", FAILURE_FIELD_CANDIDATES)


def slot_row_field():
	return _resolve_field("Payment Request", SLOT_ROW_FIELD_CANDIDATES)


def package_field():
	return _resolve_field("Payment Request", PACKAGE_FIELD_CANDIDATES)


def transaction_id_field():
	return _resolve_field("Payment Request", TRANSACTION_ID_FIELD_CANDIDATES)


def payment_status_field():
	return _resolve_field("Payment Request", PAYMENT_STATUS_FIELD_CANDIDATES)


def outcome(payment_request):
	""""Paid", "Failed", or None while the payment is still open.

	Paid ONLY on the standard `status`: that is what settlement writes once the
	gateway has verified the money, and a receipt must never rest on anything
	weaker. Failed on either signal - the standard status, or the `payment_status`
	the gateway return script stamps on a decline or a cancel while leaving the
	request "Requested". Reading only `status` here is what once sent a receipt
	for a payment the participant had cancelled.
	"""
	status_field = payment_status_field()
	fields = ["status"] + ([status_field] if status_field else [])
	row = frappe.db.get_value("Payment Request", payment_request, fields, as_dict=True)

	if not row:
		return None

	if row.status == "Paid":
		return "Paid"

	if row.status == "Failed" or (
		row.status in OPEN_STATUSES and status_field and row.get(status_field) == "Failed"
	):
		return "Failed"

	return None


def pass_context(payment_request, conference):
	"""The pass-specific part of a receipt: which package, and which dates it covers."""
	field = package_field()
	package = frappe.db.get_value("Payment Request", payment_request, field) if field else None

	days_display = None
	start = frappe.db.get_value(CONFERENCE_DOCTYPE, conference, "start_date") if conference else None

	if start and package in PACKAGE_DAYS:
		days_display = ", ".join(
			frappe.utils.formatdate(frappe.utils.add_days(start, day - 1), "d MMMM yyyy")
			for day in PACKAGE_DAYS[package]
		)

	parts = PACKAGES.get(package) or {}
	pass_days = parts.get("pass", ())
	tech_days = parts.get("tech", ())

	def dates(days):
		if not (start and days):
			return None
		return _join(
			frappe.utils.formatdate(frappe.utils.add_days(start, day - 1), "d MMMM yyyy") for day in days
		)

	def day_names(days):
		return "Both Days" if tuple(days) == (1, 2) else _join("Day %d" % (day,) for day in days)

	if pass_days and tech_days:
		payment_type = TYPE_PASS_AND_TECH
		purchase_title = "Conference Pass (%s) + Technical Sessions (%s)" % (
			day_names(pass_days),
			day_names(tech_days),
		)
	elif tech_days:
		payment_type = TYPE_TECH
		purchase_title = "Technical Sessions (%s)" % (day_names(tech_days),)
	else:
		# A package this app does not know yet still reads as a pass - that is
		# what everything sold against the Conference was until now.
		payment_type = TYPE_PASS
		purchase_title = "Conference Pass (%s)" % (day_names(pass_days) if pass_days else package or "")
		purchase_title = purchase_title.replace(" ()", "")

	# The rows of the details table that say what the package covers.
	package_rows = []
	if pass_days:
		package_rows.append(("Conference pass", day_names(pass_days)))
		package_rows.append(("Valid on", dates(pass_days)))
	if tech_days:
		package_rows.append(("Technical sessions", day_names(tech_days)))
		package_rows.append(("Slot booking for", dates(tech_days)))
	if not parts and package:
		package_rows.append(("Conference pass", package))

	return {
		"pass_package": package,
		"session_title": (
			package if (package or "").startswith("Technical Sessions")
			else ("Conference Pass - %s" % (package,) if package else "Conference Pass")
		),
		"pass_days_display": days_display,
		"payment_type": payment_type,
		"purchase_title": purchase_title,
		"package_rows": package_rows,
		"pass_day_names": day_names(pass_days) if pass_days else None,
		"pass_dates": dates(pass_days),
		"tech_day_names": day_names(tech_days) if tech_days else None,
		"tech_dates": dates(tech_days),
	}


def _join(items):
	"""'a', 'a and b', 'a, b and c'."""
	items = [str(item) for item in items if item]
	if len(items) < 2:
		return "".join(items)
	return "%s and %s" % (", ".join(items[:-1]), items[-1])


def _with(text, value, template):
	return text + (template % (value,) if value else "")


def outcome_notes(context):
	"""(confirmation_note, failure_note): one sentence each on what the payment
	means for THIS purchase - a Day 2 pass, a Technical Sessions day, a slot.

	Plain text; the email escapes it. Built here rather than in the template so
	every kind of sale is covered by the tests, not just the one in a preview.
	"""
	c = context
	conference = c.get("conference")
	kind = c.get("payment_type")

	if kind == TYPE_PASS:
		days = c.get("pass_day_names")
		if days == "Both Days":
			which = _with("conference pass for both days", conference, " of %s")
		else:
			which = _with("%s conference pass" % (days,) if days else "conference pass", conference, " for %s")
		return (
			_with("Your %s is confirmed." % (which,), c.get("pass_dates"), " It is valid on %s."),
			"your %s has not been issued" % (which,),
		)

	if kind == TYPE_TECH:
		day = c.get("tech_day_names") or "the conference"
		on = (" on %s" % (c["tech_dates"],)) if c.get("tech_dates") else ""
		return (
			"Your Technical Sessions access for %s%s is confirmed. You can now book your time slots%s "
			"in the UCIC app." % (day, _with("", conference, " of %s"), on),
			"you cannot book Technical Session slots for %s yet" % (day,),
		)

	if kind == TYPE_PASS_AND_TECH:
		pass_part = _with("%s conference pass" % (c.get("pass_day_names"),), c.get("pass_dates"), " (valid on %s)")
		tech_part = _with("Technical Sessions access for %s" % (c.get("tech_day_names"),), c.get("tech_dates"), " (%s)")
		return (
			"Your %s and %s are confirmed. You can now book your %s time slots in the UCIC app."
			% (pass_part, tech_part, c.get("tech_day_names")),
			"your %s conference pass and %s Technical Sessions access have not been issued"
			% (c.get("pass_day_names"), c.get("tech_day_names")),
		)

	session = c.get("session_title") or "the session"
	when = _with("", c.get("session_date_display"), " on %s")

	if kind == TYPE_SLOT:
		slot = (
			", %s - %s" % (_hm(c["slot_from"]), _hm(c["slot_to"]))
			if c.get("slot_from") and c.get("slot_to")
			else ""
		)
		where = _with("", c.get("venue"), ", %s")
		return (
			"Your time slot in %s%s%s%s is confirmed." % (session, when, slot, where),
			"your time slot in %s has not been confirmed" % (session,),
		)

	where = _with("", c.get("venue"), " in %s")
	return (
		"Your place in %s%s%s is confirmed." % (session, when, where),
		"your place in %s has not been confirmed" % (session,),
	)


def _hm(value):
	"""10:15 from "10:15:00" or a timedelta - Frappe returns Time fields as either."""
	parts = str(value).split(":")
	try:
		return "%02d:%02d" % (int(parts[0]), int(parts[1]))
	except (ValueError, IndexError):
		return str(value)


def build_context(payment_request):
	"""Everything the template needs, or None when there is nobody to send to.

	Reads only - it decides nothing and writes nothing, which is what makes it
	straightforward to test.
	"""
	pr = frappe.db.get_value(
		"Payment Request",
		payment_request,
		["name", "party", "grand_total", "currency", "reference_doctype", "reference_name", "email_to"],
		as_dict=True,
	)

	if not pr:
		return None

	# The Participant record's own address first: that is the one the organisers
	# curate and the one on the registration. `email_to` - the logged-in payer -
	# is the fallback, so a payment is never left unreceipted just because a
	# Participant row has no email on it.
	recipient = None
	participant_name = None

	if pr.party:
		participant = frappe.db.get_value("Participant", pr.party, ["full_name", "email"], as_dict=True)
		if participant:
			recipient = participant.email
			participant_name = participant.full_name

	if not recipient:
		recipient = pr.email_to

	if not recipient:
		return None

	# A conference pass has no session at all - it names the Conference.
	is_pass = pr.reference_doctype == CONFERENCE_DOCTYPE

	# The session. A current sale references the Conference Schedule directly; a
	# legacy booking references a DocType that names one.
	schedule_name = None if is_pass else pr.reference_name

	if pr.reference_doctype in ("Slot Allocation", "Conference Session Payment"):
		schedule_name = frappe.db.get_value(pr.reference_doctype, pr.reference_name, "conference_schedule")

	schedule = {}
	if schedule_name and frappe.db.exists(SCHEDULE_DOCTYPE, schedule_name):
		schedule = (
			frappe.db.get_value(
				SCHEDULE_DOCTYPE,
				schedule_name,
				["session_title", "date", "start_time", "end_time", "hall", "conference"],
				as_dict=True,
			)
			or {}
		)

	# WHICH SLOT, where this bought one. A whole-session sale names no row, and
	# then the session's own window is what the participant bought.
	slot = None
	row_field = slot_row_field()

	if row_field:
		row_name = frappe.db.get_value("Payment Request", payment_request, row_field)
		if row_name:
			slot = frappe.db.get_value(
				SLOT_ROW_DOCTYPE,
				row_name,
				["from_time", "to_time", "payment_gateway", "gateway_reference", "paid_on"],
				as_dict=True,
			)

	paid_on = (slot.get("paid_on") if slot else None) or frappe.utils.now()

	# THE REFERENCE ID the gateway gave this payment (for People's Bank, the
	# CyberSource Request ID). The Payment Request is the one place every kind
	# of sale has it - a whole-session sale and a conference pass have no slot
	# row - so it is read first, and the slot row is only the fallback for a
	# payment settled before the field existed.
	txn_field = transaction_id_field()
	reference_id = frappe.db.get_value("Payment Request", payment_request, txn_field) if txn_field else None

	passed = pass_context(payment_request, pr.reference_name) if is_pass else {}

	context = {
		"recipient": recipient,
		"receipt_no": pr.name,
		"participant": participant_name or pr.party,
		"session_title": passed.get("session_title") or schedule.get("session_title"),
		"conference": pr.reference_name if is_pass else schedule.get("conference"),
		# Conference pass only: "Day 1" / "Both Days" / "Day 2", and the dates.
		"pass_package": passed.get("pass_package"),
		"pass_days_display": passed.get("pass_days_display"),
		"session_date": schedule.get("date"),
		"venue": schedule.get("hall"),
		# The participant's own window when they bought a slot; the session's
		# whole window when they bought the session.
		"slot_from": slot.get("from_time") if slot else None,
		"slot_to": slot.get("to_time") if slot else None,
		"session_start": schedule.get("start_time"),
		"session_end": schedule.get("end_time"),
		"gateway": slot.get("payment_gateway") if slot else None,
		"gateway_reference": reference_id or (slot.get("gateway_reference") if slot else None),
		"paid_on": paid_on,
		"amount": pr.grand_total,
		"currency": pr.currency,
		# FORMATTED HERE, NOT IN THE TEMPLATE. Frappe's Jinja sandbox exposes a
		# `frappe` namespace, but exactly what is on it varies by version - and
		# a template that reaches for a missing helper fails at send time, in a
		# scheduled job, where nobody is watching. Doing it in Python also means
		# the tests cover it.
		"session_date_display": frappe.utils.formatdate(schedule.get("date"), "d MMMM yyyy")
		if schedule.get("date")
		else None,
		"paid_on_display": frappe.utils.format_datetime(paid_on, "d MMMM yyyy, HH:mm"),
		"amount_display": frappe.utils.fmt_money(pr.grand_total, currency=pr.currency),
		# "Paid" / "Failed" / None - which email this context is for.
		"payment_status": outcome(payment_request),
		# WHAT WAS BOUGHT, so the subject and wording can say so: a Day 2 pass,
		# Technical Sessions for a day, a slot, a whole session. See outcome_notes.
		"payment_type": passed.get("payment_type") or (TYPE_SLOT if slot else TYPE_SESSION),
		"purchase_title": passed.get("purchase_title") or schedule.get("session_title"),
		"package_rows": passed.get("package_rows") or [],
		"pass_day_names": passed.get("pass_day_names"),
		"pass_dates": passed.get("pass_dates"),
		"tech_day_names": passed.get("tech_day_names"),
		"tech_dates": passed.get("tech_dates"),
	}

	context["confirmation_note"], context["failure_note"] = outcome_notes(context)

	return context


def send_receipt(payment_request, force=False):
	"""Email one receipt. Returns True when a mail was queued.

	`force` re-sends one already marked sent - what the Desk button uses when a
	participant says the email never arrived.
	"""
	field = receipt_field()

	if not field:
		frappe.throw(
			"Payment Request has no `receipt_sent_on` field, so there is no way to record "
			"that a receipt was sent - and therefore no way to avoid sending it twice. "
			"Run `bench --site <site> migrate` to have this app create it."
		)

	# Not even with `force`: a receipt for money that never arrived is worse
	# than no email at all.
	if outcome(payment_request) != "Paid":
		if force:
			frappe.throw("Payment Request %s is not Paid, so there is no receipt to send." % (payment_request,))
		return False

	if not force and frappe.db.get_value("Payment Request", payment_request, field):
		return False

	context = build_context(payment_request)

	if not context:
		frappe.log_error(
			title="Payment receipt has no recipient",
			message="Payment Request %s is paid, but no email address could be found for it - "
			"neither the Participant record nor email_to has one." % (payment_request,),
		)
		return False

	# Wording from the Receipt Email Template DocType, layout from this app.
	subject, message = receipt_email.render(context)

	# `now=True` hands the mail to SMTP immediately instead of waiting for the
	# queue flush, which runs on its own schedule and can lag by minutes. If
	# SMTP is unreachable Frappe leaves the mail in the Email Queue and it goes
	# out on a later flush, so a blip does not lose the receipt.
	# reference_doctype/reference_name put the email on the Payment Request's
	# own timeline, so "was this participant ever told?" is answerable in Desk.
	frappe.sendmail(
		recipients=[context["recipient"]],
		subject=subject,
		message=message,
		reference_doctype="Payment Request",
		reference_name=context["receipt_no"],
		now=True,
	)

	frappe.db.set_value("Payment Request", payment_request, field, frappe.utils.now(), update_modified=False)

	return True


def send_failure_notice(payment_request, force=False):
	"""Email one participant that their payment failed. True when a mail was sent.

	Only ever for a request that really is Failed, and once per request: a
	gateway that reports the same failure twice must not mail twice.
	"""
	field = failure_field()

	if not field:
		frappe.throw(
			"Payment Request has no `failure_notified_on` field, so there is no way to record "
			"that the participant was told - and therefore no way to avoid telling them twice. "
			"Run `bench --site <site> migrate` to have this app create it."
		)

	if outcome(payment_request) != "Failed":
		return False

	if not force and frappe.db.get_value("Payment Request", payment_request, field):
		return False

	context = build_context(payment_request)

	if not context:
		frappe.log_error(
			title="Payment failure notice has no recipient",
			message="Payment Request %s failed, but no email address could be found for it." % (payment_request,),
		)
		return False

	subject, message = receipt_email.render_failure(context)

	frappe.sendmail(
		recipients=[context["recipient"]],
		subject=subject,
		message=message,
		reference_doctype="Payment Request",
		reference_name=context["receipt_no"],
		now=True,
	)

	frappe.db.set_value("Payment Request", payment_request, field, frappe.utils.now(), update_modified=False)

	return True


def send_receipt_now(payment_request):
	"""Entry point for the settlement scripts: tell the participant the outcome right away
	- a receipt if the payment is Paid, a failure notice if it is Failed.

	Enqueue it AFTER the Paid write has committed, e.g. from a Server Script:

		frappe.enqueue("ucic_notifications.receipts.send_receipt_now",
			queue="short", enqueue_after_commit=True, payment_request=name)

	A payment that is neither (still open) gets nothing.

	Never raises into the caller - the minute sweep is the backstop.
	"""
	try:
		result = outcome(payment_request)
		if result == "Paid":
			return send_receipt(payment_request)
		if result == "Failed":
			return send_failure_notice(payment_request)
		return False
	except Exception:
		frappe.log_error(
			title="Payment receipt failed",
			message="Payment Request %s\n\n%s" % (payment_request, frappe.get_traceback()),
		)
		return False


def notify_outcome_for_user(payment_request, user):
	"""Email the outcome of a payment because ITS OWNER'S APP asked for it.

	The app calls this the moment it sees the payment settle, which is what makes
	the email instant. It is a request, not an instruction: this only mails what
	the Payment Request's own status says (Paid -> receipt, Failed -> failure
	notice), only to the address on record, and only once - so a caller can
	neither choose the recipient nor trigger a mail that was not already due.

	The caller must own the payment: it names them (`email_to`) or their
	Participant record carries their login email. Anyone else is refused, so one
	logged-in participant cannot make the server mail another's payment.
	"""
	pr = frappe.db.get_value(
		"Payment Request", payment_request, ["party", "email_to", "status"], as_dict=True
	)

	if not pr:
		raise frappe.PermissionError("Payment Request %s was not found." % (payment_request,))

	user_email = frappe.db.get_value("User", user, "email") or user
	participant_email = (
		frappe.db.get_value("Participant", pr.party, "email") if pr.party else None
	)
	owners = {(v or "").strip().lower() for v in (pr.email_to, participant_email)}

	if (user or "").lower() not in owners and (user_email or "").lower() not in owners:
		raise frappe.PermissionError("Payment Request %s is not yours." % (payment_request,))

	return send_receipt_now(payment_request)


def unreceipted(max_age_days=DEFAULT_MAX_AGE_DAYS, limit=DEFAULT_LIMIT):
	"""Paid Payment Requests that have not been receipted yet."""
	return _unnotified("Paid", receipt_field(), max_age_days, limit)


def unnotified_failures(max_age_days=DEFAULT_MAX_AGE_DAYS, limit=DEFAULT_LIMIT):
	"""Failed Payment Requests whose participant has not been told yet."""
	return _unnotified("Failed", failure_field(), max_age_days, limit)


def _unnotified(status, field, max_age_days, limit):

	if not field:
		return []

	base = {
		"docstatus": 1,
		"party_type": "Participant",
		"reference_doctype": ["in", list(RECEIPTABLE_REFERENCE_DOCTYPES)],
		field: ["in", ["", None]],
	}

	if max_age_days:
		base["modified"] = [">=", frappe.utils.add_days(frappe.utils.nowdate(), -int(max_age_days))]

	# One query per way a request can be in this state - see outcome().
	variants = [{"status": status}]

	status_field = payment_status_field()
	if status == "Failed" and status_field:
		variants.append({"status": ["in", list(OPEN_STATUSES)], status_field: "Failed"})

	rows = []
	seen = set()

	for variant in variants:
		for row in frappe.get_all(
			"Payment Request",
			filters=dict(base, **variant),
			fields=["name"],
			order_by="modified asc",
			limit_page_length=limit,
		):
			if row.name not in seen:
				seen.add(row.name)
				rows.append(row)

	return rows[:limit] if limit else rows


def sweep(max_age_days=DEFAULT_MAX_AGE_DAYS, limit=DEFAULT_LIMIT):
	"""The scheduled entry point. Never raises - one bad row must not stop the rest."""
	sent = 0
	failed = 0

	for row in unreceipted(max_age_days=max_age_days, limit=limit):
		try:
			if send_receipt(row.name):
				sent = sent + 1
		except Exception:
			failed = failed + 1
			# Left unstamped on purpose: the next sweep retries it. That is the
			# main thing this design buys over sending from a gateway callback,
			# where a failure was simply lost.
			frappe.log_error(
				title="Payment receipt failed",
				message="Payment Request %s\n\n%s" % (row.name, frappe.get_traceback()),
			)

	for row in unnotified_failures(max_age_days=max_age_days, limit=limit):
		try:
			if send_failure_notice(row.name):
				sent = sent + 1
		except Exception:
			failed = failed + 1
			frappe.log_error(
				title="Payment failure notice failed",
				message="Payment Request %s\n\n%s" % (row.name, frappe.get_traceback()),
			)

	if sent or failed:
		frappe.logger("ucic_notifications").info("receipt sweep: %d sent, %d failed" % (sent, failed))

	return {"sent": sent, "failed": failed}
