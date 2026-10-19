// The organiser's side of a bank / wire transfer: look at the receipt, check
// the bank statement, then settle the Payment Request with its Mark as Paid
// button (which also sets this submission to Paid).

frappe.ui.form.on("Offline Payment Submission", {
	refresh(frm) {
		const doc = frm.doc;
		if (doc.__islocal) return;

		const indicator = { "Pending Verification": "orange", Paid: "green", Rejected: "red" }[doc.status];
		if (indicator) frm.page.set_indicator(__(doc.status), indicator);

		if (doc.receipt) {
			frm.add_custom_button(__("View Receipt"), () => window.open(doc.receipt, "_blank"));
		}

		if (doc.payment_request) {
			frm.add_custom_button(
				doc.status === "Pending Verification" ? __("Open Payment Request to Mark as Paid") : __("Open Payment Request"),
				() => frappe.set_route("Form", "Payment Request", doc.payment_request)
			).addClass(doc.status === "Pending Verification" ? "btn-primary" : "");
		}

		if (doc.status === "Pending Verification") {
			frm.dashboard.set_headline(
				__("Check the bank statement for {0} quoting {1}, then open the Payment Request and press Mark as Paid.", [
					format_currency(doc.amount, doc.currency),
					`<b>${frappe.utils.escape_html(doc.payment_request)}</b>`,
				])
			);

			frm.add_custom_button(__("Reject"), () => {
				frappe.prompt(
					{ fieldtype: "Small Text", fieldname: "reason", label: __("Why is it rejected?"), reqd: 1 },
					(values) => {
						frm.set_value("status", "Rejected");
						frm.set_value("rejection_reason", values.reason);
						frm.save();
					},
					__("Reject submission")
				);
			});
		}
	},
});
