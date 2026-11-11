"""Bridge WhatsApp chats and Helpdesk tickets.

Incoming chat -> customer picks a team (HD Team) -> HD Ticket for that team.
Later messages from the same number go to the open ticket; agent replies go
back to WhatsApp.
"""
import json
import mimetypes
from urllib.parse import unquote

import frappe
from bs4 import BeautifulSoup
from frappe.core.utils import html2text
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import add_to_date, escape_html, now_datetime

TEAM_PREFIX = "hd_team:"
MEDIA_TYPES = ("image", "audio", "video", "document")
OPEN_CATEGORIES = ("Open", "Paused")
MENU_TEXT = "Halo! Silakan pilih divisi yang ingin Anda hubungi:"
MENU_COOLDOWN_MINUTES = 30  # don't resend the menu for every message while the customer decides
PENDING_WINDOW_HOURS = 24  # unlinked messages older than this are not pulled into a new ticket


def helpdesk_installed():
	return "helpdesk" in frappe.get_installed_apps()


def setup_custom_fields():
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


def route_message(msg):
	number = msg.get("from")

	ticket = open_ticket(number)
	if ticket:
		link_message(msg, ticket)
		return

	teams = active_teams()
	choice = team_choice(msg, teams)
	if teams and not choice:
		# a stale/unknown choice gets the menu again right away
		send_team_menu(msg, teams, force=looks_like_choice(msg))
		return

	ticket = create_ticket(msg, choice)
	for pending in pending_messages(number):
		# the choice itself is routing info, not conversation
		link_message(pending, ticket, log=pending.name != msg.name)


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
	if not menu_sent_within(number, PENDING_WINDOW_HOURS * 60):
		return []
	buttons = frappe.db.get_value(
		"WhatsApp Message",
		{"type": "Outgoing", "to": number, "content_type": "interactive"},
		"buttons",
		order_by="creation desc",
	)
	return json.loads(buttons or "[]")


def looks_like_choice(msg):
	text = (msg.message or "").strip()
	return (msg.content_type == "button" and text.startswith(TEAM_PREFIX)) or (
		msg.content_type == "text" and text.isdigit() and menu_sent_within(msg.get("from"), PENDING_WINDOW_HOURS * 60)
	)


def menu_sent_within(number, minutes):
	return frappe.db.exists(
		"WhatsApp Message",
		{
			"type": "Outgoing",
			"to": number,
			"content_type": "interactive",
			"creation": [">", add_to_date(now_datetime(), minutes=-minutes)],
		},
	)


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
	if not force and menu_sent_within(number, MENU_COOLDOWN_MINUTES):
		return
	frappe.get_doc(
		{
			"doctype": "WhatsApp Message",
			"type": "Outgoing",
			"to": number,
			"content_type": "interactive",
			"message": MENU_TEXT,
			# WhatsApp caps list rows at 10 and button titles at 20 chars
			"buttons": json.dumps([{"id": TEAM_PREFIX + t, "title": t[:20]} for t in teams[:10]]),
			"whatsapp_account": msg.whatsapp_account,
		}
	).insert(ignore_permissions=True)


def create_ticket(msg, team):
	number = msg.get("from")
	name = msg.profile_name or number
	first_text = next(
		(m.message for m in pending_messages(number) if m.content_type == "text" and m.message and m.name != msg.name),
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
	phones = [number, "+" + number]
	contact = frappe.db.get_value("Contact Phone", {"phone": ["in", phones], "parenttype": "Contact"}, "parent")
	if contact:
		return contact
	contact = frappe.get_doc({"doctype": "Contact", "first_name": name})
	contact.add_phone("+" + number, is_primary_mobile_no=1)
	contact.insert(ignore_permissions=True)
	return contact.name


def link_message(msg, ticket, log=True):
	msg.db_set({"reference_doctype": "HD Ticket", "reference_name": ticket}, update_modified=False)
	if not log or (msg.content_type == "button" and (msg.message or "").startswith(TEAM_PREFIX)):
		return  # menu taps are routing info, not conversation

	content = escape_html(msg.message or "").replace("\n", "<br>")
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


def send_reply(communication, number):
	comm = frappe.get_doc("Communication", communication)
	account = frappe.db.get_value(
		"WhatsApp Message",
		{"reference_doctype": "HD Ticket", "reference_name": comm.reference_name, "type": "Incoming"},
		"whatsapp_account",
		order_by="creation desc",
	)

	outgoing = []
	text, inline_urls = split_inline_images(comm.content or "")
	if text:
		outgoing.append({"content_type": "text", "message": text})
	files = frappe.get_all(
		"File",
		filters={"attached_to_doctype": "Communication", "attached_to_name": comm.name},
		pluck="file_url",
	)
	for url in dict.fromkeys(inline_urls + files):  # an inline image may also be attached
		outgoing.append({"content_type": media_type(url), "attach": url})

	for payload in outgoing:
		try:
			frappe.get_doc(
				{
					"doctype": "WhatsApp Message",
					"type": "Outgoing",
					"to": number,
					"whatsapp_account": account,
					"reference_doctype": "HD Ticket",
					"reference_name": comm.reference_name,
					**payload,
				}
			).insert(ignore_permissions=True)
			frappe.db.commit()
		except Exception as e:
			frappe.db.rollback()
			frappe.log_error("Helpdesk -> WhatsApp send failed", reference_doctype="HD Ticket", reference_name=comm.reference_name)
			frappe.get_doc("HD Ticket", comm.reference_name).add_comment(
				"Comment", f"Gagal mengirim balasan ke WhatsApp: {escape_html(str(e))}"
			)
			frappe.db.commit()


def split_inline_images(html):
	"""(plain text, site file urls) — images pasted into the editor are sent as media, not markdown."""
	soup = BeautifulSoup(html, "html.parser")
	urls = []
	for img in soup.find_all("img"):
		src = unquote(img.get("src") or "")
		if src.startswith(("/files/", "/private/files/")) and frappe.db.exists("File", {"file_url": src}):
			urls.append(src)
		img.decompose()
	return html2text(str(soup)).strip(), urls


def media_type(file_name):
	kind = (mimetypes.guess_type(file_name or "")[0] or "").split("/")[0]
	return kind if kind in ("image", "video", "audio") else "document"
