"""The receipt email: the organiser-editable wording, and the layout around it.

TWO LAYERS, ON PURPOSE
----------------------
- WHAT IT SAYS - subject, opening message, footer, logo and brand colour -
  lives in the "Receipt Email Template" single DocType, so an organiser can
  reword it in Desk without a deploy. The text fields are Jinja, with the
  receipt's own values as placeholders: {{ participant }}, {{ session_title }}.
- HOW IT LOOKS - header band, PAID badge, amount, details table - stays in
  templates/emails/payment_receipt.html. Email HTML is nested tables and inline
  styles that have to survive Gmail, Outlook and a phone; that is not something
  to hand to a rich-text editor.

An organiser's mistake must never cost a participant their receipt, so a part
that fails to render is logged and replaced by its default - the mail still
goes out. `validate_template` catches the same mistakes earlier, on save.
"""

import html
import re

import frappe

TEMPLATE_DOCTYPE = "Receipt Email Template"
LAYOUT = "ucic_notifications/templates/emails/payment_receipt.html"

# Plain Jinja with no quoted strings: the Text Editor may store quotes as
# entities, which would break a `{{ x or "y" }}` an organiser copied from here.
DEFAULT_SUBJECT = "Payment receipt{% if session_title %} - {{ session_title }}{% else %} {{ receipt_no }}{% endif %}"

DEFAULT_BODY = (
	"<p>{% if participant %}Dear {{ participant }},{% else %}Hello,{% endif %}</p>"
	"<p>Thank you - we have received your payment"
	"{% if session_title %} for <strong>{{ session_title }}</strong>{% endif %}. "
	"Your receipt is below; please keep it for your records.</p>"
)

DEFAULT_FOOTER = (
	"<p>This is a computer-generated receipt and needs no signature.</p>"
	"<p>If anything here looks wrong, simply reply to this email and the organising team will help.</p>"
)

DEFAULT_BRAND_COLOR = "#1f3a68"

DEFAULTS = {
	"email_subject": DEFAULT_SUBJECT,
	"email_body": DEFAULT_BODY,
	"email_footer": DEFAULT_FOOTER,
	"brand_color": DEFAULT_BRAND_COLOR,
}

EDITABLE_FIELDS = ("email_subject", "email_body", "email_footer", "header_title", "logo", "brand_color")

JINJA_FIELDS = (
	("email_subject", "Subject"),
	("email_body", "Body"),
	("email_footer", "Footer"),
)

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def get_template(overrides=None):
	"""The template as saved, with blanks filled in.

	Subject and body fall back to their defaults when blank - an email with no
	subject is never what anybody meant. The footer does NOT: clearing it is how
	an organiser says "no footer". A template that has never been saved at all
	gets every default, footer included.
	"""
	saved = dict(frappe.db.get_singles_dict(TEMPLATE_DOCTYPE) or {})
	saved.update(overrides or {})

	if not any(saved.get(field) for field in DEFAULTS):
		saved = dict(DEFAULTS, **{k: v for k, v in saved.items() if v})

	return frappe._dict(
		email_subject=saved.get("email_subject") or DEFAULT_SUBJECT,
		email_body=saved.get("email_body") or DEFAULT_BODY,
		email_footer=saved.get("email_footer") or "",
		header_title=saved.get("header_title") or "",
		logo=saved.get("logo") or "",
		brand_color=saved.get("brand_color") if _HEX.match(saved.get("brand_color") or "") else DEFAULT_BRAND_COLOR,
	)


def render(context, overrides=None):
	"""(subject, html message) for one receipt context from receipts.build_context."""
	tpl = get_template(overrides)

	# The organiser's Jinja lands in HTML, so the values it pulls in are escaped:
	# a participant who registered as "<b>Bob</b>" gets exactly that, as text.
	escaped = {k: html.escape(v) if isinstance(v, str) else v for k, v in context.items()}

	subject = _render_part("email_subject", tpl.email_subject, context)
	subject = re.sub(r"\s+", " ", subject).strip()

	brand = tpl.brand_color
	on_brand = _text_on(brand)

	layout = dict(
		context,
		body_html=_render_part("email_body", tpl.email_body, escaped),
		footer_html=_render_part("email_footer", tpl.email_footer, escaped) if tpl.email_footer else "",
		header_title=tpl.header_title or context.get("conference") or "Payment receipt",
		logo_url=_absolute_url(tpl.logo),
		details=details_rows(context),
		preheader="Receipt %s - %s paid" % (context.get("receipt_no"), context.get("amount_display")),
		brand_color=brand,
		brand_text=on_brand,
		brand_muted=_mix(on_brand, brand, 0.72),
		brand_tint=_mix(brand, "#ffffff", 0.06),
		brand_line=_mix(brand, "#ffffff", 0.14),
	)

	return subject, frappe.render_template(LAYOUT, layout)


def _render_part(field, source, context):
	try:
		return frappe.render_template(source, context, is_path=False)
	except Exception:
		frappe.log_error(
			title="Receipt Email Template: %s did not render" % (field,),
			message="Fell back to the default text, so the receipt still went out. "
			"Open Receipt Email Template and fix the placeholders.\n\n%s" % (frappe.get_traceback(),),
		)
		return frappe.render_template(DEFAULTS[field], context, is_path=False)


def details_rows(context):
	"""The label/value rows of the details table, only for what this receipt has."""
	c = context
	rows = [("Receipt no.", c.get("receipt_no"))]

	if c.get("pass_package"):
		rows.append(("Conference pass", c["pass_package"]))
		if c.get("pass_days_display"):
			rows.append(("Valid on", c["pass_days_display"]))
	elif c.get("session_title"):
		rows.append(("Session", c["session_title"]))

	if c.get("conference"):
		rows.append(("Conference", c["conference"]))
	if c.get("session_date_display"):
		rows.append(("Date", c["session_date_display"]))

	if c.get("slot_from") and c.get("slot_to"):
		rows.append(("Your time slot", "%s - %s" % (_hm(c["slot_from"]), _hm(c["slot_to"]))))
	elif c.get("session_start") and c.get("session_end"):
		rows.append(("Session time", "%s - %s" % (_hm(c["session_start"]), _hm(c["session_end"]))))

	if c.get("venue"):
		rows.append(("Venue", c["venue"]))
	if c.get("gateway"):
		rows.append(("Paid via", c["gateway"]))
	if c.get("gateway_reference"):
		rows.append(("Transaction ref.", c["gateway_reference"]))

	rows.append(("Paid on", c.get("paid_on_display")))

	return [(label, str(value)) for label, value in rows if value]


def sample_context():
	"""A realistic receipt for previews, test emails and validating the template."""
	return {
		"recipient": None,
		"receipt_no": "ACC-PRQ-2026-00001",
		"participant": "Sample Participant",
		"session_title": "3-Minute Research",
		"conference": "UCIC 2026",
		"pass_package": None,
		"pass_days_display": None,
		"session_date": "2026-08-24",
		"venue": "Hall A",
		"slot_from": "10:15:00",
		"slot_to": "10:18:00",
		"session_start": "10:00:00",
		"session_end": "12:00:00",
		"gateway": "WebXPay",
		"gateway_reference": "WX-00000",
		"paid_on": "2026-08-20 09:25:00",
		"amount": 2500.0,
		"currency": "LKR",
		"session_date_display": "24 August 2026",
		"paid_on_display": "20 August 2026, 09:25",
		"amount_display": "LKR 2,500.00",
	}


def validate_template(doc):
	"""Refuse to save a template that would not render - called from the DocType."""
	color = doc.get("brand_color")
	if color and not _HEX.match(color):
		frappe.throw("Brand Colour must be a hex colour such as #1f3a68.")

	context = sample_context()

	for field, label in JINJA_FIELDS:
		source = doc.get(field)
		if not source:
			continue
		try:
			frappe.render_template(source, context, is_path=False)
		except Exception as exc:
			frappe.throw("%s has a placeholder problem and cannot be used: %s" % (label, html.escape(str(exc))))


def _hm(value):
	"""10:15 from "10:15:00" or a timedelta - Frappe returns Time fields as either."""
	parts = str(value).split(":")
	try:
		return "%02d:%02d" % (int(parts[0]), int(parts[1]))
	except (ValueError, IndexError):
		return str(value)


def _absolute_url(path):
	# A mail client has no site to resolve "/files/logo.png" against.
	if not path:
		return ""
	return path if path.startswith(("http://", "https://")) else frappe.utils.get_url(path)


def _rgb(hex_color):
	return tuple(int(hex_color[i : i + 2], 16) for i in (1, 3, 5))


def _mix(color, base, amount):
	"""`amount` of `color` over `base` - a tint that works where rgba() does not (Outlook)."""
	c, b = _rgb(color), _rgb(base)
	return "#%02x%02x%02x" % tuple(round(b[i] + (c[i] - b[i]) * amount) for i in range(3))


def _text_on(background):
	"""White on a dark brand colour, near-black on a light one."""
	r, g, b = (v / 255 for v in _rgb(background))
	return "#ffffff" if (0.299 * r + 0.587 * g + 0.114 * b) < 0.6 else "#111827"
