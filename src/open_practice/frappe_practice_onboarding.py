"""PracticeLab's authenticated, one-office practice setup."""

from uuid import uuid4

import frappe

from open_practice.frappe_session_desk import enabled
from open_practice.openhr_company import CompanyConflict, CompanyConnector, CompanyUnavailable


DOCTYPE = "OpenPractice Practice Setup"
ROLE = "OpenPractice Practice Administrator"
SETUP_FIELDS = {"office_name": 140, "office_address": 500, "legal_entity_name": 120}
PUBLIC_FIELDS = (
    *SETUP_FIELDS, "practice_type", "connection_status", "connection_mode",
    "requested_company", "openhr_site", "openhr_company",
)


def require_admin():
    if not enabled() or ROLE not in frappe.get_roles():
        raise frappe.PermissionError


def _required(value, size):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > size:
        frappe.throw(f"Enter a value between 1 and {size} characters.")
    return value.strip()


def _draft(*, lock=False):
    name = frappe.db.get_value(DOCTYPE, {"practice_admin": frappe.session.user}, for_update=lock)
    return frappe.get_doc(DOCTYPE, name, for_update=lock) if name else None


def _show(draft):
    return {field: draft.get(field) for field in PUBLIC_FIELDS} if draft else {}


def _connector():
    return CompanyConnector(
        frappe.conf.get("open_practice_openhr_url", ""),
        frappe.conf.get("open_practice_openhr_token", ""),
        allow_test_http=frappe.conf.get("open_practice_openhr_test_http") == 1,
        site_name=frappe.conf.get("open_practice_openhr_site_name"),
        country=frappe.conf.get("open_practice_openhr_country", "United States"),
        currency=frappe.conf.get("open_practice_openhr_currency", "USD"),
    )


@frappe.whitelist(methods=["GET"])
def get_setup():
    require_admin()
    return _show(_draft())


@frappe.whitelist(methods=["POST"])
def save_setup(office_name, office_address, legal_entity_name, practice_type):
    require_admin()
    values = dict(office_name=office_name, office_address=office_address, legal_entity_name=legal_entity_name)
    values = {field: _required(values[field], maximum) for field, maximum in SETUP_FIELDS.items()}
    if practice_type not in ("Single practitioner", "Group"):
        frappe.throw("Choose Single practitioner or Group.")
    values["practice_type"] = practice_type
    draft = _draft(lock=True)
    if draft and draft.create_submitted and draft.legal_entity_name != legal_entity_name.strip():
        frappe.throw("The legal entity is fixed for this Company request.")
    if draft and draft.openhr_company and draft.legal_entity_name != legal_entity_name.strip():
        frappe.throw("The legal entity is fixed for this Company connection.")
    if not draft:
        draft = frappe.new_doc(DOCTYPE)
        draft.practice_admin = frappe.session.user
        draft.request_id = uuid4().hex
    draft.update(values)
    draft.save(ignore_permissions=True)
    return _show(draft)


@frappe.whitelist(methods=["POST"])
def connect_company(mode, company_id=None, confirmed=False):
    require_admin()
    draft = _draft(lock=True)
    if not draft:
        frappe.throw("Save the practice first.")
    if draft.openhr_company:
        return _show(draft)
    if mode not in ("Create Company", "Link existing Company"):
        frappe.throw("Choose how to connect the Company.")
    if mode == "Link existing Company":
        company_id = _required(company_id, 140)
        if confirmed not in (True, 1, "1", "true"):
            frappe.throw("Confirm that the selected Company represents this legal entity.")
    if draft.create_submitted and mode != "Create Company":
        frappe.throw("Resolve the pending create request before selecting an existing Company.")

    draft.connection_mode = mode
    draft.requested_company = company_id if mode == "Link existing Company" else None

    def mark_submitted():
        draft.create_submitted = 1
        # Persist the request and its target before sending a write to openHR.
        # Concurrent requests that pass the initial lookup must reread this marker.
        draft.connection_status = "Connection unresolved"
        draft.save(ignore_permissions=True)
        frappe.db.commit()

    notice = None
    try:
        connector = _connector()
        if draft.openhr_site and draft.openhr_site != connector.site:
            raise CompanyConflict("The configured openHR site changed for this request.")
        draft.openhr_site = connector.site
        if mode == "Create Company":
            company = connector.create_or_recover(
                draft.request_id, draft.legal_entity_name,
                submitted=draft.create_submitted, mark_submitted=mark_submitted,
            )
        else:
            company = connector.link(company_id, draft.request_id)
    except CompanyUnavailable:
        draft.connection_status = "Connection unresolved"
    except CompanyConflict as error:
        draft.connection_status = "Company needs review"
        notice = str(error)
    else:
        draft.openhr_company = company
        draft.connection_status = "Company connected"

    if draft.create_submitted:
        # A retry can have finished reconciliation during the original HTTP call.
        current = _draft(lock=True)
        if current.openhr_company:
            return _show(current)
        current.openhr_company = draft.openhr_company
        current.connection_status = draft.connection_status
        draft = current
    draft.save(ignore_permissions=True)
    result = _show(draft)
    if notice:
        result["notice"] = notice
    return result
