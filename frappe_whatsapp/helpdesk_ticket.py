"""HD Ticket override: WhatsApp tickets have no customer email, so skip email sends.

Kept apart from helpdesk.py because importing HDTicket fails on sites without helpdesk.
"""
from helpdesk.helpdesk.doctype.hd_ticket.hd_ticket import HDTicket


class WhatsAppHDTicket(HDTicket):
	def skip_email_workflow(self):
		# agent replies are sent by frappe_whatsapp.helpdesk.on_communication instead
		return bool(self.get("whatsapp_number")) or super().skip_email_workflow()

	def send_acknowledgement_email(self):
		if not self.get("whatsapp_number"):
			super().send_acknowledgement_email()

	def handle_email_feedback(self):
		if not self.get("whatsapp_number"):
			super().handle_email_feedback()
