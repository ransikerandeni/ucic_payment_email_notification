// Preview renders the form AS IT IS NOW, saved or not, so wording can be tried
// before it goes live. The test email uses the SAVED template - the same one the
// sweep will use - so it is only offered once the form is saved.

const TEMPLATE_FIELDS = ["email_subject", "email_body", "email_footer", "header_title", "logo", "brand_color"];

frappe.ui.form.on("Receipt Email Template", {
	refresh(frm) {
		frm.add_custom_button(__("Preview"), () => {
			const values = {};
			TEMPLATE_FIELDS.forEach((f) => (values[f] = frm.doc[f] || ""));

			frappe.call({
				method: "ucic_notifications.api.preview_receipt",
				args: { values: JSON.stringify(values) },
				freeze: true,
			}).then(({ message }) => {
				const d = new frappe.ui.Dialog({
					title: message.subject,
					size: "large",
					fields: [{ fieldtype: "HTML", fieldname: "preview" }],
				});
				// An iframe keeps Desk's own CSS off the email, so this is what
				// the participant sees rather than an approximation of it.
				const iframe = document.createElement("iframe");
				iframe.style.cssText = "width:100%;height:70vh;border:0;border-radius:8px";
				d.fields_dict.preview.$wrapper.empty().append(iframe);
				iframe.srcdoc = message.message;
				d.show();
			});
		});

		frm.add_custom_button(__("Send Test Email"), () => {
			if (frm.is_dirty()) {
				frappe.msgprint(__("Save first - the test email uses the saved template."));
				return;
			}
			frappe.call({
				method: "ucic_notifications.api.send_test_receipt",
				freeze: true,
				freeze_message: __("Sending..."),
			}).then(({ message }) => {
				frappe.show_alert({ message: __("Test receipt sent to {0}", [message.sent_to]), indicator: "green" });
			});
		});
	},
});
