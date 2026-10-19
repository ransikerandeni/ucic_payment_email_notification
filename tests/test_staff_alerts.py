"""Emails to the organising team: Help & Support requests and failed payments.

What matters: the addresses come from UCIC Notification Settings, a blank one
means "send nothing", each failure is reported once, and none of it can break
the participant-facing flows it reports on.
"""

from tests import frappe_stub
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

    def test_sent_right_after_commit_without_a_background_job(self):
        configure()
        seed_support_request()

        staff_alerts.on_support_request_insert(_Doc())

        # Queued with the request, nothing handed to a worker...
        assert len(STATE.mails) == 1 and STATE.enqueued == []
        assert "now" not in STATE.mails[0]
        # ...and not sent until the request is committed.
        assert STATE.delivered == []
        frappe_stub.commit()
        assert len(STATE.delivered) == 1

    def test_an_smtp_error_after_commit_never_reaches_the_server_script(self):
        configure()
        seed_support_request()
        STATE.smtp_error = "SMTP down"

        staff_alerts.on_support_request_insert(_Doc())
        frappe_stub.commit()  # must not raise

        assert STATE.delivered == []
        assert STATE.errors[0][0] == "Support Request email will be retried"

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

        assert STATE.errors and STATE.errors[0][0] == "Support Request email not sent"


class TestPaymentFailureAlert:
    def test_failed_payment_alerts_the_team(self):
        configure()
        seed_failed()

        assert staff_alerts.send_failure_alert(PR) is True

        (mail,) = STATE.mails
        assert mail["recipients"] == ["payments@ucsc.lk", "help1@ucsc.lk"]
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
        configure(support_1="", failure="")
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
        assert STATE.mails[0]["recipients"] == ["payments@ucsc.lk", "help1@ucsc.lk"]

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


def test_cancelled_at_gateway_alerts_the_team():
    """payment_status Failed while status stays Requested - the shape the gateway
    return script leaves behind for a cancel or a decline."""
    configure()
    seed_failed()
    STATE.fields["Payment Request"].add("custom_payment_status")
    STATE.records["Payment Request"][PR].update(status="Requested", custom_payment_status="Failed")

    staff_alerts.sweep()

    (mail,) = STATE.mails
    assert mail["subject"].startswith("[Payment failed]")


class TestInstantFailureFromApp:
    """The app calls notify_payment_outcome the moment the payer cancels at the
    gateway. Both emails must go then - not the team's a sweep later, or never
    on a site whose scheduler is off."""

    def _cancel(self):
        configure()
        seed_failed()
        STATE.fields["Payment Request"].add("custom_payment_status")
        STATE.records["Payment Request"][PR].update(status="Requested", custom_payment_status="Failed")
        STATE.put("User", "login@example.com", email="login@example.com")

    def test_participant_and_team_are_both_mailed(self):
        self._cancel()

        assert receipts.notify_outcome_for_user(PR, "login@example.com") is True

        recipients = sorted(m["recipients"][0] for m in STATE.mails)
        assert recipients == ["payments@ucsc.lk", "ranmal@example.com"]
        assert STATE.records["Payment Request"][PR]["staff_failure_alert_on"] == STATE.now

    def test_sweep_does_not_alert_the_team_again(self):
        self._cancel()

        receipts.notify_outcome_for_user(PR, "login@example.com")
        staff_alerts.sweep()
        receipts.sweep()

        assert len(STATE.mails) == 2

    def test_team_alert_error_never_costs_the_participant_their_email(self, monkeypatch):
        self._cancel()

        def boom(*args, **kwargs):
            raise RuntimeError("smtp down")

        monkeypatch.setattr(staff_alerts, "send_failure_alert", boom)

        assert receipts.notify_outcome_for_user(PR, "login@example.com") is True
        assert [m["recipients"] for m in STATE.mails] == [["ranmal@example.com"]]
        assert STATE.errors[0][0] == "Payment failure staff alert failed"

    def test_team_is_told_even_when_participant_was_already_told(self):
        self._cancel()
        STATE.records["Payment Request"][PR]["failure_notified_on"] = "2026-10-09 10:00:00"

        receipts.notify_outcome_for_user(PR, "login@example.com")

        assert [m["recipients"] for m in STATE.mails] == [["payments@ucsc.lk", "help1@ucsc.lk"]]


def test_failure_alert_reads_as_a_failure_and_support_does_not():
    configure()
    seed_failed()
    staff_alerts.send_failure_alert(PR)
    failure = STATE.mails[0]["message"]

    assert "&#10005;&nbsp;FAILED" in failure
    assert "#dc2626" in failure
    assert "Amount not paid" in failure
    assert "Paid via" not in failure

    STATE.mails.clear()
    seed_support_request()
    staff_alerts.on_support_request_insert(_Doc())
    support = STATE.mails[0]["message"]

    assert "FAILED" not in support
    assert "#dc2626" not in support


def test_a_second_failed_attempt_on_a_reused_request_mails_both_again():
    """create_gateway_payment clears both stamps when it reuses a request for a
    new attempt; the next failure must then reach the participant AND the team."""
    configure()
    seed_failed()
    STATE.put("User", "login@example.com", email="login@example.com")

    receipts.notify_outcome_for_user(PR, "login@example.com")
    STATE.records["Payment Request"][PR].update(failure_notified_on=None, staff_failure_alert_on=None)
    receipts.notify_outcome_for_user(PR, "login@example.com")

    recipients = sorted(m["recipients"][0] for m in STATE.mails)
    assert recipients == ["payments@ucsc.lk", "payments@ucsc.lk", "ranmal@example.com", "ranmal@example.com"]


def test_both_failure_emails_draw_the_cross_as_an_absolute_image():
    """A text cross is enlarged by the Gmail phone app and spills out of its
    circle, so both emails use the app's PNG - by absolute URL, as a mail client
    has no site to resolve a relative one against."""
    configure()
    seed_failed()

    receipts.send_failure_notice(PR)
    staff_alerts.send_failure_alert(PR)

    for mail in STATE.mails:
        assert 'src="http' in mail["message"]
        assert "/assets/ucic_notifications/images/payment-failed-cross.png" in mail["message"]


class TestFailureAlertAlsoGoesToSupport:
    """The payment-failed alert goes to the Payment Failure Email AND Support
    Email 1 - one email, both addresses. Support Email 2 is not copied."""

    def test_support_email_2_is_not_copied(self):
        configure(support_2="help2@ucsc.lk")
        seed_failed()

        staff_alerts.send_failure_alert(PR)

        assert "help2@ucsc.lk" not in STATE.mails[0]["recipients"]

    def test_same_address_in_both_fields_is_mailed_once(self):
        configure(support_1="PAYMENTS@ucsc.lk")
        seed_failed()

        staff_alerts.send_failure_alert(PR)

        assert STATE.mails[0]["recipients"] == ["payments@ucsc.lk"]

    def test_support_email_1_alone_still_gets_it(self):
        configure(failure="")
        seed_failed()

        assert staff_alerts.sweep() == {"sent": 1, "failed": 0}
        assert STATE.mails[0]["recipients"] == ["help1@ucsc.lk"]

    def test_other_users_of_failure_recipients_are_unchanged(self):
        configure()

        assert staff_alerts.failure_recipients() == ["payments@ucsc.lk"]
