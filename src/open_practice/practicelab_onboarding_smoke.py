"""Seed the two disposable sites used by the PracticeLab onboarding smoke."""

import os

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.installer import update_site_config

from open_practice.frappe_practice_onboarding import ROLE
from open_practice.openhr_company import REQUEST_FIELD


def _require_disposable(site):
    if frappe.local.site != site or frappe.session.user != "Administrator":
        raise frappe.PermissionError


def configure_openhr():
    _require_disposable("openhr.localhost")
    install_request_field()
    company = frappe.get_doc({
        "doctype": "Company",
        "company_name": "Synthetic Group LLC",
        "abbr": "SGQA",
        "country": "United States",
        "default_currency": "USD",
    })
    company.insert()
    frappe.get_doc({
        "doctype": "User",
        "email": "qa-company-connector@example.invalid",
        "first_name": "QA Company Connector",
        "send_welcome_email": 0,
        "api_key": os.environ["PRACTICELAB_COMPANY_KEY"],
        "api_secret": os.environ["PRACTICELAB_COMPANY_SECRET"],
        "roles": [{"role": "System Manager"}],
    }).insert()


def install_request_field():
    if frappe.session.user != "Administrator":
        raise frappe.PermissionError
    create_custom_fields({"Company": [{
        "fieldname": REQUEST_FIELD,
        "label": "openPractice request ID",
        "fieldtype": "Data",
        "insert_after": "company_name",
        "unique": 1,
        "no_copy": 1,
    }]})


def configure_practicelab():
    _require_disposable("practicelab.localhost")
    for alias in ("solo", "group", "lost"):
        email = f"onboarding-{alias}@example.invalid"
        frappe.get_doc({
            "doctype": "User",
            "email": email,
            "first_name": f"Synthetic {alias}",
            "send_welcome_email": 0,
            "new_password": os.environ["PRACTICELAB_SMOKE_PASSWORD"],
            "user_type": "Website User",
            "roles": [{"role": ROLE}],
        }).insert()
    update_site_config("open_practice_openhr_url", "http://127.0.0.1:8011")
    update_site_config("open_practice_openhr_test_http", 1)
    token = os.environ["PRACTICELAB_COMPANY_KEY"] + ":" + os.environ["PRACTICELAB_COMPANY_SECRET"]
    update_site_config("open_practice_openhr_token", token)
