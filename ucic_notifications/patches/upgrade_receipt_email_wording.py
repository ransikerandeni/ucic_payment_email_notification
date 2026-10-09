import frappe

from ucic_notifications import receipt_email


def execute():
	"""Move a site still on the original receipt wording to the wording that
	follows the purchase (Conference Pass (Day 2), Technical Sessions (Day 1), ...).

	Field by field, and only where the saved text is EXACTLY the old default -
	so an organiser who has reworded the subject or body keeps their wording.
	The placeholders they would want are listed on the form.
	"""
	doc = frappe.get_single(receipt_email.TEMPLATE_DOCTYPE)
	changed = []

	for field, previous in receipt_email.PREVIOUS_DEFAULTS.items():
		if (doc.get(field) or "").strip() == previous:
			doc.set(field, receipt_email.DEFAULTS[field])
			changed.append(field)

	if changed:
		doc.flags.ignore_permissions = True
		doc.save()
