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
PACKAGE_FIELD_CANDIDATES = ("conference_package", "custom_conference_package")

# Which conference days each pass package covers: Day 1 is the conference's
# start date, Day 2 the day after - the same rule the Server Scripts use.
PACKAGE_DAYS = {
	"Day 1": (1,),
	"Day 2": (2,),
	"Both Days": (1, 2),
	"Technical Sessions - Day 1": (1,),
	"Technical Sessions - Day 2": (2,),
	"Day 1 + Technical Sessions - Day 2": (1, 2),
}

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


def slot_row_field():
	return _resolve_field("Payment Request", SLOT_ROW_FIELD_CANDIDATES)


def package_field():
	return _resolve_field("Payment Request", PACKAGE_FIELD_CANDIDATES)


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

	return {
		"pass_package": package,
		"session_title": (
			package if (package or "").startswith("Technical Sessions")
			else ("Conference Pass - %s" % (package,) if package else "Conference Pass")
		),
		"pass_days_display": days_display,
	}


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

	passed = pass_context(payment_request, pr.reference_name) if is_pass else {}

	return {
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
		"gateway_reference": slot.get("gateway_reference") if slot else None,
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
	}


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

	# Queued rather than sent inline: a sweep should not stall on a slow SMTP
	# server, and an unreachable one should leave the mail in the queue to go
	# out later rather than failing the whole pass.
	# reference_doctype/reference_name put the email on the Payment Request's
	# own timeline, so "was this participant ever told?" is answerable in Desk.
	frappe.sendmail(
		recipients=[context["recipient"]],
		subject=subject,
		message=message,
		reference_doctype="Payment Request",
		reference_name=context["receipt_no"],
	)

	frappe.db.set_value("Payment Request", payment_request, field, frappe.utils.now(), update_modified=False)

	return True


def unreceipted(max_age_days=DEFAULT_MAX_AGE_DAYS, limit=DEFAULT_LIMIT):
	"""Paid Payment Requests that have not been receipted yet."""
	field = receipt_field()

	if not field:
		return []

	filters = {
		"status": "Paid",
		"docstatus": 1,
		"party_type": "Participant",
		"reference_doctype": ["in", list(RECEIPTABLE_REFERENCE_DOCTYPES)],
		field: ["in", ["", None]],
	}

	if max_age_days:
		filters["modified"] = [">=", frappe.utils.add_days(frappe.utils.nowdate(), -int(max_age_days))]

	return frappe.get_all(
		"Payment Request",
		filters=filters,
		fields=["name"],
		order_by="modified asc",
		limit_page_length=limit,
	)


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

	if sent or failed:
		frappe.logger("ucic_notifications").info("receipt sweep: %d sent, %d failed" % (sent, failed))

	return {"sent": sent, "failed": failed}
