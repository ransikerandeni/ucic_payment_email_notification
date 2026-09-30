"""Field creation and the one-time backfill that makes the first sweep safe."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from ucic_notifications import receipt_email
from ucic_notifications.receipts import RECEIPTABLE_REFERENCE_DOCTYPES, receipt_field

CUSTOM_FIELDS = {
	"Payment Request": [
		{
			"fieldname": "receipt_sent_on",
			"label": "Receipt Sent On",
			"fieldtype": "Datetime",
			"insert_after": "status",
			"read_only": 1,
			# A Payment Request is submitted the moment it is raised, and the
			# sweep stamps this afterwards - so without this the write is
			# refused on a submitted document.
			"allow_on_submit": 1,
			"description": "When the participant was emailed their receipt. Clear it to re-send.",
		}
	]
}


def after_install():
	ensure_custom_fields()
	seed_receipt_email_template()
	backfill_existing()


def after_migrate():
	# Field creation only. The backfill must NOT run again: a site that has been
	# live for a while would have receipts legitimately not yet sent, and
	# stamping those would mean nobody ever gets them.
	ensure_custom_fields()


def ensure_custom_fields():
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)


def seed_receipt_email_template():
	"""Put the default wording into the template, so an organiser opens a form
	that shows what participants currently receive rather than a blank one.

	Fills blanks only, and runs once (install, or the patch on an existing
	site) - never on every migrate, or clearing the footer on purpose would be
	undone by the next deploy.
	"""
	doc = frappe.get_single(receipt_email.TEMPLATE_DOCTYPE)
	changed = False

	for field, value in receipt_email.DEFAULTS.items():
		if not doc.get(field):
			doc.set(field, value)
			changed = True

	if changed:
		doc.flags.ignore_permissions = True
		doc.save()


def backfill_existing():
	"""Mark every ALREADY-settled payment as receipted, without emailing anyone.

	THE POINT OF THIS. The sweep looks for Paid requests with no
	`receipt_sent_on`. On a site with existing payments, every one of them
	matches on the first pass - so installing this app would email a receipt to
	every participant who has ever paid, for a payment they made months ago.

	So installation draws a line: everything settled before this moment counts
	as already handled, and the sweep only ever picks up what happens next.
	`receipts.DEFAULT_MAX_AGE_DAYS` is the independent second guard, for a site
	where this somehow did not run.
	"""
	field = receipt_field()

	if not field:
		return 0

	stamped = frappe.db.sql(
		"""
		UPDATE `tabPayment Request`
		SET `%s` = %%s
		WHERE status = 'Paid'
		  AND docstatus = 1
		  AND (`%s` IS NULL OR `%s` = '')
		  AND reference_doctype IN %%s
		"""
		% (field, field, field),
		(frappe.utils.now(), RECEIPTABLE_REFERENCE_DOCTYPES),
	)

	frappe.db.commit()

	return stamped
