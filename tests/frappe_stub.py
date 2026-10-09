"""A minimal stand-in for the parts of `frappe` this app actually uses.

Receipt building is pure logic over a handful of reads, so stubbing the
framework lets the whole suite run with `pytest` and no bench, site or MySQL -
the same approach sl_payment_gateways/tests uses.

Only what the app calls is implemented, on purpose: if a future change starts
using some other part of frappe, the tests fail loudly here rather than quietly
exercising a mock that agrees with everything.
"""

import datetime
import os
import sys
import types

from jinja2 import FileSystemLoader
from jinja2.sandbox import SandboxedEnvironment


class ValidationError(Exception):
	pass


class PermissionError_(Exception):
	pass


class Stub:
	"""The state a test sets up, and the effects it then asserts on."""

	def __init__(self):
		self.reset()

	def reset(self):
		# doctype -> {name -> {field: value}}
		self.records = {}
		# single doctype -> {field: value}, as saved in tabSingles
		self.singles = {}
		# fields that "exist" on a doctype, as {doctype: set(fieldname)}
		self.fields = {}
		self.mails = []
		self.enqueued = []
		self.after_commit = []
		self.delivered = []
		self.smtp_error = None
		self.errors = []
		self.logs = []
		self.now = "2026-09-17 09:30:00"

	def put(self, doctype, docname, **values):
		# `docname`, not `name`: records carry their own `name` field, and a
		# parameter of that name would collide with it in the **values.
		self.records.setdefault(doctype, {})[docname] = values
		return docname


STATE = Stub()


class _Dict(dict):
	def __getattr__(self, key):
		try:
			return self[key]
		except KeyError as exc:
			raise AttributeError(key) from exc

	def __setattr__(self, key, value):
		self[key] = value


def _exists(doctype, filters=None):
	if doctype in ("DocField", "Custom Field"):
		parent = (filters or {}).get("parent") or (filters or {}).get("dt")
		return (filters or {}).get("fieldname") in STATE.fields.get(parent, set())

	if isinstance(filters, str):
		return filters in STATE.records.get(doctype, {})

	return bool(STATE.records.get(doctype, {}))


def _get_value(doctype, name, fieldname=None, as_dict=False, **kwargs):
	if isinstance(name, dict):
		# A filters dict: the first record matching every field, as Frappe does.
		name = next((n for n, r in STATE.records.get(doctype, {}).items() if _matches(r, name)), None)

	row = STATE.records.get(doctype, {}).get(name)

	if row is None:
		return None

	if as_dict:
		if isinstance(fieldname, (list, tuple)):
			return _Dict({f: row.get(f) for f in fieldname})
		return _Dict(row)

	if isinstance(fieldname, (list, tuple)):
		return [row.get(f) for f in fieldname]

	return row.get(fieldname)


def _set_value(doctype, name, field, value=None, **kwargs):
	row = STATE.records.setdefault(doctype, {}).setdefault(name, {})
	if isinstance(field, dict):
		row.update(field)
	else:
		row[field] = value


def _get_all(doctype, filters=None, fields=None, **kwargs):
	out = []
	for name, row in STATE.records.get(doctype, {}).items():
		if not _matches(row, filters or {}):
			continue
		out.append(_Dict({f: (name if f == "name" else row.get(f)) for f in (fields or ["name"])}))
	limit = kwargs.get("limit_page_length")
	return out[:limit] if limit else out


def _matches(row, filters):
	for field, cond in filters.items():
		value = row.get(field)
		if isinstance(cond, (list, tuple)):
			op, operand = cond
			if op == "in" and value not in operand:
				return False
			if op == ">=" and not (value or "") >= operand:
				return False
		elif value != cond:
			return False
	return True


class _EmailQueue:
	def __init__(self, kwargs):
		self.name = "EQ-%04d" % (len(STATE.mails),)
		self.kwargs = kwargs

	def send(self):
		if STATE.smtp_error:
			raise RuntimeError(STATE.smtp_error)
		STATE.delivered.append(self.kwargs)


def _sendmail(**kwargs):
	STATE.mails.append(kwargs)
	return _EmailQueue(kwargs)


class _AfterCommit:
	def add(self, fn):
		STATE.after_commit.append(fn)


def commit():
	"""Run what was registered to happen after commit, as Frappe does."""
	while STATE.after_commit:
		STATE.after_commit.pop(0)()


def _enqueue(method, queue=None, enqueue_after_commit=False, **kwargs):
	# Run it inline, as a worker would once the transaction commits.
	STATE.enqueued.append((method, kwargs))
	module, _, func = method.rpartition(".")
	return getattr(__import__(module, fromlist=[func]), func)(**kwargs)


def _log_error(title=None, message=None):
	STATE.errors.append((title, message))


# Real Jinja, sandboxed like Frappe's, loading templates the way Frappe does -
# relative to the app's root. The receipt's HTML is what a participant sees, so
# the tests render the actual file rather than trusting a mock of it.
_JENV = SandboxedEnvironment(loader=FileSystemLoader(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _render_template(template, context=None, is_path=None, safe_render=True):
	if is_path or (is_path is None and template.endswith(".html") and "\n" not in template):
		return _JENV.get_template(template).render(context or {})
	return _JENV.from_string(template).render(context or {})


def _get_singles_dict(doctype, **kwargs):
	return _Dict(STATE.singles.get(doctype, {}))


def build_module():
	frappe = types.ModuleType("frappe")

	frappe.ValidationError = ValidationError
	frappe.PermissionError = PermissionError_
	frappe._dict = _Dict
	frappe.STATE = STATE

	frappe.db = types.SimpleNamespace(
		exists=_exists, get_value=_get_value, get_singles_dict=_get_singles_dict, set_value=_set_value, sql=lambda *a, **k: None, commit=commit,
		after_commit=_AfterCommit(),
	)
	frappe.get_all = _get_all
	frappe.sendmail = _sendmail
	frappe.enqueue = _enqueue
	frappe.log_error = _log_error
	frappe.render_template = _render_template
	frappe.get_traceback = lambda: "traceback"
	frappe.logger = lambda name=None: types.SimpleNamespace(info=lambda m: STATE.logs.append(m))

	def _throw(msg, exc=ValidationError):
		raise exc(msg)

	frappe.throw = _throw

	utils = types.SimpleNamespace()
	utils.now = lambda: STATE.now
	utils.nowdate = lambda: STATE.now.split(" ")[0]
	utils.add_days = lambda d, n: (datetime.date.fromisoformat(str(d)[:10]) + datetime.timedelta(days=n)).isoformat()
	utils.escape_html = lambda s: str(s)
	utils.formatdate = lambda d, fmt=None: "24 August 2026"
	utils.format_datetime = lambda d, fmt=None: "17 September 2026, 09:25"
	utils.fmt_money = lambda v, currency=None: "%.2f" % float(v)
	utils.get_url = lambda path="": "https://ucic.example.org" + path
	utils.get_url_to_form = lambda doctype, name: "https://ucic.example.org/app/%s/%s" % (doctype.lower().replace(" ", "-"), name)
	frappe.utils = utils

	sys.modules["frappe"] = frappe
	return frappe
