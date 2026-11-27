"""Add the roster scope and native scheduling links to a prepared HR site."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from openhr_ext.native_scheduling import ATTENDANCE, NATIVE_ROSTER, REQUEST, ROSTER


def install():
    for doctype in (ROSTER, ATTENDANCE, REQUEST):
        if not frappe.db.exists("DocType", doctype):
            frappe.throw("Install the openPractice scheduling DocTypes before installing OpenHR Ext.")
    if not frappe.db.exists("DocType", NATIVE_ROSTER):
        frappe.get_doc({
            "doctype": "DocType",
            "name": NATIVE_ROSTER,
            "module": "OpenHR Ext",
            "custom": 1,
            "is_submittable": 1,
            "autoname": "hash",
            "track_changes": 1,
            "fields": [
                _field("roster_series", "Roster series", unique=1),
                _field("location_ref", "openPractice location"),
                _field("shift_location", "Shift location", "Link", options="Shift Location"),
                _field("work_date", "Native shift date", "Date"),
                _field("time_zone", "Native clock time zone", read_only=1, reqd=0),
                _field("workers_json", "Workforce references and qualifications", "Long Text"),
            ],
            "permissions": [{"role": "HR Manager", "read": 1, "create": 1, "write": 1, "submit": 1}],
        }).insert()
    create_custom_fields({
        "Shift Assignment": [
            _link("custom_openpractice_roster", "openPractice roster", NATIVE_ROSTER, "shift_location"),
            _link("custom_openpractice_coverage_request", "openPractice coverage request", REQUEST, "custom_openpractice_roster"),
        ],
        "Attendance": [
            _link("custom_openpractice_shift_assignment", "openPractice shift assignment", "Shift Assignment", "shift"),
        ],
    })


def _field(fieldname, label, fieldtype="Data", **options):
    return {"reqd": 1, "fieldname": fieldname, "label": label, "fieldtype": fieldtype, **options}


def _link(fieldname, label, options, insert_after):
    return {"fieldname": fieldname, "label": label, "fieldtype": "Link", "options": options, "insert_after": insert_after}
