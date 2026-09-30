from ucic_notifications.install import seed_receipt_email_template


def execute():
	# For sites that installed this app before the template existed -
	# after_install does the same on a fresh install.
	seed_receipt_email_template()
