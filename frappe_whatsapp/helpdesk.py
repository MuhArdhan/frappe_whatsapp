"""Bridge WhatsApp chats and Helpdesk tickets.

Incoming chat -> customer picks a team (HD Team) -> HD Ticket for that team.
Later messages from the same number go to the open ticket; agent replies go
back to WhatsApp.
"""
import json
import mimetypes

import frappe
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
	choice = team_choice(msg)

	ticket = open_ticket(number)
	if ticket:
		link_message(msg, ticket)
		return

	teams = active_teams()
	if teams and choice not in teams:
		# a stale/unknown choice gets the menu again right away
		send_team_menu(msg, teams, force=bool(choice))
		return

	ticket = create_ticket(msg, choice)
	for pending in pending_messages(number):
		link_message(pending, ticket)


def team_choice(msg):
	text = msg.message or ""
	if msg.content_type == "button" and text.startswith(TEAM_PREFIX):
		return text[len(TEAM_PREFIX) :]
	return None


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
	recently_sent = frappe.db.exists(
		"WhatsApp Message",
		{
			"type": "Outgoing",
			"to": number,
			"content_type": "interactive",
			"creation": [">", add_to_date(now_datetime(), minutes=-MENU_COOLDOWN_MINUTES)],
		},
	)
	if recently_sent and not force:
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
	first_text = next((m.message for m in pending_messages(number) if m.content_type == "text" and m.message), None)
	ticket = frappe.get_doc(
		{
			"doctype": "HD Ticket",
			"subject": (first_text or f"WhatsApp: {name}")[:140],
			"whatsapp_number": number,
			"agent_group": team,
			"contact": get_or_create_contact(number, name),
		}
	)
	ticket.insert(ignore_permissions=True)
	return ticket.name


def get_or_create_contact(number, name):
	phones = [number, "+" + number]
	contact = frappe.db.get_value("Contact Phone", {"phone": ["in", phones], "parenttype": "Contact"}, "parent")
	if contact:
		return contact
	contact = frappe.get_doc({"doctype": "Contact", "first_name": name})
	contact.add_phone("+" + number, is_primary_mobile_no=1)
	contact.insert(ignore_permissions=True)
	return contact.name


def link_message(msg, ticket):
	msg.db_set({"reference_doctype": "HD Ticket", "reference_name": ticket}, update_modified=False)
	if team_choice(msg):
		return  # the menu tap is routing info, not conversation

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
	text = html2text(comm.content or "").strip()
	if text:
		outgoing.append({"content_type": "text", "message": text})
	for f in frappe.get_all(
		"File",
		filters={"attached_to_doctype": "Communication", "attached_to_name": comm.name},
		fields=["file_url", "file_name"],
	):
		outgoing.append({"content_type": media_type(f.file_name), "attach": f.file_url})

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


def media_type(file_name):
	kind = (mimetypes.guess_type(file_name or "")[0] or "").split("/")[0]
	return kind if kind in ("image", "video", "audio") else "document"
