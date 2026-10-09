"""The subject and wording follow what was bought and how the payment ended."""

import datetime

import frappe
import pytest

from tests.frappe_stub import STATE
from tests.test_receipts import PASS_PR, PR, seed, seed_pass
from ucic_notifications import receipt_email, receipts


@pytest.fixture(autouse=True)
def real_dates(monkeypatch):
    # The stub's formatdate returns one fixed string; these tests are about
    # which day goes where, so they need the real date.
    monkeypatch.setattr(
        frappe.utils,
        "formatdate",
        lambda d, fmt=None: "{0.day} {0:%B %Y}".format(datetime.date.fromisoformat(str(d)[:10])),
    )


def pass_email(package, status="Paid"):
    seed_pass(package=package, start_date="2026-10-07")
    STATE.fields["Payment Request"] |= {"failure_notified_on", "custom_payment_status"}
    if status == "Failed":
        STATE.records["Payment Request"][PASS_PR].update(status="Requested", custom_payment_status="Failed")
    receipts.send_receipt_now(PASS_PR)
    (mail,) = STATE.mails
    return mail


PASSES = [
    (
        "Day 1",
        "Conference Pass (Day 1)",
        "Your Day 1 conference pass for UCIC 2026 is confirmed. It is valid on 7 October 2026.",
        "your Day 1 conference pass for UCIC 2026 has not been issued",
    ),
    (
        "Day 2",
        "Conference Pass (Day 2)",
        "Your Day 2 conference pass for UCIC 2026 is confirmed. It is valid on 8 October 2026.",
        "your Day 2 conference pass for UCIC 2026 has not been issued",
    ),
    (
        "Both Days",
        "Conference Pass (Both Days)",
        "Your conference pass for both days of UCIC 2026 is confirmed. "
        "It is valid on 7 October 2026 and 8 October 2026.",
        "your conference pass for both days of UCIC 2026 has not been issued",
    ),
    (
        "Technical Sessions - Day 1",
        "Technical Sessions (Day 1)",
        "Your Technical Sessions access for Day 1 of UCIC 2026 is confirmed. "
        "You can now book your time slots on 7 October 2026 in the UCIC app.",
        "you cannot book Technical Session slots for Day 1 yet",
    ),
    (
        "Technical Sessions - Day 2",
        "Technical Sessions (Day 2)",
        "Your Technical Sessions access for Day 2 of UCIC 2026 is confirmed. "
        "You can now book your time slots on 8 October 2026 in the UCIC app.",
        "you cannot book Technical Session slots for Day 2 yet",
    ),
    (
        "Day 1 + Technical Sessions - Day 2",
        "Conference Pass (Day 1) + Technical Sessions (Day 2)",
        "Your Day 1 conference pass (valid on 7 October 2026) and Technical Sessions access for "
        "Day 2 (8 October 2026) are confirmed. You can now book your Day 2 time slots in the UCIC app.",
        "your Day 1 conference pass and Day 2 Technical Sessions access have not been issued",
    ),
]


@pytest.mark.parametrize("package,title,confirmed,_failed", PASSES)
def test_paid_pass_subject_and_wording(package, title, confirmed, _failed):
    mail = pass_email(package)

    assert mail["subject"] == "Payment successful - %s - UCIC 2026" % (title,)
    assert confirmed in mail["message"]
    assert "could not be completed" not in mail["message"]


@pytest.mark.parametrize("package,title,_confirmed,failed", PASSES)
def test_failed_pass_subject_and_wording(package, title, _confirmed, failed):
    mail = pass_email(package, status="Failed")

    assert mail["subject"] == "Payment could not be completed - %s - UCIC 2026" % (title,)
    assert "could not be completed</strong>, so %s." % (failed,) in mail["message"]
    assert "we have received your payment" not in mail["message"]


def test_technical_sessions_are_not_labelled_a_conference_pass():
    seed_pass(package="Technical Sessions - Day 2", start_date="2026-10-07")
    ctx = receipts.build_context(PASS_PR)
    rows = dict(receipt_email.details_rows(ctx))

    assert ctx["payment_type"] == "Technical Sessions"
    assert rows["Technical sessions"] == "Day 2"
    assert rows["Slot booking for"] == "8 October 2026"
    assert "Conference pass" not in rows


def test_combined_package_lists_both_parts():
    seed_pass(package="Day 1 + Technical Sessions - Day 2", start_date="2026-10-07")
    rows = dict(receipt_email.details_rows(receipts.build_context(PASS_PR)))

    assert (rows["Conference pass"], rows["Valid on"]) == ("Day 1", "7 October 2026")
    assert (rows["Technical sessions"], rows["Slot booking for"]) == ("Day 2", "8 October 2026")


class TestSessions:
    def test_slot(self):
        seed()
        ctx = receipts.build_context(PR)

        assert ctx["payment_type"] == "Session Slot"
        assert ctx["confirmation_note"] == (
            "Your time slot in 3-Minute Research on 24 August 2026, 10:15 - 10:18, Hall A is confirmed."
        )
        assert ctx["failure_note"] == "your time slot in 3-Minute Research has not been confirmed"

    def test_whole_session(self):
        seed(with_slot=False)
        ctx = receipts.build_context(PR)

        assert ctx["payment_type"] == "Session"
        assert ctx["confirmation_note"] == "Your place in 3-Minute Research on 24 August 2026 in Hall A is confirmed."


class TestWordingPatch:
    def test_old_default_wording_is_upgraded(self, monkeypatch):
        from ucic_notifications.patches import upgrade_receipt_email_wording

        saved = _single(monkeypatch, dict(receipt_email.PREVIOUS_DEFAULTS))
        upgrade_receipt_email_wording.execute()

        assert saved["email_subject"] == receipt_email.DEFAULT_SUBJECT
        assert saved["email_body"] == receipt_email.DEFAULT_BODY

    def test_an_organisers_own_wording_is_kept(self, monkeypatch):
        from ucic_notifications.patches import upgrade_receipt_email_wording

        saved = _single(
            monkeypatch,
            {"email_subject": "Your {{ conference }} receipt", "email_body": receipt_email.PREVIOUS_DEFAULTS["email_body"]},
        )
        upgrade_receipt_email_wording.execute()

        assert saved["email_subject"] == "Your {{ conference }} receipt"
        assert saved["email_body"] == receipt_email.DEFAULT_BODY


def _single(monkeypatch, values):
    class Doc(dict):
        flags = type("Flags", (), {})()

        def set(self, k, v):
            self[k] = v

        def save(self):
            pass

    doc = Doc(values)
    monkeypatch.setattr(frappe, "get_single", lambda doctype: doc, raising=False)
    return doc
