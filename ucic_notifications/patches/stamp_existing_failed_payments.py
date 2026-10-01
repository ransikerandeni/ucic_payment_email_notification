"""One-off for sites that already have this app: add `failure_notified_on` and
mark every payment that failed BEFORE this update as handled, so the first
sweep does not email participants about old failures."""

from ucic_notifications import install


def execute():
	install.ensure_custom_fields()
	install.backfill_existing_failures()
