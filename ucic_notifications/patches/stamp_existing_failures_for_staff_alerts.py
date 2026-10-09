"""One-off for sites that already have this app: add `staff_failure_alert_on`
and mark every payment that failed BEFORE this update as handled, so the team
is only ever alerted about failures that happen from now on."""

from ucic_notifications import install


def execute():
	install.ensure_custom_fields()
	install.backfill_existing_staff_failure_alerts()
