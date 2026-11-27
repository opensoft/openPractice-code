"""Provision the synthetic acceptance account on a disposable Frappe site."""

import os

import frappe

from open_practice.frappe_session_desk import ROLE, enabled


def create_user():
    if not enabled() or frappe.session.user != "Administrator":
        frappe.throw("Create the rehearsal user on a configured PracticeLab site.", frappe.PermissionError)
    email = os.environ["PRACTICELAB_SMOKE_USER"]
    password = os.environ["PRACTICELAB_SMOKE_PASSWORD"]
    if not email.endswith("@example.invalid"):
        raise ValueError("The rehearsal user requires an example.invalid address.")
    frappe.get_doc({
        "doctype": "User", "email": email, "first_name": "Synthetic", "last_name": "Scheduler",
        "user_type": "Website User", "send_welcome_email": 0, "new_password": password,
        "roles": [{"role": ROLE}],
    }).insert()
    frappe.db.commit()
