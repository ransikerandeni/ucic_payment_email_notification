# Frappe reads hooks.app_version off this module, so the rebind is the
# point even though nothing here references it.
from . import __version__ as app_version  # noqa: F401

app_name = "ucic_notifications"
app_title = "UCIC Notifications"
app_publisher = "Ransike Randeni"
app_description = "Participant-facing email for the UCIC conference system - payment receipts, and whatever else needs saying"
app_email = "ransikerandeni@gmail.com"
app_license = "MIT"
app_logo_url = "/assets/ucic_notifications/images/logo.svg"

# ---------------------------------------------------------------------------
# NO doc_events, ON PURPOSE.
#
# A Payment Request reaches "Paid" through frappe.db.set_value / db_set in the
# settlement Server Scripts - direct database writes that never run the document
# lifecycle. A doc_events hook here would never fire, and neither would a Frappe
# Notification. Hence the sweep below.
# ---------------------------------------------------------------------------

after_install = "ucic_notifications.install.after_install"
after_migrate = "ucic_notifications.install.after_migrate"

scheduler_events = {
	"cron": {
		# Every minute. A receipt should reach the participant while they are
		# still looking at the success screen. The sweep only touches Paid
		# requests with no `receipt_sent_on`, so a pass with nothing to do is one
		# cheap indexed query - and one that failed on a mail server blip simply
		# tries again on the next pass.
		"* * * * *": [
			"ucic_notifications.receipts.sweep",
		]
	}
}
