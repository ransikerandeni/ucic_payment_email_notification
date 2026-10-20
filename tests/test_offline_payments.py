"""Bank Transfer / Wire Transfer: what the app is shown, who may submit a
receipt against which Payment Request, and who is emailed about it.

The rule that matters most: a submission never marks anything paid.
"""

import base64
import types

import pytest

import frappe
from tests.frappe_stub import STATE
from ucic_notifications import offline_payments

SETTINGS = offline_payments.SETTINGS_DOCTYPE
NOTIFY = "UCIC Notification Settings"
PR = "ACC-PRQ-2026-00042"
USER = "ranmal@example.com"
PDF = base64.b64encode(b"%PDF-1.4 receipt").decode()


class FakeDoc(dict):
	"""Enough of a Document for insert() / db_set()."""

	inserted = []

	def __getattr__(self, key):
		return self.get(key)

	def insert(self, ignore_permissions=False):
		doctype = self["doctype"]
		if doctype == offline_payments.SUBMISSION_DOCTYPE:
			self["name"] = "OPS-2026-00001"
		elif doctype == "File":
			self["name"] = "FILE-0001"
			self["file_url"] = "/private/files/" + self["file_name"]
		else:
			self["name"] = "%s-%d" % (doctype, len(FakeDoc.inserted))
		FakeDoc.inserted.append(self)
		STATE.records.setdefault(doctype, {})[self["name"]] = {k: v for k, v in self.items() if k != "content"}
		return self

	def db_set(self, field, value=None):
		values = field if isinstance(field, dict) else {field: value}
		self.update(values)
		STATE.records[self["doctype"]][self["name"]].update(values)


@pytest.fixture(autouse=True)
def site(monkeypatch):
	FakeDoc.inserted = []
	monkeypatch.setattr(frappe, "session", types.SimpleNamespace(user=USER), raising=False)
	monkeypatch.setattr(frappe, "get_doc", lambda values: FakeDoc(values), raising=False)

	STATE.singles[SETTINGS] = {
		"enable_bank_transfer": 1,
		"bt_account_name": "University of Colombo",
		"bt_account_number": "0001234567",
		"bt_bank_name": "People's Bank",
		"bt_branch": "  ",
		"bt_instructions": "Quote the Payment Request ID.",
		"enable_wire_transfer": 0,
		"wt_swift_code": "PSBKLKLX",
	}
	STATE.singles[NOTIFY] = {
		"payment_failure_email": "payments@ucsc.lk",
		"payment_failure_reply_email": "ucic@ucsc.lk",
	}
	STATE.put("Participant", "P-0005", full_name="Ranmal Perera", email=USER, user=USER)
	STATE.put(
		"Payment Request",
		PR,
		name=PR,
		party="P-0005",
		party_type="Participant",
		email_to=USER,
		status="Requested",
		docstatus=1,
		reference_doctype="Conference",
		reference_name="UCIC 2026",
		grand_total=2500.0,
		currency="LKR",
	)
	yield


def test_only_enabled_methods_are_listed_with_their_filled_in_details():
	methods = offline_payments.get_methods()

	assert [m["method"] for m in methods] == ["Bank Transfer"]
	assert methods[0]["details"] == [
		{"label": "Account Name", "value": "University of Colombo"},
		{"label": "Account Number", "value": "0001234567"},
		{"label": "Bank", "value": "People's Bank"},
	]
	assert methods[0]["instructions"] == "Quote the Payment Request ID."


def test_guests_cannot_list_methods(monkeypatch):
	monkeypatch.setattr(frappe, "session", types.SimpleNamespace(user="Guest"))
	with pytest.raises(frappe.PermissionError):
		offline_payments.get_methods()


def test_a_disabled_method_is_refused():
	with pytest.raises(frappe.ValidationError, match="not available"):
		offline_payments.validate_submission(PR, "Wire Transfer", USER)


def test_someone_elses_payment_request_is_refused():
	STATE.put("Participant", "P-0009", full_name="Someone Else", email="else@example.com", user="else@example.com")
	with pytest.raises(frappe.PermissionError):
		offline_payments.validate_submission(PR, "Bank Transfer", "else@example.com")


def test_a_paid_request_is_refused():
	STATE.records["Payment Request"][PR]["status"] = "Paid"
	with pytest.raises(frappe.ValidationError, match="already paid"):
		offline_payments.validate_submission(PR, "Bank Transfer", USER)


@pytest.mark.parametrize(
	"name,data,message",
	[
		("receipt.exe", PDF, "image or a PDF"),
		("receipt.pdf", "not base64!", "could not be read"),
		("receipt.pdf", base64.b64encode(b"x" * (5 * 1024 * 1024 + 1)).decode(), "too large"),
	],
)
def test_bad_receipts_are_refused(name, data, message):
	with pytest.raises(frappe.ValidationError, match=message):
		offline_payments._decode_receipt(name, data)


@pytest.mark.parametrize("method", ["Bank Transfer", "Wire Transfer"])
def test_no_receipt_no_submission(method):
	STATE.singles[SETTINGS]["enable_wire_transfer"] = 1
	with pytest.raises(frappe.ValidationError, match="attach your bank receipt"):
		offline_payments.submit_receipt(PR, method)

	assert offline_payments.SUBMISSION_DOCTYPE not in STATE.records
	assert STATE.mails == []


def test_submitting_saves_the_receipt_and_emails_both_but_marks_nothing_paid():
	out = offline_payments.submit_receipt(
		PR, "Bank Transfer", receipt_name="slip.pdf", receipt_data=PDF, payer_reference="TT-889", transfer_date="2026-10-18"
	)

	assert out["name"] == "OPS-2026-00001"
	sub = STATE.records[offline_payments.SUBMISSION_DOCTYPE]["OPS-2026-00001"]
	assert sub["status"] == offline_payments.PENDING
	assert sub["receipt"] == "/private/files/slip.pdf"
	assert sub["payer_reference"] == "TT-889"

	receipt_file = STATE.records["File"]["FILE-0001"]
	assert receipt_file["is_private"] == 1
	assert receipt_file["attached_to_name"] == "OPS-2026-00001"
	assert receipt_file["attached_to_field"] == "receipt"

	# Mandatory on the DocType, so it must be on the submission when inserted.
	inserted = next(d for d in FakeDoc.inserted if d["doctype"] == offline_payments.SUBMISSION_DOCTYPE)
	assert inserted["receipt"] == "/private/files/slip.pdf"

	# Never settled here - only Mark as Paid does that.
	assert STATE.records["Payment Request"][PR]["status"] == "Requested"

	team, participant = STATE.mails
	assert team["recipients"] == ["payments@ucsc.lk"]
	assert PR in team["subject"] and "Payment verification" in team["subject"]
	assert team["attachments"] == [{"fid": "FILE-0001"}]
	assert team["reply_to"] == USER

	assert participant["recipients"] == [USER]
	assert participant["subject"] == "Payment requested - " + PR
	assert participant["reply_to"] == "ucic@ucsc.lk"
	assert "TT-889" in participant["message"]

	# Sent after the commit, never inside it.
	assert STATE.delivered == []
	frappe.db.commit()
	assert len(STATE.delivered) == 2


def test_no_team_address_still_tells_the_participant():
	STATE.singles[NOTIFY] = {}
	offline_payments.submit_receipt(PR, "Bank Transfer", receipt_name="slip.png", receipt_data=PDF)

	assert [m["recipients"] for m in STATE.mails] == [[USER]]


def test_a_mail_failure_does_not_lose_the_submission(monkeypatch):
	def boom(*a, **k):
		raise RuntimeError("smtp down")

	monkeypatch.setattr(frappe, "sendmail", boom)
	out = offline_payments.submit_receipt(PR, "Bank Transfer", receipt_name="slip.pdf", receipt_data=PDF)

	assert out["name"] in STATE.records[offline_payments.SUBMISSION_DOCTYPE]
	assert STATE.errors and STATE.errors[0][0] == "Offline payment emails not sent"


def test_marking_paid_by_hand_emails_the_receipt_once(monkeypatch):
	from ucic_notifications import api

	monkeypatch.setattr(frappe, "has_permission", lambda *a, **k: True, raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda *a: ["Accounts Manager"], raising=False)

	calls = []
	monkeypatch.setattr(api.receipts, "send_receipt_now", lambda name: calls.append(name) or True)

	assert api.send_receipt_after_manual_payment(PR) == {"sent": True}
	assert calls == [PR]


def test_only_organisers_can_trigger_the_receipt(monkeypatch):
	from ucic_notifications import api

	monkeypatch.setattr(frappe, "has_permission", lambda *a, **k: True, raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda *a: ["Participant"], raising=False)

	with pytest.raises(frappe.PermissionError):
		api.send_receipt_after_manual_payment(PR)
