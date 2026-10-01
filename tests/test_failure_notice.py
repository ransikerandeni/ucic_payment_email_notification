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
    assert mail["subject"].startswith("Payment unsuccessful")
    assert "did not go through" in mail["message"]
    assert "PAYMENT FAILED" in mail["message"]
    assert "PAID" not in mail["message"].replace("PAYMENT FAILED", "")


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
    assert mail["subject"].startswith("Payment unsuccessful")


def test_send_receipt_now_picks_by_status():
    seed_failed()

    receipts.send_receipt_now(PR)

    assert STATE.mails[0]["subject"].startswith("Payment unsuccessful")


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

        assert STATE.mails[0]["subject"].startswith("Payment unsuccessful")

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
