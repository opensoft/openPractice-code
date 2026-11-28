"""Frappe persistence and authenticated requests for PracticeLab."""

import json
import re
from datetime import date, time
from hashlib import sha256
from uuid import uuid4

import frappe

from open_practice.desk_billing import can_start_mock_billing, start_mock_billing
from open_practice.mock_openchart import is_ready_demo_appointment
from open_practice.npi_registry import NPI_PATTERN, RegistryUnavailable, lookup_facility, search_facilities
from open_practice.session_desk import (
    ADDRESS_FIELDS, CONTRACT_STATUSES, FACILITY_NAME_LIMIT, FACILITY_TYPES, HOSPITAL_SETTINGS,
    INSURANCE_FIELDS, LIVING_ARRANGEMENTS, NPI_SEARCH_FIELDS, PATIENT_FIELDS, PATIENT_REFERENCE,
    SITE_FIELDS, STAGES, TransitionConflict, _address_values, _appointment, _event, _facility,
    _insurance_values, _location_reference, _location_text, _patient_values, _site_values,
)


ROLE = "OpenPractice Scheduler"
PATIENT = "OpenPractice Desk Patient"
FACILITY = "OpenPractice Desk Facility"
HOME = "OpenPractice Desk Home"
APPOINTMENT = "OpenPractice Desk Appointment"
ADDRESS = tuple(key for key, _, _ in ADDRESS_FIELDS)
COVERAGE = tuple(key for key, _, _ in INSURANCE_FIELDS)
FACILITY_FIELDS = (
    "label", "kind", "facility_type", "hospital_pos_code", "archived", "contract_status",
    "contract_pharmacy_reference", *SITE_FIELDS,
)
APPOINTMENT_FIELDS = ("patient", "location", "mode", "day", "slot", "group_name", "roster", "events")


def enabled():
    return frappe.conf.get("open_practice_practicelab") == 1


def require_scheduler():
    if not enabled() or ROLE not in frappe.get_roles():
        frappe.throw("PracticeLab requires an authorized scheduler account.", frappe.PermissionError)


def seed():
    """Seed demo records on install or migration of the PracticeLab site."""
    if not enabled():
        return
    for number in range(1, 5):
        reference = f"demo-{number:02}"
        if not frappe.db.exists(PATIENT, reference):
            _insert(PATIENT, reference, first_name="Demo", last_name=f"patient {number:02}")
    if not frappe.db.exists(FACILITY, "clinic"):
        _insert(FACILITY, "clinic", label="Demo clinic", kind="clinic", facility_type="office",
                contract_status="unknown")
    frappe.db.set_single_value("Website Settings", "home_page", "practice")
    frappe.db.set_single_value("Website Settings", "disable_signup", 1)


def seed_workspace():
    """Create the default Session Desk workspace after the scheduler role is installed."""
    if not enabled() or frappe.db.exists("Workspace", "openPractice"):
        return

    label = "Session Desk"
    last_sequence = frappe.db.get_value("Workspace", {}, "max(sequence_id)") or 0
    frappe.get_doc({
        "doctype": "Workspace",
        "label": "openPractice",
        "title": "openPractice",
        "module": "Open Practice",
        "public": 1,
        "icon": "calendar",
        "sequence_id": last_sequence + 1,
        "roles": [{"role": ROLE}],
        "shortcuts": [{"label": label, "type": "URL", "url": "/practice"}],
        "content": json.dumps([
            {"id": "sessiondesk", "type": "shortcut", "data": {"shortcut_name": label, "col": 4}},
        ]),
    }).insert()


def _insert(doctype, reference, **fields):
    # Endpoint writes are role-checked and validated. Raw resource writes stay restricted.
    return frappe.get_doc({"doctype": doctype, **fields}).insert(
        ignore_permissions=True, set_name=reference,
    )


def _one(doctype, reference, fields):
    return frappe.db.get_value(doctype, reference, ("name", *fields), as_dict=True)


def _all(doctype, fields, **kwargs):
    return frappe.get_all(doctype, fields=["name", *fields], **kwargs)


def _display_patient(row):
    patient = {key: row.get(key) for key in PATIENT_FIELDS}
    patient["reference"] = row.name
    if patient["date_of_birth"]:
        patient["date_of_birth"] = str(patient["date_of_birth"])
    return patient


def _get_patient(reference, values):
    if not isinstance(reference, str) or not re.fullmatch(PATIENT_REFERENCE, reference):
        raise ValueError("Choose a saved patient or enter a new one.")
    saved = _one(PATIENT, reference, PATIENT_FIELDS)
    if "new_patient" not in values:
        if saved is None:
            raise ValueError("Choose a saved patient or enter a new one.")
        return reference
    if not reference.startswith("patient-"):
        raise ValueError("Open a new appointment to enter patient details.")
    details = dict(zip(PATIENT_FIELDS, _patient_values(values["new_patient"])))
    coverage = _insurance_values(values["insurance"]) if "insurance" in values else None
    if saved:
        current = _display_patient(saved)
        if any((current[key] or None) != value for key, value in details.items()):
            raise ValueError("Patient details changed. Select the saved patient or open a new appointment.")
    else:
        chart = details["chart_reference"]
        if chart and frappe.db.exists(PATIENT, {"chart_reference": chart}):
            raise ValueError("That chart reference belongs to a saved patient.")
        _insert(PATIENT, reference, **details, **(coverage or {}))
    return reference


def _display_facility(row):
    result = dict(row)
    result["reference"] = result.pop("name")
    result["name"] = result.pop("label")
    pharmacy = result["contract_pharmacy_reference"]
    linked = _one(FACILITY, pharmacy, ("label", "npi")) if pharmacy else None
    return _facility({
        **result, "linked_pharmacy_name": linked.label if linked else None,
        "linked_pharmacy_npi": linked.npi if linked else None,
        "contract_pharmacy_name": None, "contract_pharmacy_npi": None,
    })


def _save_facility(values):
    reference = values.get("reference", "")
    if not isinstance(reference, str):
        raise ValueError("Choose a registered facility.")
    existing = _one(FACILITY, reference, FACILITY_FIELDS) if reference else None
    if reference and not existing:
        raise ValueError("Choose a registered facility.")
    facility_type = values.get("facility_type", existing.facility_type if existing else None)
    if facility_type == "":
        facility_type = None
    if facility_type is not None and (not isinstance(facility_type, str) or facility_type not in FACILITY_TYPES):
        raise ValueError("Choose a facility type.")
    if existing and existing.facility_type == "pharmacy" and facility_type != "pharmacy":
        if frappe.db.exists(FACILITY, {"contract_pharmacy_reference": reference}):
            raise ValueError("Remove this pharmacy from the facilities that use it before changing its type.")
    pos = None
    if facility_type == "hospital":
        pos = values.get("pos_code", existing.hospital_pos_code if existing else None) or None
    if pos is not None and (not isinstance(pos, str) or pos not in HOSPITAL_SETTINGS):
        raise ValueError("Choose a valid hospital setting for the place of service.")
    status = None
    pharmacy = None
    if facility_type != "pharmacy":
        status = values.get("contract_status", existing.contract_status if existing else None) or "unknown"
        if status not in CONTRACT_STATUSES:
            raise ValueError("Choose a contract status.")
        if status == "contracted":
            previous = existing.contract_pharmacy_reference if existing else None
            pharmacy = values.get("contract_pharmacy_reference", previous) or None
            if pharmacy:
                linked = _one(FACILITY, pharmacy, ("facility_type", "archived")) if isinstance(pharmacy, str) else None
                if not linked or linked.facility_type != "pharmacy" or (linked.archived and pharmacy != previous):
                    raise ValueError("Choose an active pharmacy from the facility directory.")
    name = _location_text(values, "name", "location name", FACILITY_NAME_LIMIT)
    kind = "clinic" if facility_type == "office" else "alf"
    fields = {
        "label": name, "kind": kind, "facility_type": facility_type, "hospital_pos_code": pos,
        "contract_status": status, "contract_pharmacy_reference": pharmacy, **_site_values(values, existing),
    }
    duplicate = frappe.db.get_value(FACILITY, {"label": name}, "name")
    if duplicate and duplicate != reference:
        saved = _one(FACILITY, duplicate, FACILITY_FIELDS)
        if not saved.archived and not reference and all((saved[key] or None) == (v or None) for key, v in fields.items()):
            return _display_facility(saved)
        raise ValueError("That location name is already in use. Choose the saved location or use a different name.")
    if existing:
        frappe.db.set_value(FACILITY, reference, fields)
    else:
        reference = f"{kind}-{uuid4().hex}"
        _insert(FACILITY, reference, **fields)
    return _display_facility(_one(FACILITY, reference, FACILITY_FIELDS))


def _book_location(reference, patient, values):
    kind = values.get("location_kind")
    if kind == "home":
        if not _location_reference(reference, "home"):
            raise ValueError("Choose a saved home or enter a new address.")
        fields = ("patient", "living_arrangement", *ADDRESS)
        home = _one(HOME, reference, fields)
        if "new_location" in values:
            details = values["new_location"]
            if not isinstance(details, dict) or details.get("living_arrangement") not in LIVING_ARRANGEMENTS:
                raise ValueError("Choose whether the patient lives alone or with a caregiver.")
            record = {"patient": patient, "living_arrangement": details["living_arrangement"], **_address_values(details)}
            if not home:
                _insert(HOME, reference, **record)
            elif any((home[key] or "") != value for key, value in record.items()):
                raise ValueError("Choose a saved home for this patient or add a new one.")
        elif not home or home.patient != patient:
            raise ValueError("Choose a saved home for this patient or add a new one.")
    else:
        if "new_location" in values:
            raise ValueError("Register the office or facility in Facilities, then select it for the appointment.")
        site = _one(FACILITY, reference, ("kind", "archived"))
        if not site or site.kind != kind or site.archived:
            raise ValueError("Choose an active facility of the selected type.")


def _display_appointment(row, patients, facilities, homes):
    members = json.loads(row.roster or "[]")
    site = facilities.get(row.location)
    home = homes.get(row.location)
    patient = patients.get(row.patient, {})
    address = site or home or {}
    return _appointment({
        "id": row.name, "patient": row.patient, "group_reference": row.name if members else None,
        "group_name": row.group_name, "location": row.location, "mode": row.mode, "day": str(row.day),
        "time": row.slot, "events": row.events, "member_count": len(members) if members else 1,
        "facility_name": site["name"] if site else "Patient home",
        "location_kind": site["kind"] if site else "home", "facility_type": site["facility_type"] if site else None,
        "hospital_pos_code": site["pos_code"] if site and site["facility_type"] == "hospital" else None,
        "contract_status": site["contract_status"] if site else None,
        "contract_pharmacy_name": site["contract_pharmacy_name"] if site else None,
        "contract_pharmacy_npi": site["contract_pharmacy_npi"] if site else None,
        "living_arrangement": home.living_arrangement if home else None,
        **{key: address.get(key) for key in ADDRESS},
        **{key: patient.get(key) for key in PATIENT_FIELDS},
    })


def _appointments(rows):
    patients = {row.name: _display_patient(row) for row in _all(PATIENT, PATIENT_FIELDS)}
    facilities = {row.name: _display_facility(row) for row in _all(FACILITY, FACILITY_FIELDS)}
    homes = {row.name: row for row in _all(HOME, ("living_arrangement", *ADDRESS))}
    appointments = [_display_appointment(row, patients, facilities, homes) for row in rows]
    if can_start_mock_billing():
        for appointment in appointments:
            if is_ready_demo_appointment(appointment):
                appointment["mock_billing_available"] = True
    return appointments


def _book(values):
    location, mode, day, slot = (values.get(key) for key in ("location", "mode", "day", "time"))
    if not isinstance(location, str) or not location or mode not in ("in-person", "telemedicine"):
        raise ValueError("Choose a location and visit mode.")
    try:
        if date.fromisoformat(day).isoformat() != day or time.fromisoformat(slot).isoformat(timespec="minutes") != slot:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("Enter a valid appointment date and time.") from None
    group = values.get("group")
    patient = None
    members = []
    group_name = None
    if "group" in values:
        if not isinstance(group, dict) or not _location_reference(group.get("reference"), "group"):
            raise ValueError("Open a new group appointment to enter the group details.")
        entries = group.get("patients")
        if not isinstance(entries, list) or not entries or any(not isinstance(entry, dict) for entry in entries):
            raise ValueError("Add at least one patient to the group.")
        reference = group["reference"]
        group_name = _location_text(group, "name", "group name", 120, optional=True) or "Group visit"
        members = [_get_patient(entry.get("patient"), entry) for entry in entries]
        if len(set(members)) != len(members):
            raise ValueError("Add each patient to the group once.")
        host = values.get("home_patient")
        if values.get("location_kind") == "home" and host not in members:
            raise ValueError("Choose which group patient the home belongs to.")
    else:
        patient = host = _get_patient(values.get("patient"), values)
        natural_key = json.dumps([patient, location, mode, day, slot]).encode("utf-8")
        reference = "visit-" + sha256(natural_key).hexdigest()
    _book_location(location, host, values)
    booking = {
        "patient": patient, "location": location, "mode": mode, "day": day, "slot": slot,
        "group_name": group_name, "roster": json.dumps(members),
    }
    existing = _one(APPOINTMENT, reference, APPOINTMENT_FIELDS)
    if existing:
        if any((str(existing[key]) if key == "day" else existing[key] or None) != value
               for key, value in booking.items()):
            raise TransitionConflict("This appointment is already booked. Open a new appointment to change it.")
    else:
        event = {**_event("scheduled"), "recorded_by": frappe.session.user}
        _insert(APPOINTMENT, reference, **booking, events=json.dumps([event]))
    return _appointments([_one(APPOINTMENT, reference, APPOINTMENT_FIELDS)])[0]


def _advance(reference, values):
    row = _one(APPOINTMENT, reference, APPOINTMENT_FIELDS)
    if not row:
        raise KeyError("Appointment not found.")
    events = json.loads(row.events)
    stage = events[-1]["stage"]
    if values.get("stage") != stage or stage == "wrapped":
        raise TransitionConflict("This session has moved on. Use the current action.")
    if stage == "ended" and values.get("chart_complete") is not True:
        raise ValueError("Confirm the clinician finished charting before wrap-up.")
    event = _event(STAGES[STAGES.index(stage) + 1])
    events.append({**event, "recorded_by": frappe.session.user})
    row.events = json.dumps(events)
    frappe.db.set_value(APPOINTMENT, reference, {"events": row.events, "stage": event["stage"]})
    return _appointments([row])[0]


APPOINTMENT_REFERENCE = r"(?:visit-[0-9a-f]{64}|group-[0-9a-f]{32})"
FACILITY_REFERENCE = r"clinic|(?:clinic|alf)-[0-9a-f]{32}"


def _read(route):
    if route == "/api/facility-types":
        return {
            "types": [{"value": value, "label": label, "pos_code": code}
                      for value, (label, code) in FACILITY_TYPES.items()],
            "hospital_settings": HOSPITAL_SETTINGS,
        }
    if route == "/api/facilities":
        rows = _all(FACILITY, FACILITY_FIELDS, order_by="label asc")
        return [_display_facility(row) for row in rows]
    if route == "/api/patients":
        return [_display_patient(row) for row in _all(PATIENT, PATIENT_FIELDS, order_by="last_name asc, first_name asc")]
    if route == "/api/appointments":
        return _appointments(_all(APPOINTMENT, APPOINTMENT_FIELDS, order_by="day asc, slot asc, creation asc"))
    if match := re.fullmatch(rf"/api/facilities/npi/({NPI_PATTERN})", route):
        return lookup_facility(match[1])
    if match := re.fullmatch(rf"/api/patients/{PATIENT_REFERENCE}/homes", route):
        rows = _all(HOME, ADDRESS, filters={"patient": match[1]}, order_by="street_address asc")
        return [{"reference": row.pop("name"), **row} for row in rows]
    if match := re.fullmatch(rf"/api/appointments/({APPOINTMENT_REFERENCE})/patients", route):
        row = _one(APPOINTMENT, match[1], ("patient", "roster"))
        if not row:
            raise KeyError("Appointment not found.")
        members = json.loads(row.roster) if not row.patient else (row.patient,)
        result = []
        for reference in members:
            patient = _one(PATIENT, reference, (*PATIENT_FIELDS, *COVERAGE))
            coverage = {key: patient[key] for key in COVERAGE}
            result.append({**_display_patient(patient), "insurance": coverage if coverage["payer_name"] else None})
        return result
    raise KeyError("Page not found.")


def _write(route, values):
    if route == "/api/facilities/npi/search":
        return search_facilities({
            key: _location_text(values, key, label, limit, optional=True)
            for key, label, limit in NPI_SEARCH_FIELDS
        })
    # Use the synthetic clinic row to serialize QA mutations, including duplicate bookings.
    # Frappe releases the lock on commit or rollback at the end of the request.
    if not frappe.db.get_value(FACILITY, "clinic", "name", for_update=True):
        raise ValueError("The PracticeLab fixture has not been initialized.")
    if route == "/api/appointments":
        return _book(values)
    if route == "/api/facilities":
        return _save_facility(values)
    if match := re.fullmatch(rf"/api/appointments/({APPOINTMENT_REFERENCE})/advance", route):
        return _advance(match[1], values)
    if match := re.fullmatch(rf"/api/appointments/({APPOINTMENT_REFERENCE})/mock-billing", route):
        return {"billing_case": start_mock_billing(match[1])}
    if match := re.fullmatch(rf"/api/facilities/({FACILITY_REFERENCE})/(archive|restore)", route):
        if not frappe.db.exists(FACILITY, match[1]):
            raise KeyError("Facility not found.")
        frappe.db.set_value(FACILITY, match[1], "archived", int(match[2] == "archive"))
        return _display_facility(_one(FACILITY, match[1], FACILITY_FIELDS))
    if match := re.fullmatch(rf"/api/patients/{PATIENT_REFERENCE}/insurance", route):
        coverage = _insurance_values(values)
        if coverage is None:
            raise ValueError("Enter the insurance payer and member ID.")
        if not frappe.db.exists(PATIENT, match[1]):
            raise KeyError("Patient not found.")
        frappe.db.set_value(PATIENT, match[1], coverage)
        return coverage
    raise KeyError("Page not found.")


@frappe.whitelist(methods=["GET", "POST"])
def request(route, values=None):
    require_scheduler()
    if not isinstance(route, str) or len(route) > 150:
        frappe.throw("Choose a Session Desk action.", frappe.ValidationError)
    method = frappe.request.method
    try:
        if method == "GET":
            return _read(route)
        if not isinstance(values, dict) or len(json.dumps(values)) > 65_536:
            raise ValueError("Send a JSON object of up to 64 KB.")
        return _write(route, values)
    except (TransitionConflict, KeyError, RegistryUnavailable, ValueError) as error:
        if method == "POST":
            frappe.db.rollback()
        if isinstance(error, TransitionConflict):
            status, message = 409, str(error)
        elif isinstance(error, KeyError):
            status, message = 404, error.args[0]
        elif isinstance(error, RegistryUnavailable):
            status, message = 502, str(error)
        else:
            status, message = 400, str(error)
        frappe.local.response["http_status_code"] = status
        return {"error": message}
