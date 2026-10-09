"""The editable wording, and the layout it goes into.

What matters: an organiser's edits reach the email, an organiser's mistake
never stops a receipt going out, and a participant's own input can never
inject HTML into it.
"""

import datetime

import pytest

from tests.frappe_stub import STATE
from tests.test_receipts import PR, seed
from ucic_notifications import receipt_email, receipts

TPL = receipt_email.TEMPLATE_DOCTYPE


def saved(**fields):
    STATE.singles[TPL] = fields


def sent():
    receipts.send_receipt(PR, force=True)
    return STATE.mails[-1]


class TestEditableWording:
    def test_never_saved_template_sends_the_defaults(self):
        seed()
        mail = sent()

        assert mail["subject"] == "Payment receipt - 3-Minute Research"
        assert "Dear Ranmal Perera," in mail["message"]
        assert "computer-generated receipt" in mail["message"]

    def test_organiser_body_footer_and_subject_are_used(self):
        seed()
        saved(
            email_subject="Your {{ conference }} receipt",
            email_body="<p>Hi {{ participant }}, see you at {{ venue }}!</p>",
            email_footer="<p>UCSC, 35 Reid Avenue, Colombo 7</p>",
        )
        mail = sent()

        assert mail["subject"] == "Your UCIC 2026 receipt"
        assert "Hi Ranmal Perera, see you at Hall A!" in mail["message"]
        assert "35 Reid Avenue" in mail["message"]
        assert "computer-generated" not in mail["message"]

    def test_blank_body_and_subject_fall_back_but_a_cleared_footer_stays_cleared(self):
        seed()
        saved(email_subject="", email_body="", email_footer="", brand_color="#1f3a68")
        mail = sent()

        assert mail["subject"] == "Payment receipt - 3-Minute Research"
        assert "Dear Ranmal Perera," in mail["message"]
        assert "computer-generated" not in mail["message"]

    def test_broken_placeholder_is_logged_and_the_receipt_still_goes(self):
        seed()
        saved(email_body="<p>Hi {{ participant.nope( }}</p>", email_footer="<p>ok</p>")

        assert receipts.send_receipt(PR) is True
        assert "Dear Ranmal Perera," in STATE.mails[-1]["message"]
        assert STATE.errors and "email_body" in STATE.errors[0][0]


class TestLayout:
    def test_participant_input_cannot_inject_html(self):
        seed()
        STATE.records["Participant"]["P-0005"]["full_name"] = "<img src=x onerror=alert(1)>"
        message = sent()["message"]

        assert "<img src=x" not in message
        assert "&lt;img src=x" in message

    def test_receipt_shows_the_reference_id(self):
        seed(with_slot=False)
        STATE.fields["Payment Request"].add("custom_gateway_transaction_id")
        STATE.records["Payment Request"][PR]["custom_gateway_transaction_id"] = "7902294473696640104010"
        message = sent()["message"]

        assert "Reference ID" in message
        assert "7902294473696640104010" in message

    def test_details_show_times_without_seconds_even_from_a_timedelta(self):
        seed()
        STATE.records["Slot Allocations"]["row-abc"]["from_time"] = datetime.timedelta(hours=9, minutes=5)
        message = sent()["message"]

        assert "09:05 - 10:18" in message

    def test_amount_is_shown_once_without_a_doubled_currency(self):
        seed()
        message = sent()["message"]

        assert "10.00" in message
        assert "LKR 10.00" not in message  # fmt_money already carries the symbol

    def test_logo_becomes_an_absolute_url(self):
        seed()
        saved(logo="/files/ucic-logo.png")

        assert 'src="https://ucic.example.org/files/ucic-logo.png"' in sent()["message"]

    def test_light_brand_colour_gets_dark_header_text(self):
        seed()
        saved(brand_color="#fde68a")
        assert "color:#111827" in sent()["message"]

    def test_invalid_colour_falls_back_to_the_default(self):
        seed()
        saved(brand_color="red; background:url(x)")
        message = sent()["message"]

        assert "url(x)" not in message
        assert receipt_email.DEFAULT_BRAND_COLOR in message


class TestValidateOnSave:
    def test_rejects_broken_jinja(self):
        with pytest.raises(Exception, match="Footer"):
            receipt_email.validate_template({"email_footer": "{% if %}"})

    def test_rejects_a_non_hex_colour(self):
        with pytest.raises(Exception, match="hex"):
            receipt_email.validate_template({"brand_color": "blue"})

    def test_accepts_the_defaults(self):
        receipt_email.validate_template(dict(receipt_email.DEFAULTS))
