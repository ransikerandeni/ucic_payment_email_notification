# Frappe reads hooks.app_version off this module, so the rebind is the
# point even though nothing here references it.
from . import __version__ as app_version  # noqa: F401

app_name = "ucic_notifications"
app_title = "UCIC Notifications"
app_publisher = "Ransike Randeni"
app_description = "Participant-facing email for the UCIC conference system - payment receipts, and whatever else needs saying"
app_email = "ransikerandeni@gmail.com"
app_license = "MIT"

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
		# Every 15 minutes. A receipt records something that already happened
		# and the participant has already seen the success screen, so minutes
		# of latency cost nothing - and a sweep that failed on a mail server
		# blip simply tries again on the next pass.
		"*/15 * * * *": [
			"ucic_notifications.receipts.sweep",
		]
	}
}
