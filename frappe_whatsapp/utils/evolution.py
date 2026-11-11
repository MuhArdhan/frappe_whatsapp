"""Evolution API provider (unofficial WhatsApp Web gateway).

Selected per WhatsApp Account with provider = "Evolution API". Outgoing messages go
through `send`, incoming ones arrive at `webhook` and become WhatsApp Message docs
exactly like the Meta webhook makes them, so everything built on WhatsApp Message
(helpdesk bridge, notifications) works with either provider.
"""
import base64
import hmac
import json
import mimetypes

import frappe
import requests
from frappe import _

from frappe_whatsapp.utils import format_number
from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_helpdesk_settings import whatsapp_helpdesk_settings as settings

PROVIDER = "Evolution API"
WEBHOOK_PATH = "/api/method/frappe_whatsapp.utils.evolution.webhook"
TOKEN_HEADER = "x-webhook-token"
MEDIA_TYPES = ("image", "video", "audio", "document")
SKIP_JIDS = ("@g.us", "@broadcast", "@newsletter")  # groups, status, channels
WRAPPERS = ("ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2", "documentWithCaptionMessage")


def is_evolution(account_name):
	return bool(account_name) and frappe.db.get_value("WhatsApp Account", account_name, "provider") == PROVIDER


def api(account, method, path, payload=None):
	"""Call the Evolution server; raise with its error text on failure."""
	response = requests.request(
		method,
		f"{account.url.strip().rstrip('/')}{path}",
		headers={"apikey": account.get_password("token")},
		json=payload,
		timeout=60,
	)
	if not response.ok:
		frappe.throw(_("Evolution API error {0}: {1}").format(response.status_code, response.text[:500]))
	return response.json() if response.content else {}


# ---------------------------------------------------------------- outgoing


def send(msg):
	"""Send an Outgoing WhatsApp Message; return the WhatsApp message id."""
	account = frappe.get_doc("WhatsApp Account", msg.whatsapp_account)
	inst = account.evolution_instance
	number = format_number(msg.to)
	kind = msg.content_type

	if msg.message_type == "Template" or msg.template:
		frappe.throw(_("Templates are only supported by the Meta Cloud API provider"))

	# Evolution looks the quoted message up by id in its own store
	quoted = {"quoted": {"key": {"id": msg.reply_to_message_id}}} if msg.is_reply and msg.reply_to_message_id else {}

	if kind == "text":
		res = api(account, "POST", f"/message/sendText/{inst}", {"number": number, "text": msg.message, **quoted})
	elif kind == "interactive":
		# list/button messages are unreliable on WhatsApp Web, so send a numbered menu
		res = api(account, "POST", f"/message/sendText/{inst}", {"number": number, "text": numbered_menu(msg)})
	elif kind in MEDIA_TYPES and msg.attach:
		file_name, mime_type, media = media_source(msg.attach)
		if kind == "audio":
			res = api(account, "POST", f"/message/sendWhatsAppAudio/{inst}", {"number": number, "audio": media, **quoted})
		else:
			res = api(
				account,
				"POST",
				f"/message/sendMedia/{inst}",
				{
					"number": number,
					"mediatype": kind,
					"mimetype": mime_type,
					"caption": msg.message or "",
					"fileName": file_name,
					"media": media,
					**quoted,
				},
			)
	else:
		frappe.throw(_("Content type {0} is not supported by the Evolution API provider").format(kind))

	return (res.get("key") or {}).get("id")


def numbered_menu(msg):
	options = json.loads(msg.buttons) if isinstance(msg.buttons, str) else (msg.buttons or [])
	lines = [f"{i}. {opt['title']}" for i, opt in enumerate(options, 1)]
	return "\n".join([msg.message or "", "", *lines, "", settings.get("menu_footer")]).strip()


def media_source(attach):
	"""(file name, mime type, url-or-base64) for an attachment."""
	if attach.startswith("http"):
		file_name = attach.split("?")[0].rsplit("/", 1)[-1]
		return file_name, mimetypes.guess_type(file_name)[0] or "application/octet-stream", attach
	file_doc = frappe.get_doc("File", {"file_url": attach})
	mime_type = mimetypes.guess_type(file_doc.file_name)[0] or "application/octet-stream"
	content = file_doc.get_content()
	if isinstance(content, str):  # frappe decodes text files
		content = content.encode()
	return file_doc.file_name, mime_type, base64.b64encode(content).decode()


# ---------------------------------------------------------------- connect


def connect(account):
	"""Create the instance if needed, point its webhook here, return QR or state."""
	inst = account.evolution_instance
	if not (account.url and account.get_password("token", raise_exception=False) and inst):
		frappe.throw(_("URL, Token and Evolution Instance are required"))

	if not account.webhook_verify_token:
		account.db_set("webhook_verify_token", frappe.generate_hash(length=32))

	# filtering by instanceName answers 404 when missing, so list all and look
	if not any(i.get("name") == inst for i in api(account, "GET", "/instance/fetchInstances")):
		api(account, "POST", "/instance/create", {"instanceName": inst, "integration": "WHATSAPP-BAILEYS", "qrcode": True})

	base_url = ((account.evolution_webhook_url or "").strip() or frappe.utils.get_url()).rstrip("/")
	api(
		account,
		"POST",
		f"/webhook/set/{inst}",
		{
			"webhook": {
				"enabled": True,
				"url": base_url + WEBHOOK_PATH,
				"events": ["MESSAGES_UPSERT"],
				"byEvents": False,
				"base64": True,
				# site header lets a Docker-internal URL (http://frappe:8000) reach the right site
				"headers": {TOKEN_HEADER: account.webhook_verify_token, "X-Frappe-Site-Name": frappe.local.site},
			}
		},
	)

	state = (api(account, "GET", f"/instance/connectionState/{inst}").get("instance") or {}).get("state")
	if state == "open":
		return {"state": "open"}
	qr = api(account, "GET", f"/instance/connect/{inst}")
	return {"state": state, "qr": qr.get("base64")}


def check_connections():
	"""Scheduler: tell whoever set the account up when its number goes offline, once per outage."""
	for name in frappe.get_all("WhatsApp Account", {"provider": PROVIDER, "status": "Active"}, pluck="name"):
		account = frappe.get_doc("WhatsApp Account", name)
		try:
			state = (api(account, "GET", f"/instance/connectionState/{account.evolution_instance}").get("instance") or {}).get("state")
		except Exception as e:
			state = str(e)[:200] or type(e).__name__
		key = f"wa_evolution_down:{name}"
		was_down = frappe.cache.get_value(key)
		frappe.cache.set_value(key, state != "open")
		if state != "open" and not was_down:
			notify_down(account, state)


def notify_down(account, state):
	subject = f"WhatsApp {account.name} terputus: {state}"
	body = "Pesan pelanggan tidak masuk sampai tersambung lagi. Buka WhatsApp Account lalu klik Connect WhatsApp (QR)."
	for user in alert_recipients(account):
		frappe.get_doc(
			{
				"doctype": "Notification Log",
				"for_user": user,
				"type": "Alert",
				"document_type": "WhatsApp Account",
				"document_name": account.name,
				"subject": subject,
				"email_content": body,
			}
		).insert(ignore_permissions=True)


def alert_recipients(account):
	# System Manager is often handed out widely (cashiers etc.); the account's admins are the ones who can rescan
	return {u for u in (account.owner, account.modified_by) if u and u != "Guest"}


# ---------------------------------------------------------------- incoming


@frappe.whitelist(allow_guest=True, methods=["POST"])
def webhook():
	payload = frappe.request.get_json(silent=True) or {}
	account = authenticate(payload.get("instance"))

	event = (payload.get("event") or "").lower().replace("_", ".")
	if event != "messages.upsert":
		return

	data = payload.get("data") or {}
	messages = data if isinstance(data, list) else [data]
	log_payload(payload)
	for message in messages:
		try:
			save_incoming(account, message)
		except Exception:
			# answer 2xx anyway: a non-4xx error makes Evolution retry the same message
			frappe.log_error("Evolution webhook: message not saved", json.dumps(strip_base64(message))[:5000])


def authenticate(instance):
	name = frappe.db.get_value(
		"WhatsApp Account", {"provider": PROVIDER, "evolution_instance": instance or "", "status": "Active"}, "name"
	)
	expected = name and frappe.db.get_value("WhatsApp Account", name, "webhook_verify_token")
	given = frappe.get_request_header(TOKEN_HEADER) or ""
	if not expected or not hmac.compare_digest(given, expected):
		frappe.throw(_("Invalid webhook token"), frappe.PermissionError)
	return frappe.get_doc("WhatsApp Account", name)


def save_incoming(account, data):
	key = data.get("key") or {}
	if key.get("fromMe") or not key.get("id"):
		return
	number = sender_number(key)
	if not number or frappe.db.exists("WhatsApp Message", {"message_id": key["id"]}):
		return

	message = data.get("message") or {}
	b64 = message.get("base64")
	message = unwrap(message)

	doc = {
		"doctype": "WhatsApp Message",
		"type": "Incoming",
		"from": number,
		"message_id": key["id"],
		"profile_name": data.get("pushName"),
		"whatsapp_account": account.name,
	}

	if "reactionMessage" in message:
		reaction = message["reactionMessage"]
		doc.update(content_type="reaction", message=reaction.get("text"), reply_to_message_id=(reaction.get("key") or {}).get("id"))
		frappe.get_doc(doc).insert(ignore_permissions=True)
		return

	ctx = data.get("contextInfo") or next(
		(v["contextInfo"] for v in message.values() if isinstance(v, dict) and v.get("contextInfo")), {}
	)
	quoted_text = None
	if ctx.get("stanzaId"):  # customer replied to a specific message
		doc.update(is_reply=1, reply_to_message_id=ctx["stanzaId"])
		quoted_text = text_of(unwrap(ctx.get("quotedMessage") or {}))

	media_kind = next((k for k in MEDIA_TYPES if f"{k}Message" in message), None)
	if not media_kind:
		doc.update(content_type="text", message=text_of(message) or f"[{data.get('messageType') or 'unsupported message'}]")
		new_message(doc, quoted_text).insert(ignore_permissions=True)
		return

	media = message[f"{media_kind}Message"]
	if not b64:
		b64 = api(account, "POST", f"/chat/getBase64FromMediaMessage/{account.evolution_instance}", {"message": data}).get("base64")
	mime_type = (media.get("mimetype") or "application/octet-stream").split(";")[0]
	file_name = media.get("fileName") or f"{frappe.generate_hash(length=10)}{mimetypes.guess_extension(mime_type) or ''}"

	# two saves like the Meta webhook: the helpdesk bridge routes media once `attach` is set
	msg = new_message({**doc, "content_type": media_kind, "message": media.get("caption") or ""}, quoted_text).insert(
		ignore_permissions=True
	)
	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": file_name,
			"attached_to_doctype": "WhatsApp Message",
			"attached_to_name": msg.name,
			"attached_to_field": "attach",
			"is_private": 1,
			"content": base64.b64decode(b64),
		}
	).save(ignore_permissions=True)
	msg.attach = file_doc.file_url
	msg.save(ignore_permissions=True)


def new_message(doc, quoted_text):
	msg = frappe.get_doc(doc)
	# fallback for the helpdesk quote when the quoted message never passed through Frappe (sent from the phone)
	msg.flags.quoted_text = quoted_text
	return msg


def text_of(message):
	return (
		message.get("conversation")
		or (message.get("extendedTextMessage") or {}).get("text")
		or next((message[f"{k}Message"].get("caption") for k in MEDIA_TYPES if f"{k}Message" in message), None)
	)


def sender_number(key):
	jid = key.get("remoteJid") or ""
	if jid.endswith("@lid"):  # privacy id, not a phone number
		jid = key.get("remoteJidAlt") or key.get("senderPn") or ""
	if not jid or jid.endswith(SKIP_JIDS):
		return None
	return jid.split("@")[0].split(":")[0]


def unwrap(message):
	for wrapper in WRAPPERS:
		if wrapper in message:
			return unwrap((message[wrapper] or {}).get("message") or {})
	return message


def strip_base64(obj):
	if isinstance(obj, dict):
		return {k: ("<base64>" if k == "base64" else strip_base64(v)) for k, v in obj.items()}
	if isinstance(obj, list):
		return [strip_base64(v) for v in obj]
	return obj


def log_payload(payload):
	frappe.get_doc(
		{"doctype": "WhatsApp Notification Log", "template": "Webhook", "meta_data": json.dumps(strip_base64(payload))}
	).insert(ignore_permissions=True)
