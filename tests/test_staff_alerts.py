"""Emails to the organising team: Help & Support requests and failed payments.

What matters: the addresses come from UCIC Notification Settings, a blank one
means "send nothing", each failure is reported once, and none of it can break
the participant-facing flows it reports on.
"""

from tests.frappe_stub import STATE
from tests.test_receipts import PR, seed
from ucic_notifications import receipts, staff_alerts

SETTINGS = "UCIC Notification Settings"
SR = "SUP-REQ-0007"


def configure(support_1="help1@ucsc.lk", support_2="help2@ucsc.lk", failure="payments@ucsc.lk"):
    STATE.singles[SETTINGS] = {
        "support_email_1": support_1,
        "support_email_2": support_2,
        "payment_failure_email": failure,
    }


def seed_support_request(**overrides):
    values = {
        "name": SR,
        "subject": "Cannot see my pass",
        "message": "I paid but the pass page is empty.\n<b>help</b>",
        "email": "ranmal@example.com",
        "event": "UCIC 2026",
        "page_name": "Conference Pass",
        "page_route": "/conference-pass",
        "creation": "2026-10-09 10:00:00",
    }
    values.update(overrides)
    STATE.put("Support Request", SR, **values)
    STATE.put("Participant", "P-0005", full_name="Ranmal Perera", email="ranmal@example.com")
    return STATE.records["Support Request"][SR]


class _Doc:
    name = SR


def seed_failed():
    seed()
    STATE.fields["Payment Request"] = {
        "receipt_sent_on",
        "failure_notified_on",
        "staff_failure_alert_on",
        "slot_allocation_row",
    }
    STATE.records["Payment Request"][PR].update(status="Failed", failure_notified_on=None, staff_failure_alert_on=None)


class TestSupportRequest:
    def test_goes_to_both_support_addresses(self):
        configure()
        seed_support_request()

        staff_alerts.on_support_request_insert(_Doc())

        (mail,) = STATE.mails
        assert mail["recipients"] == ["help1@ucsc.lk", "help2@ucsc.lk"]
        assert mail["subject"] == "[Help & Support] Cannot see my pass"
        assert mail["reply_to"] == "ranmal@example.com"
        assert (mail["reference_doctype"], mail["reference_name"]) == ("Support Request", SR)

    def test_body_carries_the_request_and_escapes_the_message(self):
        configure()
        seed_support_request()

        staff_alerts.on_support_request_insert(_Doc())
        body = STATE.mails[0]["message"]

        assert "Ranmal Perera" in body
        assert "Conference Pass (/conference-pass)" in body
        assert "I paid but the pass page is empty.<br>" in body
        assert "<b>help</b>" not in body and "&lt;b&gt;help&lt;/b&gt;" in body

    def test_sent_after_commit_not_inline(self):
        configure()
        seed_support_request()

        staff_alerts.on_support_request_insert(_Doc())

        assert STATE.enqueued == [("ucic_notifications.staff_alerts.send_support_request_email", {"support_request": SR})]

    def test_only_one_address_set(self):
        configure(support_2="")
        seed_support_request()

        staff_alerts.on_support_request_insert(_Doc())

        assert STATE.mails[0]["recipients"] == ["help1@ucsc.lk"]

    def test_same_address_twice_is_mailed_once(self):
        configure(support_2="HELP1@ucsc.lk")
        seed_support_request()

        staff_alerts.on_support_request_insert(_Doc())

        assert STATE.mails[0]["recipients"] == ["help1@ucsc.lk"]

    def test_no_addresses_sends_nothing(self):
        configure(support_1="", support_2="")
        seed_support_request()

        staff_alerts.on_support_request_insert(_Doc())

        assert STATE.mails == [] and STATE.enqueued == []

    def test_never_configured_sends_nothing(self):
        seed_support_request()

        staff_alerts.on_support_request_insert(_Doc())

        assert STATE.mails == []

    def test_a_mail_error_never_reaches_the_insert(self, monkeypatch):
        import frappe

        configure()
        seed_support_request()

        def boom(**kwargs):
            raise RuntimeError("SMTP down")

        monkeypatch.setattr(frappe, "sendmail", boom)

        staff_alerts.on_support_request_insert(_Doc())  # must not raise

        assert STATE.errors and STATE.errors[0][0] == "Support Request email not queued"


class TestPaymentFailureAlert:
    def test_failed_payment_alerts_the_team(self):
        configure()
        seed_failed()

        assert staff_alerts.send_failure_alert(PR) is True

        (mail,) = STATE.mails
        assert mail["recipients"] == ["payments@ucsc.lk"]
        assert mail["subject"] == "[Payment failed] Ranmal Perera - %s" % PR
        assert "ranmal@example.com" in mail["message"]
        assert "3-Minute Research" in mail["message"]
        assert STATE.records["Payment Request"][PR]["staff_failure_alert_on"] == STATE.now

    def test_alerted_once(self):
        configure()
        seed_failed()

        staff_alerts.send_failure_alert(PR)
        assert staff_alerts.send_failure_alert(PR) is False
        assert len(STATE.mails) == 1

    def test_paid_payment_is_never_alerted(self):
        configure()
        seed_failed()
        STATE.records["Payment Request"][PR]["status"] = "Paid"

        assert staff_alerts.send_failure_alert(PR) is False
        assert STATE.mails == []

    def test_no_address_sends_nothing_and_stamps_nothing(self):
        configure(failure="")
        seed_failed()

        assert staff_alerts.sweep() == {"sent": 0, "failed": 0}
        assert STATE.mails == []
        assert STATE.records["Payment Request"][PR]["staff_failure_alert_on"] is None

    def test_team_is_told_even_when_participant_has_no_address(self):
        configure()
        seed_failed()
        STATE.records["Participant"]["P-0005"]["email"] = None
        STATE.records["Payment Request"][PR]["email_to"] = None

        assert staff_alerts.send_failure_alert(PR) is True
        assert STATE.mails[0]["recipients"] == ["payments@ucsc.lk"]

    def test_staff_alert_is_independent_of_the_participant_notice(self):
        configure()
        seed_failed()

        receipts.sweep()
        staff_alerts.sweep()

        recipients = sorted(m["recipients"][0] for m in STATE.mails)
        assert recipients == ["payments@ucsc.lk", "ranmal@example.com"]

    def test_participant_flow_unchanged_when_nothing_configured(self):
        seed_failed()

        receipts.sweep()
        staff_alerts.sweep()

        (mail,) = STATE.mails
        assert mail["recipients"] == ["ranmal@example.com"]

    def test_sweep_skips_old_failures(self):
        configure()
        seed_failed()
        STATE.records["Payment Request"][PR]["modified"] = "2026-08-01 09:00:00"

        staff_alerts.sweep()

        assert STATE.mails == []
