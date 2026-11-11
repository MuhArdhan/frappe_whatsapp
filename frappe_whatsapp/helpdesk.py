"""Bridge WhatsApp chats and Helpdesk tickets.

Incoming chat -> customer picks a team (HD Team) -> HD Ticket for that team.
Later messages from the same number go to the open ticket; agent replies go
back to WhatsApp.
"""
import json
import mimetypes
from urllib.parse import unquote

import frappe
from bs4 import BeautifulSoup, Tag
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import add_to_date, escape_html, get_datetime, now_datetime
from markdownify import MarkdownConverter, abstract_inline_conversion

from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_helpdesk_settings import whatsapp_helpdesk_settings as settings

TEAM_PREFIX = "hd_team:"
MEDIA_TYPES = ("image", "audio", "video", "document")
OPEN_CATEGORIES = ("Open", "Paused")
HARI = ("Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu")
BULAN = ("Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober", "November", "Desember")
# WhatsApp Message status -> helpdesk timeline badge; Communication has no "Delivered" option
DELIVERY_BADGE = {"sent": "Sent", "delivered": "Sent", "read": "Read", "played": "Read", "failed": "Error"}
PENDING_WINDOW_HOURS = 24  # unlinked messages older than this are not pulled into a new ticket


def helpdesk_installed():
	return "helpdesk" in frappe.get_installed_apps()


def setup_custom_fields(app_name=None):
	"""after_migrate: field that marks a ticket as a WhatsApp conversation."""
	if not helpdesk_installed():
		return
	create_custom_fields(
		{
			"HD Ticket": [
				{
					"fieldname": "whatsapp_number",
					"label": "WhatsApp Number",
					"fieldtype": "Data",
					"insert_after": "raised_by",
					"read_only": 1,
					"search_index": 1,
				}
			]
		},
		ignore_validate=True,
	)


# ---------------------------------------------------------------- incoming


def on_whatsapp_message(doc, method=None):
	"""WhatsApp Message on_update."""
	if doc.type == "Outgoing":
		update_delivery_status(doc)
		return
	if doc.type != "Incoming" or doc.reference_name or doc.content_type == "reaction":
		return
	if doc.content_type in MEDIA_TYPES and not doc.attach:
		return  # the webhook saves the downloaded file in a second save; route it then
	if not helpdesk_installed():
		return

	# Guest (webhook) can't create tickets for a contact; email intake runs as Administrator too.
	user = frappe.session.user
	frappe.set_user("Administrator")
	frappe.db.savepoint("wa_helpdesk")
	try:
		route_message(doc)
	except Exception:
		# keep the WhatsApp Message saved and answer Meta with 200, or it retries forever
		frappe.db.rollback(save_point="wa_helpdesk")
		frappe.log_error("WhatsApp -> Helpdesk routing failed", reference_doctype=doc.doctype, reference_name=doc.name)
	finally:
		frappe.set_user(user)


def update_delivery_status(msg):
	"""Show sent/read of an agent reply as the delivery badge of its Communication (Meta and Evolution)."""
	status = DELIVERY_BADGE.get((msg.status or "").lower())
	if not status or not msg.message_id or not helpdesk_installed():
		return
	try:
		row = frappe.db.get_value(
			"Communication",
			{"message_id": msg.message_id, "sent_or_received": "Sent", "reference_doctype": "HD Ticket"},
			["name", "delivery_status", "reference_name"],
			as_dict=True,
		)
		# statuses can arrive out of order; never go back from Read
		if not row or row.delivery_status in (status, "Read"):
			return
		frappe.db.set_value("Communication", row.name, "delivery_status", status, update_modified=False)
		frappe.get_doc("HD Ticket", row.reference_name).publish_update()
	except Exception:
		frappe.log_error("WhatsApp delivery status not shown on ticket", reference_doctype=msg.doctype, reference_name=msg.name)


def route_message(msg):
	number = msg.get("from")

	ticket = open_ticket(number)
	wants_menu = is_menu_keyword(msg)
	if ticket and wants_menu:
		link_message(msg, ticket)
		close_for_switch(ticket)
		ticket = None
	if ticket:
		link_message(msg, ticket)
		maybe_send_off_hours_note(msg, ticket)
		return

	teams = active_teams()
	choice = team_choice(msg, teams)
	if teams and not choice:
		# a stale/unknown choice or the menu keyword gets the menu again right away
		send_team_menu(msg, teams, force=wants_menu or looks_like_choice(msg))
		return

	ticket = create_ticket(msg, choice)
	for pending in pending_messages(number):
		# the choice and the menu keyword are routing info, not conversation
		link_message(pending, ticket, log=pending.name != msg.name and not is_menu_keyword(pending))
	send_confirmation(msg, ticket, choice)


def is_menu_keyword(msg):
	return msg.content_type == "text" and (msg.message or "").strip().lower() in settings.menu_keywords()


def close_for_switch(ticket):
	"""Customer wants another team: close this ticket, the next choice opens a new one."""
	doc = frappe.get_doc("HD Ticket", ticket)
	doc.status = "Closed"
	doc.flags.wa_menu_switch = True  # the customer gets the menu, not a goodbye
	doc.save(ignore_permissions=True)
	doc.add_comment("Info", "Pelanggan mengetik menu untuk pindah divisi, percakapan berlanjut di ticket baru.")


def send_confirmation(msg, ticket, team):
	text = settings.get("confirm_text").format(team=team) if team else settings.get("confirm_text_no_team")
	note = off_hours_note(frappe.db.get_value("HD Ticket", ticket, "sla"))
	send_courtesy(msg, ticket, f"{text}\n\n{note}" if note else text)


def maybe_send_off_hours_note(msg, ticket):
	"""Message on an open ticket outside working hours: say when we answer, at most once per off_hours_note_hours."""
	note = off_hours_note(frappe.db.get_value("HD Ticket", ticket, "sla"))
	if not note:
		return
	# shortcut: one note per N hours, not per closed period; enough for overnight/weekend
	since = add_to_date(now_datetime(), hours=-int(settings.get("off_hours_note_hours")))
	already = frappe.db.exists(
		"WhatsApp Message",
		# same opening time = same closed period; the note may also sit inside a confirmation
		{"type": "Outgoing", "to": msg.get("from"), "message": ["like", f"%{note}%"], "creation": [">", since]},
	)
	if not already:
		send_courtesy(msg, ticket, note)


def off_hours_note(sla_name=None, now=None):
	"""Off-hours text with the next opening time, or None in working hours / without an SLA calendar."""
	sla_name = sla_name or frappe.db.get_value("HD Service Level Agreement", {"default_sla": 1, "enabled": 1}, "name")
	if not sla_name:
		return None
	sla = frappe.get_cached_doc("HD Service Level Agreement", sla_name)
	hours = sla.get_working_hours()
	if not hours:
		return None
	now = now or now_datetime()
	holidays = set(sla.get_holidays())
	if now.date() not in holidays and sla.is_working_time(now, hours):
		return None

	today = hours.get(now.strftime("%A"))
	if today and now.date() not in holidays and now - get_datetime(now.date()) < today[0]:
		opens = get_datetime(now.date()) + today[0]  # before opening time today
	else:
		opens = sla.get_next_working_day_start(now)
	if not opens:
		return None
	when = f"{HARI[opens.weekday()]}, {opens.day} {BULAN[opens.month - 1]} pukul {opens:%H.%M}"
	return settings.get("off_hours_text").format(opens=when)


def send_courtesy(msg, ticket, text):
	"""A courtesy message must never roll back the ticket or the message routing."""
	frappe.db.savepoint("wa_confirm")
	try:
		frappe.get_doc(
			{
				"doctype": "WhatsApp Message",
				"type": "Outgoing",
				"to": msg.get("from"),
				"content_type": "text",
				"message": text,
				"whatsapp_account": msg.whatsapp_account,
				"reference_doctype": "HD Ticket",
				"reference_name": ticket,
			}
		).insert(ignore_permissions=True)
	except Exception:
		frappe.db.rollback(save_point="wa_confirm")
		frappe.log_error("WhatsApp courtesy message not sent", reference_doctype="HD Ticket", reference_name=ticket)


def team_choice(msg, teams):
	"""Team picked by a menu button, or by its number in a text reply (text menu)."""
	text = (msg.message or "").strip()
	if msg.content_type == "button" and text.startswith(TEAM_PREFIX):
		team = text[len(TEAM_PREFIX) :]
		return team if team in teams else None
	if msg.content_type == "text" and text.isdigit():
		# number refers to the menu the customer saw, not today's team list
		options = last_menu(msg.get("from"))
		index = int(text) - 1
		if 0 <= index < len(options):
			team = options[index]["id"][len(TEAM_PREFIX) :]
			return team if team in teams else None
	return None


def last_menu(number):
	buttons = frappe.db.get_value("WhatsApp Message", unused_menu_filters(number, PENDING_WINDOW_HOURS * 60), "buttons", order_by="creation desc")
	return json.loads(buttons or "[]")


def looks_like_choice(msg):
	text = (msg.message or "").strip()
	return (msg.content_type == "button" and text.startswith(TEAM_PREFIX)) or (
		msg.content_type == "text" and text.isdigit() and menu_sent_within(msg.get("from"), PENDING_WINDOW_HOURS * 60)
	)


def menu_sent_within(number, minutes):
	return frappe.db.exists("WhatsApp Message", unused_menu_filters(number, minutes))


def unused_menu_filters(number, minutes):
	"""Menus sent in the last `minutes` and after the latest ticket: older ones were already answered."""
	since = add_to_date(now_datetime(), minutes=-minutes)
	last_ticket = frappe.db.get_value("HD Ticket", {"whatsapp_number": number}, "creation", order_by="creation desc")
	if last_ticket and last_ticket > since:
		since = last_ticket
	return {"type": "Outgoing", "to": number, "content_type": "interactive", "creation": [">", since]}


def open_ticket(number):
	return frappe.db.get_value(
		"HD Ticket",
		{"whatsapp_number": number, "status_category": ["in", OPEN_CATEGORIES]},
		"name",
		order_by="creation desc",
	)


def active_teams():
	return frappe.get_all("HD Team", filters={"disabled": 0}, pluck="name", order_by="name asc")


def pending_messages(number):
	names = frappe.get_all(
		"WhatsApp Message",
		filters={
			"type": "Incoming",
			"from": number,
			"reference_name": ["is", "not set"],
			"creation": [">", add_to_date(now_datetime(), hours=-PENDING_WINDOW_HOURS)],
		},
		pluck="name",
		order_by="creation asc",
	)
	return [frappe.get_doc("WhatsApp Message", n) for n in names]


def send_team_menu(msg, teams, force=False):
	number = msg.get("from")
	if not force and menu_sent_within(number, int(settings.get("menu_cooldown_minutes"))):
		return
	frappe.get_doc(
		{
			"doctype": "WhatsApp Message",
			"type": "Outgoing",
			"to": number,
			"content_type": "interactive",
			"message": settings.get("menu_text"),
			# WhatsApp caps list rows at 10 and button titles at 20 chars
			"buttons": json.dumps([{"id": TEAM_PREFIX + t, "title": t[:20]} for t in teams[:10]]),
			"whatsapp_account": msg.whatsapp_account,
		}
	).insert(ignore_permissions=True)


def create_ticket(msg, team):
	number = msg.get("from")
	name = msg.profile_name or number
	first_text = next(
		(
			m.message
			for m in pending_messages(number)
			if m.content_type == "text" and m.message and m.name != msg.name and not is_menu_keyword(m)
		),
		None,
	)
	ticket = frappe.get_doc(
		{
			"doctype": "HD Ticket",
			"subject": (first_text or f"WhatsApp: {name}")[:140],
			"whatsapp_number": number,
			# the agent reply box refuses to send without a recipient; .invalid never receives mail
			"raised_by": placeholder_email(number),
			"agent_group": team,
			"contact": get_or_create_contact(number, name),
		}
	)
	ticket.insert(ignore_permissions=True)
	return ticket.name


def placeholder_email(number):
	return f"{number}@whatsapp.invalid"


def get_or_create_contact(number, name):
	contact = find_contact(number)
	if contact:
		return contact
	contact = frappe.get_doc({"doctype": "Contact", "first_name": name})
	contact.add_phone("+" + number, is_primary_mobile_no=1)
	contact.insert(ignore_permissions=True)
	return contact.name


def quoted_text(msg):
	"""Text of the message the customer replied to, if any."""
	if not (msg.get("is_reply") and msg.get("reply_to_message_id")):
		return None
	row = frappe.db.get_value(
		"WhatsApp Message", {"message_id": msg.reply_to_message_id}, ["message", "content_type"], as_dict=True
	)
	text = (row and (row.message or f"[{row.content_type}]")) or msg.flags.get("quoted_text") or "[pesan sebelumnya]"
	return text if len(text) <= 300 else text[:300] + "…"


def find_contact(number):
	"""Existing contact whose phone matches however it was typed: 62812.., +62 812-.., 0812.."""
	# shortcut: only Indonesia's 0 trunk prefix is mapped; add other country codes if customers come from abroad
	candidates = [number] + (["0" + number[2:]] if number.startswith("62") else [])
	row = frappe.db.sql(
		"""select parent from `tabContact Phone`
		where parenttype = 'Contact' and regexp_replace(phone, '[^0-9]', '') in %(candidates)s
		order by modified desc limit 1""",
		{"candidates": candidates},
	)
	return row[0][0] if row else None


def link_message(msg, ticket, log=True):
	msg.db_set({"reference_doctype": "HD Ticket", "reference_name": ticket}, update_modified=False)
	if not log or (msg.content_type == "button" and (msg.message or "").startswith(TEAM_PREFIX)):
		return  # menu taps are routing info, not conversation

	content = escape_html(msg.message or "").replace("\n", "<br>")
	quote = quoted_text(msg)
	if quote:
		content = f"<blockquote>↪ <i>Membalas:</i> {escape_html(quote)}</blockquote>" + content
	if msg.attach:
		if msg.content_type == "image":
			content += f'<p><img src="{msg.attach}"></p>'
		else:
			content += f'<p><a href="{msg.attach}">{escape_html(msg.attach.rsplit("/", 1)[-1])}</a></p>'

	comm = frappe.get_doc(
		{
			"doctype": "Communication",
			"communication_type": "Communication",
			"communication_medium": "Chat",
			"sent_or_received": "Received",
			"status": "Linked",
			"subject": f"WhatsApp {msg.get('from')}",
			"content": content or "-",
			"sender_full_name": msg.profile_name or msg.get("from"),
			"phone_no": msg.get("from"),
			"message_id": msg.message_id,  # lets an agent's Reply on this row quote it on WhatsApp
			"reference_doctype": "HD Ticket",
			"reference_name": ticket,
		}
	)
	comm.insert(ignore_permissions=True)

	if msg.attach:
		ticket_doc = frappe.get_doc("HD Ticket", ticket)
		ticket_doc.attach_file_with_doc("Communication", comm.name, msg.attach)
		ticket_doc.attach_file_with_doc("HD Ticket", ticket, msg.attach)


# ---------------------------------------------------------------- outgoing


def on_communication(doc, method=None):
	"""Communication after_insert: send agent replies on WhatsApp tickets."""
	if (
		doc.reference_doctype != "HD Ticket"
		or doc.sent_or_received != "Sent"
		or doc.communication_type != "Communication"
	):
		return
	number = frappe.db.get_value("HD Ticket", doc.reference_name, "whatsapp_number")
	if number:
		# after commit: reply_via_agent attaches files after inserting the Communication
		frappe.enqueue(send_reply, communication=doc.name, number=number, enqueue_after_commit=True)


def on_ticket_update(doc, method=None):
	"""HD Ticket on_update: tell the customer when their WhatsApp ticket is resolved or closed."""
	if (
		doc.get("whatsapp_number")
		and doc.has_value_changed("status_category")
		and doc.status_category == "Resolved"
		and not doc.flags.wa_menu_switch
	):
		frappe.enqueue(send_closing_message, ticket=doc.name, enqueue_after_commit=True)


def send_closing_message(ticket):
	number, team = frappe.db.get_value("HD Ticket", ticket, ["whatsapp_number", "agent_group"])
	text = settings.get("closed_text").format(team=team) if team else settings.get("closed_text_no_team")
	send_to_customer(ticket, number, ticket_account(ticket), {"content_type": "text", "message": text}, "pesan penutup")


def ticket_account(ticket):
	return frappe.db.get_value(
		"WhatsApp Message",
		{"reference_doctype": "HD Ticket", "reference_name": ticket, "type": "Incoming"},
		"whatsapp_account",
		order_by="creation desc",
	)


def send_to_customer(ticket, number, account, payload, what="balasan"):
	"""Send one message in its own transaction; a failure becomes a comment on the ticket. Returns the message or None."""
	try:
		msg = frappe.get_doc(
			{
				"doctype": "WhatsApp Message",
				"type": "Outgoing",
				"to": number,
				"whatsapp_account": account,
				"reference_doctype": "HD Ticket",
				"reference_name": ticket,
				**payload,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		return msg
	except Exception as e:
		frappe.db.rollback()
		frappe.log_error("Helpdesk -> WhatsApp send failed", reference_doctype="HD Ticket", reference_name=ticket)
		frappe.get_doc("HD Ticket", ticket).add_comment(
			"Comment", f"Gagal mengirim {what} ke WhatsApp: {escape_html(str(e))}"
		)
		frappe.db.commit()


def send_reply(communication, number):
	comm = frappe.get_doc("Communication", communication)
	account = ticket_account(comm.reference_name)

	outgoing = []
	text, inline_urls, quote = split_inline_images(comm.content or "")
	if text:
		outgoing.append({"content_type": "text", "message": text})
	files = frappe.get_all(
		"File",
		filters={"attached_to_doctype": "Communication", "attached_to_name": comm.name},
		pluck="file_url",
	)
	for url in dict.fromkeys(inline_urls + files):  # an inline image may also be attached
		outgoing.append({"content_type": media_type(url), "attach": url})

	quoted_id = quote and quoted_message_id(comm.reference_name, quote)
	if quoted_id and outgoing:
		# the first message carries the WhatsApp reply, like a person replying then sending files
		outgoing[0].update(is_reply=1, reply_to_message_id=quoted_id)

	sent = [m for m in (send_to_customer(comm.reference_name, number, account, p) for p in outgoing) if m]
	if outgoing:
		# the badge follows the first message; later status updates find the reply by this id
		first = sent[0] if sent else None
		comm.db_set(
			{"message_id": first.message_id if first else None, "delivery_status": "Sent" if first else "Error"},
			update_modified=False,
		)
		frappe.db.commit()
		frappe.get_doc("HD Ticket", comm.reference_name).publish_update()


def split_inline_images(html):
	"""(plain text, site file urls) — images pasted into the editor are sent as media, not markdown."""
	soup = BeautifulSoup(html, "html.parser")
	# the agent's Reply button appends the quoted message: send it as a WhatsApp reply, not as text
	quote = None
	for marker in soup.select("p.reply-to-content"):
		block = marker.find_next_sibling("blockquote")
		if block:
			quote = quote or plain(block)
			block.decompose()
		marker.decompose()
	urls = []
	for img in soup.find_all("img"):
		src = unquote(img.get("src") or "")
		if src.startswith(("/files/", "/private/files/")) and frappe.db.exists("File", {"file_url": src}):
			urls.append(src)
		img.decompose()
	return WhatsAppConverter().convert(str(soup)).strip(), urls, quote


def plain(html):
	"""Whitespace-normalised text, so the editor's re-rendered quote matches the stored HTML."""
	soup = html if isinstance(html, Tag) else BeautifulSoup(html or "", "html.parser")
	return " ".join(soup.get_text(" ").split())


def quoted_message_id(ticket, quote):
	"""WhatsApp id of the customer message the agent quoted; the newest one if the text repeats."""
	for content, message_id in frappe.get_all(
		"Communication",
		{"reference_doctype": "HD Ticket", "reference_name": ticket, "sent_or_received": "Received", "message_id": ["is", "set"]},
		["content", "message_id"],
		order_by="creation desc",
		as_list=True,
	):
		if plain(content) == quote:
			return message_id
	return None


class WhatsAppConverter(MarkdownConverter):
	"""HTML -> WhatsApp formatting (*bold*, _italic_, ~strike~), not markdown (**bold**)."""

	def __init__(self):
		# WhatsApp shows backslash escapes literally
		super().__init__(bullets="•", escape_asterisks=False, escape_underscores=False, escape_misc=False)

	convert_b = convert_strong = abstract_inline_conversion(lambda self: "*")
	convert_em = convert_i = abstract_inline_conversion(lambda self: "_")
	convert_del = convert_s = abstract_inline_conversion(lambda self: "~")

	def convert_a(self, el, text, parent_tags):
		href = el.get("href") or ""
		return text if not href or href == text or "_inline" in parent_tags else f"{text} ({href})"

	def convert_hN(self, n, el, text, parent_tags):
		text = text.strip()
		return f"\n\n*{text}*\n\n" if text and "_inline" not in parent_tags else text


def media_type(file_name):
	kind = (mimetypes.guess_type(file_name or "")[0] or "").split("/")[0]
	return kind if kind in ("image", "video", "audio") else "document"
