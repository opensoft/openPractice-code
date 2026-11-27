"""Project submitted native Shift Assignments and Attendance on the HR site."""

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import get_datetime, get_system_timezone, get_time, getdate


NATIVE_ROSTER = "OpenPractice Native Roster"
ROSTER = "OpenPractice Roster Revision"
ATTENDANCE = "OpenPractice Attendance Reconciliation"
REQUEST = "OpenPractice Coverage Request"
ROSTER_LINK = "custom_openpractice_roster"
COVERAGE_LINK = "custom_openpractice_coverage_request"
ATTENDANCE_LINK = "custom_openpractice_shift_assignment"
WORKER_FIELDS = ("staff_id", "role", "qualification")


def _workers(scope):
    try:
        workers = json.loads(scope.workers_json)
        if not isinstance(workers, dict) or not workers:
            raise ValueError
        for employee, identity in workers.items():
            if not employee or set(identity) != set(WORKER_FIELDS) or any(
                not isinstance(value, str) or not value.strip() for value in identity.values()
            ):
                raise ValueError
        if len({identity["staff_id"] for identity in workers.values()}) != len(workers):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        frappe.throw("Map each employee to a unique staff reference, role and qualification.")
    return workers


def validate_scope(doc, method=None):
    doc.time_zone = get_system_timezone()
    _workers(doc)
    if frappe.db.exists(ROSTER, {"roster_series": doc.roster_series}):
        frappe.throw("Choose a roster series that has not been published.")


def _rows(doctype, filters, fields, *, order_by="name asc", limit=None):
    # These are current reads after the scope row is locked, including under MariaDB repeatable read.
    return frappe.db.get_values(
        doctype, filters, fields, as_dict=True, order_by=order_by, for_update=True, limit=limit,
    )


def _latest(doctype, field, value, fields):
    rows = _rows(
        doctype, {field: value, "docstatus": 1}, fields, order_by="sequence_number desc", limit=1,
    )
    return rows[0] if rows else None


def _get_scope(name):
    return frappe.get_doc(NATIVE_ROSTER, name, for_update=True) if name else None


def _covers_date(shift, work_date):
    return getdate(shift.start_date) <= work_date and (not shift.end_date or getdate(shift.end_date) >= work_date)


def lock_shift(doc, method=None):
    scope = _get_scope(doc.get(ROSTER_LINK))
    if scope is None or method == "before_cancel":
        return
    if doc.employee not in _workers(scope) or doc.shift_location != scope.shift_location or not _covers_date(
        doc, getdate(scope.work_date),
    ):
        frappe.throw("The shift must match the roster's employee mapping, shift location and work date.")


def _find_attendance_shift(doc):
    if not doc.shift or not doc.employee or not doc.attendance_date:
        return None
    day = getdate(doc.attendance_date)
    # The explicit link disambiguates shifts that share an employee, shift type and date.
    shifts = frappe.db.get_values("Shift Assignment", {
        "employee": doc.employee, "shift_type": doc.shift, "start_date": ["<=", day],
        "status": "Active", "docstatus": 1, ROSTER_LINK: ["is", "set"],
    }, ["name", ROSTER_LINK, "start_date", "end_date"], as_dict=True)
    matches = [
        shift.name for shift in shifts if _covers_date(shift, day)
        and frappe.db.get_value(NATIVE_ROSTER, shift[ROSTER_LINK], "work_date") == day
    ]
    if len(matches) > 1:
        frappe.throw("Choose the native shift assignment for this attendance record.")
    return matches[0] if matches else None


def _attendance_scope(doc):
    shift_name = doc.get(ATTENDANCE_LINK)
    if not shift_name:
        return None, None
    shift = frappe.db.get_value("Shift Assignment", shift_name, ROSTER_LINK)
    # Discover the scope without taking a shift-row lock first. All writes lock the scope first.
    scope = _get_scope(shift)
    if scope is None:
        frappe.throw("Choose a shift assignment linked to an openPractice roster.")
    return scope, frappe.get_doc("Shift Assignment", shift_name, for_update=True)


def lock_attendance(doc, method=None):
    if method == "before_submit" and not doc.get(ATTENDANCE_LINK):
        doc.set(ATTENDANCE_LINK, _find_attendance_shift(doc))
    scope, shift = _attendance_scope(doc)
    if scope is None or method == "before_cancel":
        return
    if (
        shift.docstatus != 1 or shift.status != "Active" or doc.employee != shift.employee
        or doc.shift != shift.shift_type or getdate(doc.attendance_date) != getdate(scope.work_date)
    ):
        frappe.throw("Attendance must match the confirmed shift's employee, shift type and roster date.")


def _utc(local, zone):
    # HRMS stores wall-clock datetimes. Reject a missing or ambiguous local hour.
    local = local.replace(tzinfo=None)
    candidates = {local.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc) for fold in (0, 1)}
    valid = {candidate for candidate in candidates if candidate.astimezone(zone).replace(tzinfo=None) == local}
    if len(valid) != 1:
        raise ValueError("The native time is ambiguous or does not exist.")
    return valid.pop()


def _native_assignments(scope):
    workers = _workers(scope)
    zone, day = ZoneInfo(scope.time_zone), getdate(scope.work_date)
    assignments = []
    shifts = _rows(
        "Shift Assignment", {ROSTER_LINK: scope.name, "docstatus": 1, "status": "Active"},
        ["name", "employee", "shift_type", "shift_location", "start_date", "end_date", COVERAGE_LINK],
    )
    for shift in shifts:
        if shift.employee not in workers or shift.shift_location != scope.shift_location or not _covers_date(shift, day):
            frappe.throw("A confirmed shift falls outside the published roster scope.")
        start_clock, end_clock = frappe.db.get_value("Shift Type", shift.shift_type, ["start_time", "end_time"])
        start = datetime.combine(day, get_time(start_clock))
        end = datetime.combine(day, get_time(end_clock))
        if end < start:
            end += timedelta(days=1)
        try:
            start, end = _utc(start, zone), _utc(end, zone)
            if end <= start:
                raise ValueError
        except ValueError:
            frappe.throw("The native shift must have a valid positive interval in the roster time zone.")
        assignments.append({
            **workers[shift.employee],
            "location": scope.location_ref,
            "starts_at": start.isoformat(),
            "ends_at": end.isoformat(),
            "source_shift_assignment": shift.name,
            **({"coverage_request": shift[COVERAGE_LINK]} if shift[COVERAGE_LINK] else {}),
        })
    return sorted(assignments, key=lambda row: (row["staff_id"], row["starts_at"], row["source_shift_assignment"]))


def _submit(doctype, fields):
    doc = frappe.get_doc({"doctype": doctype, **fields})
    doc.flags.openhr_native_projection = True
    doc.insert(ignore_permissions=True)
    doc.submit()
    return doc


def require_native_evidence(doc, method=None):
    if doc.flags.openhr_native_projection:
        return
    series = doc.roster_series if doc.doctype == ROSTER else frappe.db.get_value(
        ROSTER, doc.roster_revision, "roster_series",
    )
    if frappe.db.exists(NATIVE_ROSTER, {"roster_series": series}):
        frappe.throw("Use native shift assignments and attendance to update this roster series.")


def _current_roster(scope):
    return _latest(ROSTER, "roster_series", scope.roster_series, ["name", "sequence_number", "assignments_json"])


def _worked(assignments, records, zone):
    by_shift = defaultdict(list)
    for record in records:
        by_shift[record[ATTENDANCE_LINK]].append(record)
    intervals, unresolved = [], []
    for assignment in assignments:
        shift_name = assignment["source_shift_assignment"]
        matches = by_shift[shift_name]
        if len(matches) != 1:
            unresolved.append(shift_name)
            continue
        record = matches[0]
        if record.status in ("Absent", "On Leave"):
            continue
        try:
            if record.status not in ("Present", "Half Day") or not record.in_time or not record.out_time:
                raise ValueError
            start = _utc(get_datetime(record.in_time), zone)
            end = _utc(get_datetime(record.out_time), zone)
            if end <= start:
                raise ValueError
        except (TypeError, ValueError):
            unresolved.append(record.name)
            continue
        intervals.append({
            **{field: assignment[field] for field in (*WORKER_FIELDS, "location")},
            "starts_at": start.isoformat(),
            "ends_at": end.isoformat(),
            "source_attendance": record.name,
        })
    return intervals, unresolved


def _sync_attendance(scope, roster):
    assignments = json.loads(roster.assignments_json)
    if not assignments:
        return
    latest = _latest(ATTENDANCE, "roster_revision", roster.name, [
        "sequence_number", "worked_intervals_json", "exceptions_json",
    ])
    records = _rows("Attendance", {
        ATTENDANCE_LINK: ["in", [row["source_shift_assignment"] for row in assignments]],
        "docstatus": 1, "attendance_date": scope.work_date,
    }, ["name", ATTENDANCE_LINK, "status", "in_time", "out_time"])
    if not records and not latest:
        return
    intervals, exceptions = _worked(assignments, records, ZoneInfo(scope.time_zone))
    if latest and (intervals, exceptions) == (
        json.loads(latest.worked_intervals_json), json.loads(latest.exceptions_json),
    ):
        return
    _submit(ATTENDANCE, {
        "roster_revision": roster.name,
        "sequence_number": latest.sequence_number + 1 if latest else 1,
        "worked_intervals_json": json.dumps(intervals),
        "exceptions_json": json.dumps(exceptions),
    })


def _sync_roster(scope, reason):
    latest = _current_roster(scope)
    assignments = _native_assignments(scope)
    if latest and assignments == json.loads(latest.assignments_json):
        return
    roster = _submit(ROSTER, {
        "roster_series": scope.roster_series,
        "sequence_number": latest.sequence_number + 1 if latest else 1,
        "recorded_at_iso": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "assignments_json": json.dumps(assignments),
    })
    _sync_attendance(scope, roster)


def publish_roster(doc, method=None):
    _sync_roster(doc, "Published native roster")


def project_shift(doc, method=None):
    scope = _get_scope(doc.get(ROSTER_LINK))
    if scope and scope.docstatus == 1:
        _sync_roster(scope, f"Shift Assignment {doc.name} ({method})")


def project_attendance(doc, method=None):
    scope, _shift = _attendance_scope(doc)
    if scope and scope.docstatus == 1:
        _sync_attendance(scope, _current_roster(scope))
