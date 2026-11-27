"""Seed synthetic PracticeLab and openHR onboarding rehearsal sites."""

import os

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.installer import update_site_config

from open_practice.frappe_practice_onboarding import ROLE
from open_practice.openhr_company import REQUEST_FIELD


def _require_synthetic(*sites):
    if frappe.local.site not in sites or frappe.session.user != "Administrator":
        raise frappe.PermissionError


def configure_openhr():
    _require_synthetic("openhr.localhost", "openhr.practicelab.internal")
    install_request_field()
    if not frappe.db.exists("Warehouse Type", "Transit"):
        frappe.get_doc({"doctype": "Warehouse Type", "name": "Transit"}).insert()
    company = frappe.get_doc({
        "doctype": "Company",
        "company_name": "Synthetic Group LLC",
        "abbr": "SGQA",
        "country": "United States",
        "default_currency": "USD",
    })
    if not frappe.db.exists("Company", company.company_name):
        company.insert()
    connector_email = "qa-company-connector@example.invalid"
    if frappe.db.exists("User", connector_email):
        return
    frappe.get_doc({
        "doctype": "User",
        "email": connector_email,
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
    _require_synthetic("practicelab.localhost", "practicelab.opensoft.dev")
    if frappe.conf.get("open_practice_practicelab") != 1:
        raise frappe.PermissionError
    for alias in ("solo", "group", "lost"):
        email = f"onboarding-{alias}@example.invalid"
        if frappe.db.exists("User", email):
            continue
        frappe.get_doc({
            "doctype": "User",
            "email": email,
            "first_name": f"Synthetic {alias}",
            "send_welcome_email": 0,
            "new_password": os.environ["PRACTICELAB_SMOKE_PASSWORD"],
            "user_type": "Website User",
            "roles": [{"role": ROLE}],
        }).insert()
    update_site_config("open_practice_openhr_url", os.environ.get(
        "PRACTICELAB_OPENHR_CONNECTOR_URL", "http://127.0.0.1:8011",
    ))
    if site_name := os.environ.get("PRACTICELAB_OPENHR_TEST_SITE_NAME"):
        update_site_config("open_practice_openhr_site_name", site_name)
    update_site_config("open_practice_openhr_test_http", 1)
    token = os.environ["PRACTICELAB_COMPANY_KEY"] + ":" + os.environ["PRACTICELAB_COMPANY_SECRET"]
    update_site_config("open_practice_openhr_token", token)


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("configure-openhr", "install-request-field"))
    parser.add_argument("site")
    args = parser.parse_args()

    os.chdir("sites")
    frappe.init(site=args.site)
    frappe.connect()
    try:
        frappe.set_user("Administrator")
        if args.action == "configure-openhr":
            configure_openhr()
        else:
            install_request_field()
        frappe.db.commit()
    finally:
        frappe.destroy()


if __name__ == "__main__":
    main()
