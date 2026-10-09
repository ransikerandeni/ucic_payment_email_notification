"""What a receipt must and must not do.

The two that matter most are the ones a Server Script could not be trusted on:
a replayed settlement must not email twice, and installing this app must not
blast a receipt at everyone who has ever paid.
"""

import pytest

from tests.frappe_stub import STATE
from ucic_notifications import receipts

PR = "ACC-PRQ-2026-00007"


def seed(with_slot=True, participant_email="ranmal@example.com", receipt_sent_on=None):
    STATE.fields["Payment Request"] = {"receipt_sent_on", "slot_allocation_row"}

    STATE.put(
        "Payment Request",
        PR,
        name=PR,
        party="P-0005",
        party_type="Participant",
        status="Paid",
        docstatus=1,
        grand_total=10.0,
        currency="LKR",
        reference_doctype="Conference Schedule",
        reference_name="CONF-SCH-08-2026-0024",
        email_to="login@example.com",
        slot_allocation_row="row-abc" if with_slot else None,
        receipt_sent_on=receipt_sent_on,
        modified="2026-09-17 09:00:00",
    )
    STATE.put("Participant", "P-0005", full_name="Ranmal Perera", email=participant_email)
    STATE.put(
        "Conference Schedule",
        "CONF-SCH-08-2026-0024",
        session_title="3-Minute Research",
        date="2026-08-24",
        start_time="10:00:00",
        end_time="12:00:00",
        hall="Hall A",
        conference="UCIC 2026",
    )
    STATE.put(
        "Slot Allocations",
        "row-abc",
        from_time="10:15:00",
        to_time="10:18:00",
        payment_gateway="WebXPay",
        gateway_reference="WX-99881",
        paid_on="2026-09-17 09:25:00",
    )


class TestBuildContext:
    def test_slot_sale_carries_the_participants_own_window(self):
        seed()
        ctx = receipts.build_context(PR)

        assert ctx["recipient"] == "ranmal@example.com"
        assert ctx["participant"] == "Ranmal Perera"
        assert (ctx["slot_from"], ctx["slot_to"]) == ("10:15:00", "10:18:00")
        assert ctx["gateway_reference"] == "WX-99881"
        assert (ctx["amount"], ctx["currency"]) == (10.0, "LKR")

    def test_reference_id_comes_from_the_payment_request(self):
        seed(with_slot=False)
        STATE.fields["Payment Request"].add("custom_gateway_transaction_id")
        STATE.records["Payment Request"][PR]["custom_gateway_transaction_id"] = "7902294473696640104010"

        # A whole-session sale has no slot row, so this is the only source.
        assert receipts.build_context(PR)["gateway_reference"] == "7902294473696640104010"

    def test_reference_id_prefers_the_payment_request_over_the_slot_row(self):
        seed()
        STATE.fields["Payment Request"].add("custom_gateway_transaction_id")
        STATE.records["Payment Request"][PR]["custom_gateway_transaction_id"] = "7902294473696640104010"

        assert receipts.build_context(PR)["gateway_reference"] == "7902294473696640104010"

    def test_whole_session_sale_has_no_slot_of_its_own(self):
        seed(with_slot=False)
        ctx = receipts.build_context(PR)

        # The absence of a row IS the signal - see create_gateway_payment.
        assert ctx["slot_from"] is None
        assert (ctx["session_start"], ctx["session_end"]) == ("10:00:00", "12:00:00")

    def test_falls_back_to_the_payer_login_when_participant_has_no_email(self):
        seed(participant_email=None)
        assert receipts.build_context(PR)["recipient"] == "login@example.com"

    def test_returns_nothing_when_there_is_no_address_at_all(self):
        seed(participant_email=None)
        STATE.records["Payment Request"][PR]["email_to"] = None
        assert receipts.build_context(PR) is None


PASS_PR = "ACC-PRQ-2026-00031"


def seed_pass(package="Both Days", start_date="2026-08-23"):
    STATE.fields["Payment Request"] = {"receipt_sent_on", "slot_allocation_row", "custom_conference_package"}

    STATE.put(
        "Payment Request",
        PASS_PR,
        name=PASS_PR,
        party="P-0005",
        party_type="Participant",
        status="Paid",
        docstatus=1,
        grand_total=10000.0,
        currency="LKR",
        reference_doctype="Conference",
        reference_name="UCIC 2026",
        custom_conference_package=package,
        email_to="login@example.com",
        receipt_sent_on=None,
        modified="2026-09-17 09:00:00",
    )
    STATE.put("Participant", "P-0005", full_name="Ranmal Perera", email="ranmal@example.com")
    STATE.put("Conference", "UCIC 2026", start_date=start_date)


class TestConferencePass:
    def test_a_pass_names_its_package_not_a_session(self):
        seed_pass()
        ctx = receipts.build_context(PASS_PR)

        assert ctx["pass_package"] == "Both Days"
        assert ctx["session_title"] == "Conference Pass - Both Days"
        assert ctx["conference"] == "UCIC 2026"
        # No session, so none of the session's own details.
        assert ctx["session_date"] is None and ctx["slot_from"] is None
        assert (ctx["amount"], ctx["currency"]) == (10000.0, "LKR")

    def test_both_days_lists_both_dates(self):
        seed_pass()
        # The stub's formatdate returns a fixed string; two of them means both
        # days were formatted.
        assert receipts.build_context(PASS_PR)["pass_days_display"].count(",") == 1

    def test_no_start_date_means_no_dates_rather_than_wrong_ones(self):
        seed_pass(start_date=None)
        assert receipts.build_context(PASS_PR)["pass_days_display"] is None

    def test_the_sweep_receipts_a_pass(self):
        seed_pass(package="Day 1")
        assert receipts.sweep() == {"sent": 1, "failed": 0}
        assert STATE.mails[0]["subject"] == "Payment receipt - Conference Pass - Day 1"


class TestSendOnce:
    def test_sends_and_stamps(self):
        seed()
        assert receipts.send_receipt(PR) is True
        assert len(STATE.mails) == 1
        assert STATE.mails[0]["recipients"] == ["ranmal@example.com"]
        assert STATE.records["Payment Request"][PR]["receipt_sent_on"] == STATE.now

    def test_a_replay_does_not_email_twice(self):
        seed()
        receipts.send_receipt(PR)
        assert receipts.send_receipt(PR) is False
        assert len(STATE.mails) == 1

    def test_force_is_how_an_organiser_resends(self):
        seed(receipt_sent_on="2026-09-16 10:00:00")
        assert receipts.send_receipt(PR, force=True) is True
        assert len(STATE.mails) == 1

    def test_no_recipient_is_logged_not_raised(self):
        seed(participant_email=None)
        STATE.records["Payment Request"][PR]["email_to"] = None

        assert receipts.send_receipt(PR) is False
        assert not STATE.mails
        assert STATE.errors and "no recipient" in STATE.errors[0][0].lower()

    def test_refuses_loudly_when_the_field_is_missing(self):
        seed()
        STATE.fields["Payment Request"] = {"slot_allocation_row"}

        # Without somewhere to record the send there is no way to avoid a
        # second one, so this must not "just send anyway".
        with pytest.raises(Exception, match="receipt_sent_on"):
            receipts.send_receipt(PR)


class TestSweep:
    def test_picks_up_an_unreceipted_paid_request(self):
        seed()
        assert receipts.sweep() == {"sent": 1, "failed": 0}
        assert len(STATE.mails) == 1

    def test_skips_one_already_receipted(self):
        seed(receipt_sent_on="2026-09-16 10:00:00")
        assert receipts.sweep() == {"sent": 0, "failed": 0}
        assert not STATE.mails

    def test_ignores_requests_that_are_not_paid(self):
        seed()
        STATE.records["Payment Request"][PR]["status"] = "Requested"
        assert receipts.sweep()["sent"] == 0

    def test_ignores_references_it_does_not_own(self):
        seed()
        # A Sales Invoice payment request belongs to somebody else entirely.
        STATE.records["Payment Request"][PR]["reference_doctype"] = "Sales Invoice"
        assert receipts.sweep()["sent"] == 0

    def test_does_not_email_ancient_payments(self):
        """The rail that stops an install mailing everyone who ever paid.

        `after_install` backfills receipt_sent_on on everything already settled,
        but that is one guard. This is the second, independent one: even an
        unstamped request is left alone once it is older than max_age_days.
        """
        seed()
        STATE.records["Payment Request"][PR]["modified"] = "2024-03-02 11:00:00"

        assert receipts.sweep()["sent"] == 0
        assert not STATE.mails

        # ...and it is only the age that excluded it.
        STATE.records["Payment Request"][PR]["modified"] = "2026-09-17 09:00:00"
        assert receipts.sweep()["sent"] == 1

    def test_max_age_can_be_widened_deliberately(self):
        seed()
        STATE.records["Payment Request"][PR]["modified"] = "2024-03-02 11:00:00"

        # Passing 0 means "no age limit" - for a deliberate catch-up run.
        assert receipts.sweep(max_age_days=0)["sent"] == 1

    def test_one_bad_row_does_not_stop_the_rest(self, monkeypatch):
        seed()
        STATE.put(
            "Payment Request",
            "ACC-PRQ-2026-00008",
            name="ACC-PRQ-2026-00008",
            party="P-0005",
            party_type="Participant",
            status="Paid",
            docstatus=1,
            grand_total=10.0,
            currency="LKR",
            reference_doctype="Conference Schedule",
            reference_name="CONF-SCH-08-2026-0024",
            email_to="login@example.com",
            receipt_sent_on=None,
            modified="2026-09-17 09:00:00",
        )

        real = receipts.send_receipt

        def explode(name, force=False):
            if name == PR:
                raise RuntimeError("smtp is down")
            return real(name, force=force)

        monkeypatch.setattr(receipts, "send_receipt", explode)

        result = receipts.sweep()

        assert result == {"sent": 1, "failed": 1}
        # The failed one is left unstamped, so the next sweep retries it -
        # the whole point of sweeping rather than sending from a callback.
        assert STATE.records["Payment Request"][PR]["receipt_sent_on"] is None
