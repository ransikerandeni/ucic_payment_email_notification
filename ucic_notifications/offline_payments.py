"""Bank Transfer and Wire Transfer - paying outside the app.

The flow, end to end:

1. The app's "Other Payment Options" page lists the methods switched on in
   Desk -> **UCIC Offline Payment Settings**, with the bank details an
   organiser keeps there (`get_methods`).
2. The participant picks one. The app asks `create_gateway_payment` for a
   Payment Request with `gateway` set to the method's name - the same Server
   Script, prices and checks as a card payment, minus the gateway checkout -
   and shows its ID, which the participant quotes on the transfer.
3. Once they have paid at their bank, they upload the receipt
   (`submit_receipt`). That saves an **Offline Payment Submission**, emails
   the team's Payment Failure Email (UCIC Notification Settings) with the
   receipt attached, and emails the participant that the payment has been
   requested.
4. An organiser finds the money on the bank statement and presses **Mark as
   Paid** on the Payment Request (the existing ucic_mark_payment_request_paid
   Server Script), which also closes the submission. The receipt email then
   goes out from the usual sweep.

Nothing here marks anything paid: a receipt image is a claim, not proof, and
only a person checking the bank can settle it.
"""

import base64
import binascii
import re

import frappe

from ucic_notifications import receipts, staff_alerts

SETTINGS_DOCTYPE = "UCIC Offline Payment Settings"
SUBMISSION_DOCTYPE = "Offline Payment Submission"

PENDING = "Pending Verification"

# Method -> its switch, its free-text instructions, and the bank details shown
# to the participant in this order. A blank value is simply not shown, so an
# organiser fills in only what their bank needs. Keep in step with the fields
# of UCIC Offline Payment Settings.
METHODS = {
	"Bank Transfer": {
		"enable": "enable_bank_transfer",
		"instructions": "bt_instructions",
		"description": "Transfer from a Sri Lankan bank account or deposit at a branch.",
		"fields": (
			("bt_account_name", "Account Name"),
			("bt_account_number", "Account Number"),
			("bt_bank_name", "Bank"),
			("bt_branch", "Branch"),
			("bt_branch_code", "Branch Code"),
			("bt_swift_code", "SWIFT / BIC"),
			("bt_currency", "Account Currency"),
		),
	},
	"Wire Transfer": {
		"enable": "enable_wire_transfer",
		"instructions": "wt_instructions",
		"description": "International transfer (SWIFT) from a bank outside Sri Lanka.",
		"fields": (
			("wt_beneficiary_name", "Beneficiary Name"),
			("wt_beneficiary_address", "Beneficiary Address"),
			("wt_account_number", "Account Number"),
			("wt_iban", "IBAN"),
			("wt_bank_name", "Beneficiary Bank"),
			("wt_bank_address", "Bank Address"),
			("wt_swift_code", "SWIFT / BIC"),
			("wt_intermediary_bank", "Intermediary Bank"),
			("wt_intermediary_swift", "Intermediary SWIFT / BIC"),
			("wt_currency", "Account Currency"),
		),
	},
}

# Requests a receipt can still be submitted against - the same set Mark as
# Paid will settle from.
OPEN_STATUSES = ("Requested", "Initiated", "Partially Paid")

# What this project raises Payment Requests against (see create_gateway_payment).
UCIC_REFERENCES = ("Conference", "Conference Schedule")

RECEIPT_EXTENSIONS = ("pdf", "png", "jpg", "jpeg", "webp", "gif")
MAX_RECEIPT_BYTES = 5 * 1024 * 1024

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# The Participant check box that opts one participant in to these methods.
# Unticked (the default) means card only. A field on the Participant DocType
# itself (a custom DocType, edited in Desk); a `custom_` copy added through
# Customize Form works too.
PARTICIPANT_ALLOW_FIELD = "allow_other_payment_options"


# ---------------------------------------------------------------------------
# What the app shows
# ---------------------------------------------------------------------------


def _settings():
	return frappe.db.get_singles_dict(SETTINGS_DOCTYPE) or {}


def is_enabled(method, settings=None):
	spec = METHODS.get(method)
	if not spec:
		return False
	settings = settings if settings is not None else _settings()
	return bool(int(settings.get(spec["enable"]) or 0))


def describe(method, settings):
	"""One method as the app renders it: label/value rows plus instructions."""
	spec = METHODS[method]
	details = []
	for fieldname, label in spec["fields"]:
		value = (settings.get(fieldname) or "").strip()
		if value:
			details.append({"label": label, "value": value})

	return {
		"method": method,
		"description": spec["description"],
		"details": details,
		"instructions": (settings.get(spec["instructions"]) or "").strip(),
	}


def participant_allowed(participant):
	"""Whether an organiser has ticked this Participant for offline methods."""
	column = receipts._resolve_field(
		"Participant", (PARTICIPANT_ALLOW_FIELD, "custom_" + PARTICIPANT_ALLOW_FIELD)
	)
	if not participant or not column:
		return False
	return bool(frappe.db.get_value("Participant", participant, column))


@frappe.whitelist()
def get_methods():
	"""The offline methods switched on, with their bank details - none for a
	participant not allowed them."""
	_require_login()
	participant = frappe.db.get_value("Participant", {"user": frappe.session.user}, "name")
	if not participant_allowed(participant):
		return []
	settings = _settings()
	return [describe(method, settings) for method in METHODS if is_enabled(method, settings)]


# ---------------------------------------------------------------------------
# Submitting a receipt
# ---------------------------------------------------------------------------


def _require_login():
	if frappe.session.user == "Guest":
		raise frappe.PermissionError("Log in to continue.")


def _owns(pr, user):
	"""The caller is the payer: their Participant is the party, or the request
	names them. Same rule as receipts.notify_outcome_for_user."""
	if pr.party and frappe.db.get_value("Participant", {"user": user}, "name") == pr.party:
		return True

	user_email = (frappe.db.get_value("User", user, "email") or user or "").strip().lower()
	participant_email = frappe.db.get_value("Participant", pr.party, "email") if pr.party else None
	owners = {(v or "").strip().lower() for v in (pr.email_to, participant_email)} - {""}

	return (user or "").strip().lower() in owners or user_email in owners


def _decode_receipt(receipt_name, receipt_data):
	name = (receipt_name or "").strip().replace("/", "_").replace("\\", "_")
	ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""

	if not name or ext not in RECEIPT_EXTENSIONS:
		frappe.throw("Please attach your bank receipt as an image or a PDF.")

	try:
		content = base64.b64decode(receipt_data or "", validate=True)
	except (binascii.Error, ValueError):
		frappe.throw("The receipt could not be read. Please attach it again.")

	if not content:
		frappe.throw("Please attach your bank receipt.")

	if len(content) > MAX_RECEIPT_BYTES:
		frappe.throw("The receipt is too large (max 5 MB).")

	return name[-140:], content


def validate_submission(payment_request, payment_method, user):
	"""Everything checked before a submission is saved. Returns the Payment
	Request row. Raises with a message the participant can act on."""
	if payment_method not in METHODS:
		frappe.throw("Unknown payment method: %s" % (payment_method,))

	if not is_enabled(payment_method):
		frappe.throw("%s is not available right now." % (payment_method,))

	pr = frappe.db.get_value(
		"Payment Request",
		payment_request or "",
		["name", "party", "party_type", "email_to", "status", "docstatus", "reference_doctype",
		 "reference_name", "grand_total", "currency"],
		as_dict=True,
	)

	if not pr:
		frappe.throw("Payment Request %s was not found." % (payment_request,))

	if not _owns(pr, user):
		raise frappe.PermissionError("Payment Request %s is not yours." % (payment_request,))

	if pr.party_type == "Participant" and not participant_allowed(pr.party):
		frappe.throw("%s is not available for your registration. Please pay by card." % (payment_method,))

	if pr.reference_doctype not in UCIC_REFERENCES:
		frappe.throw("Payment Request %s cannot be paid by %s." % (pr.name, payment_method))

	if pr.status == "Paid":
		frappe.throw("Payment Request %s is already paid." % (pr.name,))

	if pr.docstatus != 1 or pr.status not in OPEN_STATUSES:
		frappe.throw(
			"Payment Request %s is no longer open (%s). Please start the payment again." % (pr.name, pr.status)
		)

	return pr


@frappe.whitelist()
def submit_receipt(
	payment_request,
	payment_method,
	receipt_name=None,
	receipt_data=None,
	payer_reference=None,
	transfer_date=None,
	remarks=None,
):
	"""Save the participant's bank receipt against their Payment Request and
	tell the team and the participant. Does NOT mark anything paid."""
	_require_login()
	user = frappe.session.user

	pr = validate_submission(payment_request, payment_method, user)
	file_name, content = _decode_receipt(receipt_name, receipt_data)

	# Both required, for Bank Transfer and Wire Transfer alike: with the
	# receipt, they are what the organising team matches against the bank
	# statement. Checked before anything is saved.
	payer_reference = (payer_reference or "").strip()[:140]
	if not payer_reference:
		frappe.throw("Please enter the bank reference shown on your receipt.")

	transfer_date = (transfer_date or "").strip()
	if not transfer_date:
		frappe.throw("Please enter the date you made the transfer.")
	if not _DATE.match(transfer_date):
		frappe.throw("Transfer date must be a date (YYYY-MM-DD).")
	if transfer_date > frappe.utils.nowdate():
		frappe.throw("The transfer date cannot be in the future.")

	ctx = receipts.build_context(pr.name) or {}
	participant_name = ctx.get("participant") or (
		frappe.db.get_value("Participant", pr.party, "full_name") if pr.party else None
	)

	# The receipt is mandatory on the submission (reqd on its DocType), so the
	# file is saved first and attached to the submission once that exists.
	receipt_file = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": file_name,
			"is_private": 1,
			"content": content,
		}
	)
	receipt_file.insert(ignore_permissions=True)

	doc = frappe.get_doc(
		{
			"doctype": SUBMISSION_DOCTYPE,
			"payment_request": pr.name,
			"payment_method": payment_method,
			"status": PENDING,
			"participant": pr.party if pr.party_type == "Participant" else None,
			"participant_name": participant_name,
			"email": ctx.get("recipient") or pr.email_to or user,
			"payment_for": ctx.get("session_title") or ("%s %s" % (pr.reference_doctype, pr.reference_name)),
			"amount": pr.grand_total,
			"currency": pr.currency,
			"payer_reference": payer_reference,
			"transfer_date": transfer_date,
			"remarks": (remarks or "").strip() or None,
			"receipt": receipt_file.file_url,
			"submitted_by": user,
			"submitted_on": frappe.utils.now(),
		}
	)
	doc.insert(ignore_permissions=True)

	# So the receipt shows in the submission's attachments and is permission-
	# checked against it.
	receipt_file.db_set(
		{
			"attached_to_doctype": SUBMISSION_DOCTYPE,
			"attached_to_name": doc.name,
			"attached_to_field": "receipt",
		}
	)

	# On the Payment Request too, where the organiser presses Mark as Paid.
	frappe.get_doc(
		{
			"doctype": "Comment",
			"comment_type": "Comment",
			"reference_doctype": "Payment Request",
			"reference_name": pr.name,
			"comment_email": user,
			"content": "%s receipt submitted by the participant: %s. Check the bank statement, then Mark as Paid."
			% (frappe.utils.escape_html(payment_method), _form_link(SUBMISSION_DOCTYPE, doc.name)),
		}
	).insert(ignore_permissions=True)

	# The submission is saved whatever happens to the emails.
	try:
		send_submission_emails(doc.name, receipt_file.name)
	except Exception:
		frappe.log_error(
			title="Offline payment emails not sent",
			message="%s %s\n\n%s" % (SUBMISSION_DOCTYPE, doc.name, frappe.get_traceback()),
		)

	return {
		"name": doc.name,
		"payment_request": pr.name,
		"status": PENDING,
		"email": doc.email,
	}


def _form_link(doctype, name):
	return '<a href="%s">%s</a>' % (frappe.utils.get_url_to_form(doctype, name), frappe.utils.escape_html(name))


# ---------------------------------------------------------------------------
# Emails
# ---------------------------------------------------------------------------


def _details(sub, amount_display):
	return [
		("Payment Request ID", sub.payment_request),
		("Participant", sub.participant_name),
		("Participant ID", sub.participant),
		("Email", sub.email),
		("For", sub.payment_for),
		("Amount", amount_display),
		("Payment method", sub.payment_method),
		("Bank reference", sub.payer_reference),
		("Transfer date", frappe.utils.formatdate(sub.transfer_date, "d MMMM yyyy") if sub.transfer_date else None),
		("Submission", sub.name),
	]


def send_submission_emails(submission, receipt_file=None):
	"""The team's "please verify" email and the participant's "payment
	requested" email. Returns which were queued. Sent after the commit, so a
	rolled-back submission is never mailed and an SMTP error cannot undo it."""
	sub = frappe.db.get_value(
		SUBMISSION_DOCTYPE,
		submission,
		["name", "payment_request", "payment_method", "participant", "participant_name", "email",
		 "payment_for", "amount", "currency", "payer_reference", "transfer_date", "remarks"],
		as_dict=True,
	)

	if not sub:
		return {"team": False, "participant": False}

	# Formatted exactly as the receipt formats it ("Rs 1,000.00"), so the two
	# emails about one payment show one figure.
	amount_display = frappe.utils.fmt_money(sub.amount, currency=sub.currency)

	who = sub.participant_name or sub.participant or sub.email
	details = _details(sub, amount_display)
	sent = {"team": False, "participant": False}

	team = staff_alerts.failure_recipients()
	if team:
		queue = frappe.sendmail(
			recipients=team,
			subject="[Payment verification] %s - %s (%s)" % (who, sub.payment_request, sub.payment_method),
			message=staff_alerts.render(
				kicker="%s submitted" % (sub.payment_method,),
				title="%s - %s" % (who, amount_display),
				intro="A participant says they have paid by %s and uploaded their receipt (attached). "
				"Check the bank statement for this amount and the Payment Request ID, then open the "
				"Payment Request and press Mark as Paid." % (sub.payment_method.lower(),),
				details=details,
				quote=sub.remarks,
				link_label="Open Payment Request",
				link_url=frappe.utils.get_url_to_form("Payment Request", sub.payment_request),
			),
			reply_to=sub.email or None,
			attachments=[{"fid": receipt_file}] if receipt_file else None,
			reference_doctype=SUBMISSION_DOCTYPE,
			reference_name=sub.name,
		)
		staff_alerts._send_after_commit(queue)
		sent["team"] = True

	if sub.email:
		reply_to = (staff_alerts._settings().get("payment_failure_reply_email") or "").strip() or None
		queue = frappe.sendmail(
			recipients=[sub.email],
			subject="Payment requested - %s" % (sub.payment_request,),
			message=staff_alerts.render(
				kicker="Payment requested",
				title="%s by %s" % (amount_display, sub.payment_method.lower()),
				intro="Thank you%s. We have received your %s receipt for Payment Request %s. "
				"The UCIC organising team will check it against the bank statement and email you a "
				"receipt once the payment is confirmed. Until then your payment shows as requested, "
				"not paid." % (
					(", " + sub.participant_name) if sub.participant_name else "",
					sub.payment_method.lower(),
					sub.payment_request,
				),
				details=[(label, value) for label, value in details if label not in ("Participant ID", "Submission")],
			),
			reply_to=reply_to,
			reference_doctype=SUBMISSION_DOCTYPE,
			reference_name=sub.name,
		)
		staff_alerts._send_after_commit(queue)
		sent["participant"] = True

	return sent
