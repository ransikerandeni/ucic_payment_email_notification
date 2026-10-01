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
