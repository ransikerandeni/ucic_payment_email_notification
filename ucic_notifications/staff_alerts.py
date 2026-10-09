"""Emails to the ORGANISING TEAM, as opposed to participants.

Two of them, both addressed from "UCIC Notification Settings" in Desk:

- HELP & SUPPORT. Every Support Request is mailed to Support Email 1 and
  Support Email 2. The app's `save_support_request` Server Script creates it
  with `insert()`, which DOES run the document lifecycle - so unlike payments,
  a plain `after_insert` hook works here. The send is enqueued after commit:
  a request that is rolled back is never mailed, and a slow mail server never
  slows the participant's "Send" button.

- PAYMENT FAILURES. Every Failed Payment Request is mailed once to the Payment
  Failure Email. A payment reaches Failed through `frappe.db.set_value` in the
  gateway return script, so - like the receipt - this is found by a sweep, and
  guarded by its own `staff_failure_alert_on` field. It is deliberately NOT tied
  to the participant's `failure_notified_on`: a payment with no participant
  address still has to reach the team, and either email can be re-sent alone.

Nothing here may break what it reports on. The support hook swallows its own
errors (logging them) so a mail problem can never cost a participant their
saved request, and a blank address simply means "send nothing".
"""

import html

import frappe

from ucic_notifications import receipt_email, receipts

SETTINGS_DOCTYPE = "UCIC Notification Settings"
SUPPORT_DOCTYPE = "Support Request"
LAYOUT = "ucic_notifications/templates/emails/staff_alert.html"

STAFF_FAILURE_FIELD_CANDIDATES = ("staff_failure_alert_on", "custom_staff_failure_alert_on")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def _settings():
	return frappe.db.get_singles_dict(SETTINGS_DOCTYPE) or {}


def _addresses(*values):
	"""Clean, de-duplicated addresses in order; blanks dropped."""
	out = []
	for value in values:
		value = (value or "").strip()
		if value and value.lower() not in {v.lower() for v in out}:
			out.append(value)
	return out


def support_recipients():
	s = _settings()
	return _addresses(s.get("support_email_1"), s.get("support_email_2"))


def failure_recipients():
	return _addresses(_settings().get("payment_failure_email"))


def staff_failure_field():
	return receipts._resolve_field("Payment Request", STAFF_FAILURE_FIELD_CANDIDATES)


# ---------------------------------------------------------------------------
# Help & Support
# ---------------------------------------------------------------------------


def on_support_request_insert(doc, method=None):
	"""doc_events hook. Never raises: the participant's request is already saved,
	and that matters more than the email about it."""
	try:
		if not support_recipients():
			return

		frappe.enqueue(
			"ucic_notifications.staff_alerts.send_support_request_email",
			queue="short",
			enqueue_after_commit=True,
			support_request=doc.name,
		)
	except Exception:
		frappe.log_error(
			title="Support Request email not queued",
			message="Support Request %s\n\n%s" % (doc.name, frappe.get_traceback()),
		)


def send_support_request_email(support_request):
	"""Email one Support Request to both support addresses. True when sent."""
	recipients = support_recipients()

	if not recipients:
		return False

	req = frappe.db.get_value(
		SUPPORT_DOCTYPE,
		support_request,
		["name", "subject", "message", "email", "event", "page_name", "page_route", "creation"],
		as_dict=True,
	)

	if not req:
		return False

	participant = None
	if req.email:
		participant = frappe.db.get_value("Participant", {"email": req.email}, "full_name")

	page = req.page_name or ""
	if req.page_route:
		page = "%s (%s)" % (page, req.page_route) if page else req.page_route

	details = [
		("Request", req.name),
		("From", participant),
		("Email", req.email),
		("Conference", req.event),
		("Page", page),
		("Received", frappe.utils.format_datetime(req.creation, "d MMMM yyyy, HH:mm") if req.creation else None),
	]

	message = render(
		kicker="Help & Support",
		title=req.subject or "Support request",
		intro="A participant has sent a request from the UCIC app. Reply to this email to answer them directly.",
		details=details,
		quote=req.message,
		link_label="Open in ERPNext",
		link_url=frappe.utils.get_url_to_form(SUPPORT_DOCTYPE, req.name),
	)

	frappe.sendmail(
		recipients=recipients,
		subject="[Help & Support] %s" % (req.subject or req.name),
		message=message,
		# So "Reply" in the team's mail client goes straight to the participant.
		reply_to=req.email or None,
		reference_doctype=SUPPORT_DOCTYPE,
		reference_name=req.name,
		now=True,
	)

	return True


# ---------------------------------------------------------------------------
# Payment failures
# ---------------------------------------------------------------------------


def send_failure_alert(payment_request, force=False):
	"""Tell the organising team one payment failed. True when a mail was sent."""
	recipients = failure_recipients()
	field = staff_failure_field()

	if not recipients or not field:
		return False

	row = frappe.db.get_value("Payment Request", payment_request, ["status", field], as_dict=True)

	if not row or row.status != "Failed":
		return False

	if not force and row.get(field):
		return False

	pr = frappe.db.get_value(
		"Payment Request", payment_request, ["party", "email_to", "grand_total", "currency"], as_dict=True
	)

	# The participant-facing context has everything, but is None when the
	# participant has no address - and the team must hear about it regardless.
	ctx = receipts.build_context(payment_request) or {}

	amount_display = ctx.get("amount_display") or frappe.utils.fmt_money(pr.grand_total, currency=pr.currency)
	participant = ctx.get("participant") or pr.party

	details = [
		("Payment Request", payment_request),
		("Participant", participant),
		("Participant ID", pr.party if participant != pr.party else None),
		("Email", ctx.get("recipient") or pr.email_to),
		("For", ctx.get("session_title")),
		("Conference", ctx.get("conference")),
		("Amount", amount_display),
		("Paid via", ctx.get("gateway")),
		("Reference ID", ctx.get("gateway_reference")),
		("Failed on", frappe.utils.format_datetime(frappe.utils.now(), "d MMMM yyyy, HH:mm")),
	]

	message = render(
		kicker="Payment failed",
		title="%s - %s" % (participant or payment_request, amount_display),
		intro="A participant's payment did not go through. They have been emailed separately, "
		"if an address is on record. Check the gateway if they say the amount was deducted.",
		details=details,
		link_label="Open Payment Request",
		link_url=frappe.utils.get_url_to_form("Payment Request", payment_request),
	)

	frappe.sendmail(
		recipients=recipients,
		subject="[Payment failed] %s - %s" % (participant or payment_request, payment_request),
		message=message,
		reference_doctype="Payment Request",
		reference_name=payment_request,
		now=True,
	)

	frappe.db.set_value("Payment Request", payment_request, field, frappe.utils.now(), update_modified=False)

	return True


def unalerted_failures(max_age_days=receipts.DEFAULT_MAX_AGE_DAYS, limit=receipts.DEFAULT_LIMIT):
	"""Failed Payment Requests the team has not been told about yet."""
	return receipts._unnotified("Failed", staff_failure_field(), max_age_days, limit)


def sweep(max_age_days=receipts.DEFAULT_MAX_AGE_DAYS, limit=receipts.DEFAULT_LIMIT):
	"""Scheduled entry point. Never raises; one bad row must not stop the rest.

	With no Payment Failure Email set there is nobody to tell, so it does not
	even query - and does not stamp, so nothing is marked "told" that was not.
	"""
	sent = 0
	failed = 0

	if not failure_recipients():
		return {"sent": 0, "failed": 0}

	for row in unalerted_failures(max_age_days=max_age_days, limit=limit):
		try:
			if send_failure_alert(row.name):
				sent = sent + 1
		except Exception:
			failed = failed + 1
			frappe.log_error(
				title="Payment failure staff alert failed",
				message="Payment Request %s\n\n%s" % (row.name, frappe.get_traceback()),
			)

	if sent or failed:
		frappe.logger("ucic_notifications").info("staff alert sweep: %d sent, %d failed" % (sent, failed))

	return {"sent": sent, "failed": failed}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render(kicker, title, intro, details, quote=None, link_label=None, link_url=None):
	"""The staff email's HTML. Branding comes from the Receipt Email Template so
	every email from the system looks like it is from the same place."""
	tpl = receipt_email.get_template()
	brand = tpl.brand_color
	on_brand = receipt_email._text_on(brand)

	# The participant's message is plain text: escape it, keep its line breaks.
	quote_html = html.escape(quote).replace("\n", "<br>") if quote else ""

	return frappe.render_template(
		LAYOUT,
		{
			"kicker": kicker,
			"title": title,
			"intro": intro,
			"details": [(label, str(value)) for label, value in details if value],
			"quote_html": quote_html,
			"link_label": link_label,
			"link_url": link_url,
			"preheader": title,
			"logo_url": receipt_email._absolute_url(tpl.logo),
			"brand_color": brand,
			"brand_text": on_brand,
			"brand_muted": receipt_email._mix(on_brand, brand, 0.72),
			"brand_tint": receipt_email._mix(brand, "#ffffff", 0.06),
			"brand_line": receipt_email._mix(brand, "#ffffff", 0.14),
		},
	)


def sample_support_details():
	return [
		("Request", "SUP-REQ-0001"),
		("From", "Sample Participant"),
		("Email", "participant@example.com"),
		("Conference", "UCIC 2026"),
		("Page", "Conference Pass (/conference-pass)"),
	]
