"""Self-check for the WhatsApp <-> Helpdesk bridge.

Runs the whole flow against the real site inside one transaction and rolls it
back, so it is safe on a dev site with data:

    bench --site <site> execute frappe_whatsapp.frappe_whatsapp.tests.test_helpdesk_bridge.run

Meta API calls are mocked; nothing is sent to WhatsApp.
"""
import hashlib
import hmac
import os
import random
import types
from unittest.mock import patch

import frappe

from frappe_whatsapp import helpdesk
from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_message.whatsapp_message import WhatsAppMessage


def run():
	sent = []
	try:
		with (
			patch.object(WhatsAppMessage, "notify", lambda self, data: sent.append(data)),
			patch.object(WhatsAppMessage, "upload_media", lambda self, f: "MEDIA123"),
			# routing swallows errors into Error Log; surface them here
			patch.object(helpdesk.frappe, "log_error", _reraise),
		):
			_check_flow(sent)
			_check_signature()
		print("helpdesk bridge self-check: OK")
	finally:
		frappe.db.rollback()
		for path in _disk_files:  # rollback doesn't remove files written to disk
			if os.path.exists(path):
				os.remove(path)


_disk_files = []


def _make_file(file_name, content):
	f = frappe.get_doc({"doctype": "File", "file_name": file_name, "content": content}).insert(ignore_permissions=True)
	_disk_files.append(f.get_full_path())
	return f.file_url


def _reraise(*args, **kwargs):
	raise  # noqa: PLE0704 -- called from inside an except block


def _incoming(number, account, **fields):
	return frappe.get_doc(
		{"doctype": "WhatsApp Message", "type": "Incoming", "from": number, "whatsapp_account": account,
		 "profile_name": "Budi Test", "content_type": "text", **fields}
	).insert(ignore_permissions=True)


def _check_flow(sent):
	account = frappe.get_doc(
		{"doctype": "WhatsApp Account", "account_name": "wa-selfcheck", "status": "Active",
		 "phone_id": "selfcheck-" + frappe.generate_hash(length=6), "url": "https://graph.example", "version": "v0",
		 "webhook_verify_token": frappe.generate_hash(length=8)}
	).insert(ignore_permissions=True).name
	team = frappe.get_doc(
		{"doctype": "HD Team", "team_name": "WA Selfcheck " + frappe.generate_hash(length=4),
		 "users": [{"user": "Administrator"}]}
	).insert(ignore_permissions=True).name
	number = f"62999{random.randint(1000000, 9999999)}"

	# 1. first message -> team menu, no ticket yet
	_incoming(number, account, message="Pesanan saya belum sampai")
	menus = [d for d in sent if d.get("type") == "interactive" and d["to"] == number]
	assert len(menus) == 1, sent
	assert not helpdesk.open_ticket(number)

	# 2. another message within the cooldown -> no second menu
	_incoming(number, account, message="Tolong dicek ya")
	assert len([d for d in sent if d.get("type") == "interactive" and d["to"] == number]) == 1

	# 3. customer picks the team -> ticket for that team, waiting messages pulled in
	_incoming(number, account, content_type="button", message=helpdesk.TEAM_PREFIX + team)
	ticket = helpdesk.open_ticket(number)
	assert ticket, "ticket not created"
	t = frappe.get_doc("HD Ticket", ticket)
	assert t.agent_group == team, t.agent_group
	assert t.subject == "Pesanan saya belum sampai", t.subject
	assert t.contact
	received = frappe.get_all("Communication", {"reference_name": ticket, "sent_or_received": "Received"}, pluck="content", order_by="creation asc")
	assert len(received) == 2 and "belum sampai" in received[0], received

	# 4. media on the open ticket -> communication + file on the ticket
	image_url = _make_file("selfcheck-wa.png", b"\x89PNG fake")
	_incoming(number, account, content_type="image", message="", attach=image_url)
	assert frappe.db.exists("File", {"attached_to_doctype": "HD Ticket", "attached_to_name": ticket, "file_url": image_url})
	assert len(frappe.get_all("Communication", {"reference_name": ticket, "sent_or_received": "Received"})) == 3

	# 5. WhatsApp tickets skip the email workflow
	assert t.skip_email_workflow() is True

	# 6. agent reply (text + attachment) -> WhatsApp
	comm = frappe.get_doc(
		{"doctype": "Communication", "communication_type": "Communication", "communication_medium": "",
		 "sent_or_received": "Sent", "content": "<p>Baik, kami <b>cek</b> dulu.</p>",
		 "reference_doctype": "HD Ticket", "reference_name": ticket, "subject": "Re"}
	).insert(ignore_permissions=True)
	t.attach_file_with_doc("Communication", comm.name, _make_file("selfcheck-invoice.txt", b"invoice"))
	sent.clear()
	with patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"):
		helpdesk.send_reply(comm.name, number)
	assert sent[0]["type"] == "text" and "Baik, kami **cek** dulu." in sent[0]["text"]["body"], sent
	assert sent[1]["type"] == "document" and sent[1]["document"] == {"id": "MEDIA123", "filename": "selfcheck-invoice.txt"}, sent

	# 7. a failed send is noted on the ticket instead of being lost
	def boom(self, data):
		raise Exception("token expired")
	with (
		patch.object(WhatsAppMessage, "notify", boom),
		patch.object(frappe.db, "commit"),
		patch.object(frappe.db, "rollback"),
		patch.object(helpdesk.frappe, "log_error"),
	):
		helpdesk.send_reply(comm.name, number)
	assert frappe.db.exists("Comment", {"reference_name": ticket, "content": ["like", "%token expired%"]})


def _check_signature():
	from frappe_whatsapp.utils.webhook import verify_signature

	account = frappe.get_doc("WhatsApp Account", "wa-selfcheck")
	account.app_secret = "s3cret"
	account.save(ignore_permissions=True)
	body = b'{"entry": []}'
	good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()

	old_request = getattr(frappe.local, "request", None)
	frappe.local.request = types.SimpleNamespace(get_data=lambda: body)
	try:
		with patch("frappe_whatsapp.utils.webhook.frappe.get_request_header", lambda h: good):
			verify_signature()
		with patch("frappe_whatsapp.utils.webhook.frappe.get_request_header", lambda h: "sha256=bad"):
			try:
				verify_signature()
			except frappe.PermissionError:
				pass
			else:
				raise AssertionError("bad signature accepted")
	finally:
		frappe.local.request = old_request
