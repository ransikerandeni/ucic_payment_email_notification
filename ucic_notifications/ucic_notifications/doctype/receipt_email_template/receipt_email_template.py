from frappe.model.document import Document

from ucic_notifications import receipt_email


class ReceiptEmailTemplate(Document):
	def validate(self):
		# Refused here, on save, rather than discovered in a scheduled job.
		receipt_email.validate_template(self)
