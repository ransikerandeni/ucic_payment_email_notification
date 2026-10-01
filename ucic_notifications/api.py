"""Whitelisted entry points.

All organiser-only. Re-sending a receipt is something an organiser does from
Desk when a participant says the email never arrived; previewing and test-sending
back the Receipt Email Template form. Everything else runs on a schedule.
"""

import json

import frappe

from ucic_notifications import receipt_email, receipts

ORGANISER_ROLES = ("System Manager", "Accounts Manager")


def _require_organiser(action):
	if not set(ORGANISER_ROLES) & set(frappe.get_roles()):
		raise frappe.PermissionError("Only an organiser can %s." % (action,))


@frappe.whitelist()
def resend_receipt(payment_request):
	"""Re-send the receipt for one Payment Request.

	GUARDED, because this is a method that makes the server send mail on demand.
	Without a role check any logged-in participant could point it at somebody
	else's Payment Request and both learn that it exists and have its contents
	mailed out. Receipts name a payer, a session and an amount.
	"""
	if not frappe.has_permission("Payment Request", "read", doc=payment_request):
		raise frappe.PermissionError("You are not allowed to read Payment Request %s." % (payment_request,))

	_require_organiser("re-send a receipt")

	sent = receipts.send_receipt(payment_request, force=True)

	return {"sent": bool(sent)}


@frappe.whitelist()
def notify_payment_outcome(payment_request):
	"""Called by the UCIC app the moment a payment settles, so the participant is
	emailed straight away instead of waiting for the sweep.

	Logged-in participants only, and only for their own payment - see
	receipts.notify_outcome_for_user. Safe to call twice: each outcome is mailed
	once. The sweep remains the backstop for a participant who closed the app
	before it could ask.
	"""
	if frappe.session.user == "Guest":
		raise frappe.PermissionError("Log in to continue.")

	return {"sent": bool(receipts.notify_outcome_for_user(payment_request, frappe.session.user))}


@frappe.whitelist()
def preview_receipt(values=None):
	"""A sample receipt rendered with the template as it stands in the form.

	`values` are the form's unsaved field values, so wording can be tried before
	it is saved. Sample data only - never a real participant's receipt.
	"""
	_require_organiser("preview the receipt email")

	overrides = json.loads(values) if isinstance(values, str) and values else (values or {})
	overrides = {k: v for k, v in overrides.items() if k in receipt_email.EDITABLE_FIELDS}

	subject, message = receipt_email.render(receipt_email.sample_context(), overrides)

	return {"subject": subject, "message": message}


@frappe.whitelist()
def send_test_receipt():
	"""Email a sample receipt, from the SAVED template, to the organiser asking."""
	_require_organiser("send a test receipt")

	recipient = frappe.db.get_value("User", frappe.session.user, "email")

	if not recipient:
		frappe.throw("Your user has no email address to send the test to.")

	subject, message = receipt_email.render(receipt_email.sample_context())

	# now=True: the organiser is waiting on it, and an SMTP problem should show
	# up here, on screen, rather than in the queue.
	frappe.sendmail(recipients=[recipient], subject="[Test] " + subject, message=message, now=True)

	return {"sent_to": recipient}


@frappe.whitelist()
def run_sweep():
	"""Run the receipt sweep by hand - for testing the setup without waiting."""
	if "System Manager" not in frappe.get_roles():
		raise frappe.PermissionError("Only a System Manager can run the receipt sweep.")

	return receipts.sweep()
