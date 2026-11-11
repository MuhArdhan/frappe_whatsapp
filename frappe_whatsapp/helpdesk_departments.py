"""Keep HD Teams in sync with ERPNext Departments.

A Department ticked "Show in Helpdesk" gets an HD Team named after its
department_name (same-named departments of different companies share one team).
Members are the active Employees of those departments that have a user login;
each becomes an HD Agent. People are managed in HR, helpdesk follows.

Teams created by hand in helpdesk are left alone; a synced team that loses all
members or its tick is disabled, never deleted (tickets still point at it).
"""
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

FLAG = "show_in_helpdesk"
SYNCED = "synced_from_department"


def enabled():
	installed = frappe.get_installed_apps()
	return "helpdesk" in installed and "erpnext" in installed


def setup_custom_fields(app_name=None):
	if not enabled():
		return
	create_custom_fields(
		{
			"Department": [
				{
					"fieldname": FLAG,
					"label": "Show in Helpdesk / WhatsApp",
					"fieldtype": "Check",
					"insert_after": "disabled",
					"description": "Creates a helpdesk team from this department; its employees with a user login become agents.",
				}
			],
			"HD Team": [
				{
					"fieldname": SYNCED,
					"label": "Synced from Department",
					"fieldtype": "Check",
					"insert_after": "disabled",
					"read_only": 1,
				}
			],
		},
		ignore_validate=True,
	)


def queue_sync(doc=None, method=None):
	"""doc_events on Employee / Department: one deduplicated full resync after commit."""
	if enabled() and not frappe.flags.in_install:
		frappe.enqueue(
			"frappe_whatsapp.helpdesk_departments.sync",
			job_id="wa_department_team_sync",
			deduplicate=True,
			enqueue_after_commit=True,
		)


def sync():
	if not enabled():
		return
	wanted = wanted_teams()

	for team_name, users in wanted.items():
		for user in users:
			ensure_agent(user)
		upsert_team(team_name, users)

	for team_name in frappe.get_all("HD Team", filters={SYNCED: 1, "disabled": 0}, pluck="name"):
		if not wanted.get(team_name):
			team = frappe.get_doc("HD Team", team_name)
			team.disabled = 1
			team.save(ignore_permissions=True)  # on_update disables its assignment rule too


def wanted_teams():
	"""{team name: sorted users} for every ticked, enabled department."""
	departments = frappe.get_all(
		"Department", filters={FLAG: 1, "disabled": 0}, fields=["name", "department_name"]
	)
	team_of = {d.name: d.department_name for d in departments}
	teams = {name: set() for name in team_of.values()}
	if team_of:
		for emp in frappe.get_all(
			"Employee",
			filters={"status": "Active", "department": ["in", list(team_of)], "user_id": ["is", "set"]},
			fields=["department", "user_id"],
		):
			teams[team_of[emp.department]].add(emp.user_id)
	return {name: sorted(users) for name, users in teams.items()}


def ensure_agent(user):
	if frappe.db.exists("HD Agent", user) or not frappe.db.get_value("User", user, "enabled"):
		return
	frappe.get_doc(
		{
			"doctype": "HD Agent",
			"user": user,
			"agent_name": frappe.db.get_value("User", user, "full_name") or user,
			"is_active": 1,
		}
	).insert(ignore_permissions=True)


def upsert_team(team_name, users):
	users = [u for u in users if frappe.db.get_value("User", u, "enabled")]
	if not frappe.db.exists("HD Team", team_name):
		if users:  # HD Team requires at least one member
			frappe.get_doc(
				{"doctype": "HD Team", "team_name": team_name, SYNCED: 1, "users": [{"user": u} for u in users]}
			).insert(ignore_permissions=True)
		return

	team = frappe.get_doc("HD Team", team_name)
	current = sorted(row.user for row in team.users)
	disabled = 0 if users else 1
	if team.get(SYNCED) and current == users and team.disabled == disabled:
		return
	team.set(SYNCED, 1)  # a ticked department takes over a hand-made team of the same name
	team.disabled = disabled
	if users:
		team.set("users", [{"user": u} for u in users])
	team.save(ignore_permissions=True)
