"""Start a reviewed billing case from a synthetic PracticeLab appointment."""

import frappe

from open_practice.mock_openchart import DESK_ENCOUNTER_PREFIX, desk_encounter_reference


BILLING_CASE = "OpenPractice Billing Case"
DESK_APPOINTMENT = "OpenPractice Desk Appointment"
APPOINTMENT_FIELDS = ("patient", "location", "mode", "day", "slot", "stage")


def rehearsal_enabled():
    return (
        frappe.conf.get("open_practice_practicelab") == 1
        and frappe.conf.get("open_practice_novitas_test") == 1
        and frappe.conf.get("open_practice_novitas_roster_mode", "rehearsal") == "rehearsal"
    )


def can_start_mock_billing():
    return rehearsal_enabled() and "OpenPractice Test Biller" in frappe.get_roles()


def validate_mock_source(source, encounter_ref):
    if not source and not (encounter_ref or "").startswith(DESK_ENCOUNTER_PREFIX):
        return
    if not source or not rehearsal_enabled():
        raise ValueError("A mock Desk encounter requires its original PracticeLab appointment on a rehearsal site.")
    appointment = frappe.db.get_value(DESK_APPOINTMENT, source, APPOINTMENT_FIELDS, as_dict=True)
    if not appointment or desk_encounter_reference(appointment) != encounter_ref:
        raise ValueError("The mock encounter does not match this appointment.")


def start_mock_billing(reference):
    if not can_start_mock_billing():
        frappe.throw("Mock billing requires a PracticeLab rehearsal biller.", frappe.PermissionError)
    appointment = frappe.db.get_value(DESK_APPOINTMENT, reference, APPOINTMENT_FIELDS, as_dict=True, for_update=True)
    if not appointment:
        raise KeyError("Appointment not found.")
    encounter_ref = desk_encounter_reference(appointment)
    if case_name := frappe.db.get_value(BILLING_CASE, {"source_appointment": reference}, "name"):
        frappe.get_doc(BILLING_CASE, case_name).check_permission("read")
        return case_name

    # These charges are part of the mock office-visit rehearsal. A biller must
    # review the case and verify synthetic coverage before submitting it.
    return frappe.get_doc(
        doctype=BILLING_CASE,
        chart_encounter_ref=encounter_ref,
        source_appointment=reference,
        place_of_service="11",
        charges=[{"procedure_code": "99213", "charge": "149.90", "units": 1, "diagnosis_pointers": "1,2"}],
    ).insert().name
