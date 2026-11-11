// Copyright (c) 2025, Shridhar Patil and contributors
// For license information, please see license.txt

frappe.ui.form.on("WhatsApp Account", {
	refresh(frm) {
		if (!frm.is_new() && frm.doc.provider === "Evolution API") {
			frm.add_custom_button(__("Connect WhatsApp (QR)"), () => evolution_connect(frm));
			return;
		}
		if (!frm.is_new()) {
			frm.add_custom_button(__("Subscribe App to Webhooks"), () => {
				frappe.confirm(
					__("Subscribe this app to webhooks for WhatsApp Business Account {0}?", [
						frm.doc.business_id || frm.doc.account_name,
					]),
					() => {
						frm.call({
							doc: frm.doc,
							method: "subscribe_app",
							freeze: true,
							freeze_message: __("Subscribing app to webhooks..."),
							callback: (r) => {
								if (!r.exc) {
									frappe.show_alert({
										message: __("App subscribed to webhooks"),
										indicator: "green",
									});
								}
							},
						});
					}
				);
			});
		}
	},
});

function evolution_connect(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Connect WhatsApp"),
		fields: [{ fieldtype: "HTML", fieldname: "qr" }],
		primary_action_label: __("Refresh"),
		primary_action: () => load(),
	});
	const load = () =>
		frm.call({ doc: frm.doc, method: "evolution_connect", freeze: true }).then((r) => {
			const res = r.message || {};
			const html =
				res.state === "open"
					? `<p class="text-success">${__("WhatsApp is connected.")}</p>`
					: res.qr
					? `<p>${__("WhatsApp on your phone → Linked devices → Link a device, then scan:")}</p>
					   <img src="${res.qr}" style="width:280px;max-width:100%">
					   <p class="text-muted">${__("The QR expires quickly; press Refresh for a new one.")}</p>`
					: `<p>${__("No QR code yet (state: {0}). Press Refresh.", [res.state || "-"])}</p>`;
			dialog.fields_dict.qr.$wrapper.html(html);
			frm.reload_doc();
		});
	dialog.show();
	load();
}
