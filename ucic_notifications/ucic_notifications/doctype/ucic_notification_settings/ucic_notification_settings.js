// Send Test Email mails a sample of each staff email to the SAVED addresses, so
// an organiser can confirm both the addresses and the outgoing mail setup.

frappe.ui.form.on("UCIC Notification Settings", {
	refresh(frm) {
		frm.add_custom_button(__("Send Test Emails"), () => {
			if (frm.is_dirty()) {
				frappe.msgprint(__("Save first - the test uses the saved addresses."));
				return;
			}
			frappe.call({
				method: "ucic_notifications.api.send_test_staff_alerts",
				freeze: true,
				freeze_message: __("Sending..."),
			}).then(({ message }) => {
				const sent = message.sent_to || [];
				if (!sent.length) {
					frappe.msgprint(__("No addresses are set, so nothing was sent."));
					return;
				}
				frappe.show_alert({ message: __("Test emails sent to {0}", [sent.join(", ")]), indicator: "green" });
			});
		});
	},
});
