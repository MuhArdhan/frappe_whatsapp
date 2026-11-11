"""Texts and timings of the WhatsApp <-> Helpdesk auto replies, editable from the desk.

An empty field falls back to DEFAULTS, so clearing a field restores the original text.
"""
from string import Formatter

import frappe
from frappe import _
from frappe.model.document import Document

DOCTYPE = "WhatsApp Helpdesk Settings"

DEFAULTS = {
	"menu_text": "Halo! Silakan pilih divisi yang ingin Anda hubungi:",
	"menu_footer": "Balas dengan angka pilihan Anda.",
	"confirm_text": (
		"Terima kasih, Anda sudah terhubung dengan tim *{team}*. "
		"Silakan sampaikan pertanyaan Anda, tim kami akan segera membalas. "
		"Ketik *menu* jika ingin pindah ke divisi lain."
	),
	"confirm_text_no_team": "Terima kasih, pesan Anda sudah kami terima. Tim kami akan segera membalas.",
	"closed_text": (
		"Percakapan Anda dengan tim *{team}* sudah selesai. Terima kasih! "
		"Jika ada pertanyaan lain, silakan kirim pesan kembali."
	),
	"closed_text_no_team": "Percakapan Anda sudah selesai. Terima kasih! Jika ada pertanyaan lain, silakan kirim pesan kembali.",
	"off_hours_text": (
		"Saat ini di luar jam kerja kami. Pesan Anda sudah kami terima dan akan dibalas mulai {opens}."
	),
	# not "0": a customer may answer an agent's question with 0
	"menu_keywords": "menu",
	# don't resend the menu for every message while the customer decides
	"menu_cooldown_minutes": 30,
	"off_hours_note_hours": 12,
}

# placeholders each text may use; anything else would break the reply at send time
PLACEHOLDERS = {
	"confirm_text": {"team"},
	"closed_text": {"team"},
	"off_hours_text": {"opens"},
}


class WhatsAppHelpdeskSettings(Document):
	def validate(self):
		for field, value in DEFAULTS.items():
			if not isinstance(value, str) or not self.get(field):
				continue
			try:
				used = {part[1] for part in Formatter().parse(self.get(field)) if part[1] is not None}
			except ValueError:
				used = None
			allowed = PLACEHOLDERS.get(field, set())
			if used is None or not used <= allowed:
				frappe.throw(
					_("{0}: only these placeholders are allowed: {1}. Write a literal brace as {{{{ or }}}}.").format(
						_(self.meta.get_label(field)), ", ".join("{" + p + "}" for p in sorted(allowed)) or "-"
					)
				)


def get(field):
	return frappe.db.get_single_value(DOCTYPE, field) or DEFAULTS[field]


def menu_keywords():
	return {k.strip().lower() for k in get("menu_keywords").split(",") if k.strip()}
