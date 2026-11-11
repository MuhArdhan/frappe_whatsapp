"""Self-check for the WhatsApp <-> Helpdesk bridge.

Runs the whole flow against the real site inside one transaction and rolls it
back, so it is safe on a dev site with data:

    bench --site <site> execute frappe_whatsapp.frappe_whatsapp.tests.test_helpdesk_bridge.run

Meta and Evolution API calls are mocked; nothing is sent to WhatsApp.
"""
import base64
import hashlib
import hmac
import os
import random
import types
from unittest.mock import patch

import frappe

from frappe_whatsapp import helpdesk
from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_message.whatsapp_message import WhatsAppMessage
from frappe_whatsapp.utils import evolution


def run():
	sent = []
	try:
		with (
			patch.object(WhatsAppMessage, "notify", lambda self, data: sent.append(data)),
			patch.object(WhatsAppMessage, "upload_media", lambda self, f: "MEDIA123"),
			# routing swallows errors into Error Log; surface them here
			patch.object(helpdesk.frappe, "log_error", _reraise),
		):
			_check_format_and_contact()
			_check_off_hours()
			# flows below must not depend on the clock
			with patch.object(helpdesk, "off_hours_note", lambda *a, **k: None):
				_check_flow(sent)
				_check_signature()
				_check_evolution()
				_check_departments()
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
	assert t.raised_by == f"{number}@whatsapp.invalid", t.raised_by  # fills the reply box's To
	confirm = [d for d in sent if d.get("type") == "text" and d["to"] == number]
	assert len(confirm) == 1 and f"tim *{team}*." in confirm[0]["text"]["body"] and "(" not in confirm[0]["text"]["body"], sent
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
	assert sent[0]["type"] == "text" and "Baik, kami *cek* dulu." in sent[0]["text"]["body"], sent
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


def _check_format_and_contact():
	# agent editor HTML -> WhatsApp markup, no markdown or backslash escapes
	text, *_ = helpdesk.split_inline_images(
		"<p>Halo <b>Budi</b>, <i>cek</i> <s>lama</s> a_b 2*3</p><ul><li>satu</li><li>dua</li></ul>"
		'<h3>Judul</h3><p><a href="https://x.id/a">link</a> <a href="https://x.id">https://x.id</a></p>'
	)
	assert text == "Halo *Budi*, _cek_ ~lama~ a_b 2*3\n\n• satu\n• dua\n\n*Judul*\n\nlink (https://x.id/a) https://x.id", repr(text)

	# the helpdesk Reply button's quote is not sent back to the customer
	text, *_ = helpdesk.split_inline_images('<p>oke kak</p><p class="reply-to-content"></p><blockquote><p>pesan lama</p></blockquote>')
	assert text == "oke kak", repr(text)

	# a contact saved as 0812-... is found for 62812...
	number = f"62997{random.randint(1000000, 9999999)}"
	contact = frappe.get_doc({"doctype": "Contact", "first_name": "Format Test"})
	contact.add_phone(f"0{number[2:5]}-{number[5:9]} {number[9:]}", is_primary_mobile_no=1)
	contact.insert(ignore_permissions=True)
	assert helpdesk.get_or_create_contact(number, "x") == contact.name
	assert helpdesk.find_contact("1" + number) is None


def _check_off_hours():
	from datetime import datetime, timedelta

	sla = frappe.new_doc("HD Service Level Agreement")
	for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"):
		sla.append("support_and_resolution", {"workday": day, "start_time": timedelta(hours=10), "end_time": timedelta(hours=18)})
	with patch.object(helpdesk.frappe, "get_cached_doc", lambda *a: sla):
		note = lambda *args: helpdesk.off_hours_note("x", datetime(2026, 11, *args))
		assert note(11, 12, 0) is None  # Wednesday noon
		assert note(11, 8, 0).endswith("mulai Rabu, 11 November pukul 10.00."), note(11, 8, 0)
		assert note(13, 19, 30).endswith("mulai Senin, 16 November pukul 10.00."), note(13, 19, 30)
		assert note(14, 12, 0).startswith(helpdesk.OFF_HOURS_PREFIX) and "Senin, 16 November" in note(14, 12, 0)


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


def _check_evolution():
	"""Same helpdesk flow over the Evolution API provider, with a numbered text menu."""
	account = frappe.get_doc(
		{"doctype": "WhatsApp Account", "account_name": "wa-selfcheck-evo", "status": "Active",
		 "provider": "Evolution API", "evolution_instance": "selfcheck-" + frappe.generate_hash(length=6),
		 "url": "http://evolution.example", "token": "k", "webhook_verify_token": "evo-secret"}
	).insert(ignore_permissions=True)
	team = frappe.get_doc(
		{"doctype": "HD Team", "team_name": "WA Evo " + frappe.generate_hash(length=4), "users": [{"user": "Administrator"}]}
	).insert(ignore_permissions=True).name
	number = f"62998{random.randint(1000000, 9999999)}"
	calls = []

	def fake_api(acc, method, path, payload=None):
		calls.append((path, payload))
		if "getBase64" in path:
			return {"base64": base64.b64encode(b"\x89PNG evo").decode()}
		return {"key": {"id": f"EV{len(calls)}"}}

	def evo_msg(msg_id, message, jid=None):
		return {"key": {"remoteJid": jid or f"{number}@s.whatsapp.net", "fromMe": False, "id": msg_id},
				"pushName": "Sari Evo", "message": message, "messageType": next(iter(message), "")}

	with patch.object(evolution, "api", fake_api), patch.object(helpdesk, "active_teams", lambda: ["Other Team", team]):
		# 1. first message -> numbered text menu via sendText
		evolution.save_incoming(account, evo_msg("E1", {"conversation": "Mesin kasir error"}))
		path, body = calls[-1]
		assert path == f"/message/sendText/{account.evolution_instance}", calls
		assert body["number"] == number and f"2. {team}" in body["text"], body
		assert frappe.db.get_value("WhatsApp Message", {"message_id": "EV1"}, "status") == "Success"

		# 2. invalid number -> menu again (forced), still no ticket
		evolution.save_incoming(account, evo_msg("E2", {"conversation": "9"}))
		assert not helpdesk.open_ticket(number)
		assert sum(1 for p, _ in calls if "sendText" in p) == 2

		# 3. a team added after the menu went out must not shift the numbering
		with patch.object(helpdesk, "active_teams", lambda: ["Another", "Other Team", team]):
			# reply "2" -> ticket for that team; "2" itself is not logged as conversation
			evolution.save_incoming(account, evo_msg("E3", {"extendedTextMessage": {"text": "2"}}))
		ticket = helpdesk.open_ticket(number)
		assert ticket and frappe.db.get_value("HD Ticket", ticket, "agent_group") == team
		confirm = calls[-1]
		assert confirm[0].startswith("/message/sendText/") and f"tim *{team}*." in confirm[1]["text"], calls
		assert frappe.db.get_value("HD Ticket", ticket, "subject") == "Mesin kasir error"
		received = frappe.get_all("Communication", {"reference_name": ticket, "sent_or_received": "Received"}, pluck="content")
		assert "2" not in received and any("kasir" in c for c in received), received

		# 4. image without inline base64 -> fetched, stored privately, attached to the ticket
		evolution.save_incoming(account, evo_msg("E4", {"imageMessage": {"mimetype": "image/png", "caption": "foto"}}))
		url = frappe.db.get_value("WhatsApp Message", {"message_id": "E4"}, "attach")
		assert url and url.startswith("/private/files/"), url
		_disk_files.append(frappe.get_doc("File", {"file_url": url}).get_full_path())
		assert frappe.db.exists("File", {"attached_to_doctype": "HD Ticket", "attached_to_name": ticket, "file_url": url})

		# 5. duplicates, own messages and groups are ignored
		before = frappe.db.count("WhatsApp Message")
		evolution.save_incoming(account, evo_msg("E4", {"conversation": "dup"}))
		evolution.save_incoming(account, {**evo_msg("E5", {"conversation": "me"}), "key": {"remoteJid": f"{number}@s.whatsapp.net", "fromMe": True, "id": "E5"}})
		evolution.save_incoming(account, evo_msg("E6", {"conversation": "grup"}, jid="12345@g.us"))
		assert frappe.db.count("WhatsApp Message") == before

		# 6. agent reply with an inline image (name with spaces) and an attachment -> text + image + document
		inline = _make_file("selfcheck evo inline.png", b"PNG inline")
		comm = frappe.get_doc(
			{"doctype": "Communication", "communication_type": "Communication", "communication_medium": "",
			 "sent_or_received": "Sent",
			 "content": f'<p>Sudah kami cek</p><p><img src="{inline.replace(" ", "%20")}"></p>',
			 "reference_doctype": "HD Ticket", "reference_name": ticket, "subject": "Re"}
		).insert(ignore_permissions=True)
		frappe.get_doc("HD Ticket", ticket).attach_file_with_doc("Communication", comm.name, _make_file("selfcheck-evo.txt", b"nota"))
		calls.clear()
		with patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"):
			helpdesk.send_reply(comm.name, number)
		assert calls[0][0].startswith("/message/sendText/") and calls[0][1]["text"] == "Sudah kami cek", calls
		assert calls[1][1]["mediatype"] == "image" and calls[1][1]["fileName"] == "selfcheck evo inline.png", calls
		media = calls[2][1]
		assert calls[2][0].startswith("/message/sendMedia/") and media["mediatype"] == "document", calls
		assert media["fileName"] == "selfcheck-evo.txt" and base64.b64decode(media["media"]) == b"nota"

		# 6b. ticket resolved, customer writes again within the menu cooldown -> fresh menu, then a new ticket
		frappe.db.set_value("HD Ticket", ticket, {"status": "Resolved", "status_category": "Resolved"})
		calls.clear()
		evolution.save_incoming(account, evo_msg("E7", {"conversation": "halo lagi"}))
		assert calls and f"2. {team}" in calls[-1][1]["text"], calls
		evolution.save_incoming(account, evo_msg("E8", {"conversation": "2"}))
		new_ticket = helpdesk.open_ticket(number)
		assert new_ticket and new_ticket != ticket, (new_ticket, ticket)
		assert frappe.db.get_value("HD Ticket", new_ticket, "subject") == "halo lagi"
		assert "Ketik *menu*" in calls[-1][1]["text"], calls

		# 6c. "menu" on an open ticket -> that ticket closed, menu again, the choice opens a new ticket
		calls.clear()
		queued = []
		with patch.object(helpdesk.frappe, "enqueue", lambda fn, **kw: queued.append(fn)):
			evolution.save_incoming(account, evo_msg("E9", {"conversation": " Menu "}))
		assert helpdesk.send_closing_message not in queued, queued  # menu switch: no goodbye
		assert frappe.db.get_value("HD Ticket", new_ticket, "status") == "Closed"
		assert not helpdesk.open_ticket(number)
		assert calls and f"2. {team}" in calls[-1][1]["text"], calls
		evolution.save_incoming(account, evo_msg("E10", {"conversation": "2"}))
		third = helpdesk.open_ticket(number)
		assert third and third not in (ticket, new_ticket), third
		assert frappe.db.get_value("HD Ticket", third, "subject") != "Menu"
		assert frappe.db.get_value("WhatsApp Message", {"message_id": "E9"}, "reference_name") == new_ticket

		# 6e. off hours: messages on the open ticket get one note, not one per message
		calls.clear()
		note = helpdesk.OFF_HOURS_PREFIX + " Pesan Anda sudah kami terima dan akan dibalas mulai Senin."
		with patch.object(helpdesk, "off_hours_note", lambda *a, **k: note):
			evolution.save_incoming(account, evo_msg("E11", {"conversation": "malam kak"}))
			evolution.save_incoming(account, evo_msg("E12", {"conversation": "masih ada orang?"}))
		assert [b["text"] for p, b in calls if "sendText" in p] == [note], calls
		assert frappe.db.get_value("WhatsApp Message", {"message_id": "E12"}, "reference_name") == third

		# 6f. customer quotes a message: the ticket shows what they replied to
		quoted_id = frappe.db.get_value("WhatsApp Message", {"type": "Outgoing", "to": number, "message": note}, "message_id")
		reply = lambda mid, text, quoted: {"extendedTextMessage": {"text": text, "contextInfo": {"stanzaId": quoted, "quotedMessage": {"conversation": "dari HP"}}}}
		evolution.save_incoming(account, evo_msg("E13", reply("E13", "jam berapa?", quoted_id)))
		evolution.save_incoming(account, evo_msg("E14", reply("E14", "yang ini", "UNKNOWN-ID")))  # sent from the phone
		latest = frappe.get_all("Communication", {"reference_name": third, "sent_or_received": "Received"}, pluck="content", order_by="creation desc", limit=2)
		assert "Membalas:</i> " + helpdesk.OFF_HOURS_PREFIX in latest[1] and "jam berapa?" in latest[1], latest
		assert "Membalas:</i> dari HP" in latest[0] and "yang ini" in latest[0], latest

		# 6g. agent uses Reply on the "jam berapa?" row -> WhatsApp reply quoting E13, quote not sent as text
		def agent_reply(html):
			comm = frappe.get_doc(
				{"doctype": "Communication", "communication_type": "Communication", "communication_medium": "",
				 "sent_or_received": "Sent", "content": html, "reference_doctype": "HD Ticket", "reference_name": third, "subject": "Re"}
			).insert(ignore_permissions=True)
			calls.clear()
			with patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"):
				helpdesk.send_reply(comm.name, number)
			return calls[0][1]

		# the editor re-renders the stored HTML, so whitespace differs
		rendered = latest[1].replace("<br>", "<br/>\n  ")
		body = agent_reply(f'<p>Jam 10 kak</p><p class="reply-to-content"></p><blockquote>{rendered}</blockquote>')
		assert body["text"] == "Jam 10 kak" and body["quoted"] == {"key": {"id": "E13"}}, body
		body = agent_reply('<p>Oke</p><p class="reply-to-content"></p><blockquote><p>tidak ada di ticket</p></blockquote>')
		assert body["text"] == "Oke" and "quoted" not in body, body

		# 6d. agent resolves -> one closing message; Resolved -> Closed sends no second one
		calls.clear()
		run_now = lambda fn, enqueue_after_commit=False, **kw: fn(**kw)
		with patch.object(helpdesk.frappe, "enqueue", run_now), patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"):
			for status in ("Resolved", "Closed"):
				t3 = frappe.get_doc("HD Ticket", third)
				t3.status = status
				t3.save(ignore_permissions=True)
		texts = [b["text"] for p, b in calls if "sendText" in p]
		assert texts == [helpdesk.CLOSED_TEXT.format(team=team)], calls

	# 7. webhook token: right token for the instance passes, wrong one is refused
	with patch("frappe_whatsapp.utils.evolution.frappe.get_request_header", lambda h: "evo-secret"):
		assert evolution.authenticate(account.evolution_instance).name == account.name
	with patch("frappe_whatsapp.utils.evolution.frappe.get_request_header", lambda h: "wrong"):
		try:
			evolution.authenticate(account.evolution_instance)
		except frappe.PermissionError:
			pass
		else:
			raise AssertionError("wrong webhook token accepted")

	# 8. privacy ids resolve to the phone number
	assert evolution.sender_number({"remoteJid": "abc@lid", "remoteJidAlt": f"{number}@s.whatsapp.net"}) == number

	# 9. connection monitor alerts once per outage, again after it recovered and dropped
	states = iter(["open", "close", "close", "open", "connecting"])

	def state_api(acc, method, path, payload=None):
		return {"instance": {"state": next(states) if acc.name == account.name else "open"}}

	alerts = lambda: frappe.db.count("Notification Log", {"document_name": account.name, "subject": ["like", "%terputus%"]})
	key = f"wa_evolution_down:{account.name}"
	frappe.cache.delete_value(key)
	recipients = len(evolution.alert_recipients(account))
	assert recipients, "no one to alert"
	try:
		with patch.object(evolution, "api", state_api):
			for expected in (0, 1, 1, 1, 2):
				evolution.check_connections()
				assert alerts() == expected * recipients, (alerts(), expected)
	finally:
		frappe.cache.delete_value(key)


def _check_departments():
	"""Ticked departments become HD Teams; same-named departments of two companies share one."""
	from frappe_whatsapp import helpdesk_departments as hd

	companies = frappe.get_all("Company", pluck="name", limit=2, order_by="name asc")
	gender = frappe.get_all("Gender", pluck="name", limit=1)[0]
	dept_name = "WA Dept " + frappe.generate_hash(length=4)
	manual = {t.name: t.disabled for t in frappe.get_all("HD Team", fields=["name", "disabled"])}

	def employee(company):
		user = frappe.get_doc(
			{"doctype": "User", "email": f"wa-{frappe.generate_hash(length=8)}@example.com",
			 "first_name": "Dept Test", "send_welcome_email": 0}
		).insert(ignore_permissions=True).name
		dept = frappe.get_doc(
			{"doctype": "Department", "department_name": dept_name, "company": company, hd.FLAG: 1}
		).insert(ignore_permissions=True).name
		emp = frappe.get_doc(
			{"doctype": "Employee", "first_name": "Dept Test", "gender": gender, "date_of_birth": "1990-01-01",
			 "date_of_joining": "2020-01-01", "company": company, "status": "Active",
			 "department": dept, "user_id": user}
		).insert(ignore_permissions=True)
		return user, dept, emp

	user1, dept1, emp1 = employee(companies[0])
	user2, dept2, _ = employee(companies[-1])
	hd.sync()

	# 1. one team named after the department, both companies' employees, agents created
	team = frappe.get_doc("HD Team", dept_name)
	assert team.get(hd.SYNCED) and not team.disabled
	assert sorted(r.user for r in team.users) == sorted({user1, user2}), team.users
	assert frappe.db.exists("HD Agent", user1) and frappe.db.exists("HD Agent", user2)

	# 2. employee leaves -> removed from the team
	if user1 != user2 and len(companies) > 1:
		emp1.status = "Left"
		emp1.relieving_date = "2026-01-01"
		emp1.save(ignore_permissions=True)
		hd.sync()
		assert [r.user for r in frappe.get_doc("HD Team", dept_name).users] == [user2]

	# 3. untick -> team disabled (kept for old tickets), manual teams untouched
	for d in {dept1, dept2}:
		frappe.db.set_value("Department", d, hd.FLAG, 0)
	hd.sync()
	assert frappe.db.get_value("HD Team", dept_name, "disabled") == 1
	for name, disabled in manual.items():
		assert frappe.db.get_value("HD Team", name, "disabled") == disabled, name
