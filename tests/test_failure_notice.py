"""A failed payment tells the participant it failed - once, and only if it failed."""

from tests.frappe_stub import STATE
from tests.test_receipts import PR, seed
from ucic_notifications import receipts


def seed_failed(**kwargs):
    seed(**kwargs)
    STATE.fields["Payment Request"] = {"receipt_sent_on", "failure_notified_on", "slot_allocation_row"}
    STATE.records["Payment Request"][PR].update(status="Failed", failure_notified_on=None)


def test_failed_payment_gets_a_failure_email():
    seed_failed()

    assert receipts.send_failure_notice(PR) is True

    (mail,) = STATE.mails
    assert mail["recipients"] == ["ranmal@example.com"]
    assert mail["subject"].startswith("Payment could not be completed")
    assert "could not be completed" in mail["message"]
    assert "PAYMENT NOT COMPLETED" in mail["message"]
    assert "PAID" not in mail["message"].replace("PAYMENT NOT COMPLETED", "")


def test_failure_email_is_sent_once():
    seed_failed()

    assert receipts.send_failure_notice(PR) is True
    assert receipts.send_failure_notice(PR) is False
    assert len(STATE.mails) == 1


def test_a_paid_request_never_gets_a_failure_email():
    seed()
    STATE.fields["Payment Request"] = {"receipt_sent_on", "failure_notified_on"}

    assert receipts.send_failure_notice(PR) is False
    assert STATE.mails == []


def test_sweep_sends_failure_notice_and_not_a_receipt():
    seed_failed()

    receipts.sweep()

    (mail,) = STATE.mails
    assert mail["subject"].startswith("Payment could not be completed")


def test_send_receipt_now_picks_by_status():
    seed_failed()

    receipts.send_receipt_now(PR)

    assert STATE.mails[0]["subject"].startswith("Payment could not be completed")


class TestAppTriggeredNotification:
    def test_owner_triggers_an_instant_receipt(self):
        seed()
        STATE.put("User", "login@example.com", email="login@example.com")

        assert receipts.notify_outcome_for_user(PR, "login@example.com") is True
        assert STATE.mails[0]["recipients"] == ["ranmal@example.com"]

    def test_owner_triggers_an_instant_failure_notice(self):
        seed_failed()
        STATE.put("User", "login@example.com", email="login@example.com")

        receipts.notify_outcome_for_user(PR, "login@example.com")

        assert STATE.mails[0]["subject"].startswith("Payment could not be completed")

    def test_calling_twice_mails_once(self):
        seed()
        STATE.put("User", "login@example.com", email="login@example.com")

        receipts.notify_outcome_for_user(PR, "login@example.com")
        receipts.notify_outcome_for_user(PR, "login@example.com")

        assert len(STATE.mails) == 1

    def test_someone_elses_payment_is_refused(self):
        import pytest

        seed()
        STATE.put("User", "other@example.com", email="other@example.com")

        with pytest.raises(Exception):
            receipts.notify_outcome_for_user(PR, "other@example.com")

        assert STATE.mails == []


def seed_cancelled():
    """What the gateway return script leaves behind when the payer presses Cancel
    (or the card is declined): payment_status Failed, status still Requested."""
    seed()
    STATE.fields["Payment Request"] = {
        "receipt_sent_on",
        "failure_notified_on",
        "slot_allocation_row",
        "custom_payment_status",
    }
    STATE.records["Payment Request"][PR].update(
        status="Requested", custom_payment_status="Failed", failure_notified_on=None
    )


class TestCancelledAtGateway:
    def test_app_trigger_sends_failure_notice_not_receipt(self):
        seed_cancelled()
        STATE.put("User", "login@example.com", email="login@example.com")

        receipts.notify_outcome_for_user(PR, "login@example.com")

        (mail,) = STATE.mails
        assert mail["subject"].startswith("Payment could not be completed")
        assert "could not be completed" in mail["message"]
        assert not STATE.records["Payment Request"][PR].get("receipt_sent_on")

    def test_receipt_is_refused(self):
        seed_cancelled()

        assert receipts.send_receipt(PR) is False
        assert STATE.mails == []

    def test_forced_resend_is_refused(self):
        import pytest

        seed_cancelled()

        with pytest.raises(Exception):
            receipts.send_receipt(PR, force=True)

        assert STATE.mails == []

    def test_sweep_sends_failure_notice(self):
        seed_cancelled()

        receipts.sweep()

        (mail,) = STATE.mails
        assert mail["subject"].startswith("Payment could not be completed")


def test_an_open_payment_gets_no_email():
    seed()
    STATE.fields["Payment Request"] = {"receipt_sent_on", "failure_notified_on", "custom_payment_status"}
    STATE.records["Payment Request"][PR].update(status="Requested", custom_payment_status="Pending")
    STATE.put("User", "login@example.com", email="login@example.com")

    assert receipts.notify_outcome_for_user(PR, "login@example.com") is False
    receipts.sweep()
    assert STATE.mails == []
