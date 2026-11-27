"""Run T-26 with submitted native openHR records and the existing AP-11 adapter."""

import json
import os
from dataclasses import replace
from unittest import TestCase, skipUnless
from uuid import uuid4
from zoneinfo import ZoneInfo

from open_practice.openhr_adapter import ATTENDANCE, REQUEST, ROSTER, FrappeClient, FrappeHTTPError, OpenHRAdapter, OpenShiftRequest
from open_practice.staffing_demand import calculate_demand, summarize_demand
from open_practice.staffing_variance import TimeWindow, compare_staffing, rank_replacements
from open_practice.tests.test_openhr_adapter import submit
from open_practice.tests.staff_provider_conformance import StaffingConformance
from open_practice.tests.test_staffing_demand import at, overlapping_procedures
from open_practice.tests.test_staffing_variance import REPORTING_PERIOD, rn_callout_scenario


def ensure(client, doctype, name, fields):
    try:
        return client.get(doctype, name)
    except FrappeHTTPError as error:
        if error.status != 404:
            raise
        return client.create(doctype, fields)


def cancel(client, doctype, name):
    return client.request(
        "/api/method/frappe.client.cancel", method="POST",
        values={"doctype": doctype, "name": name}, response_key="message",
    )


@skipUnless(os.environ.get("OPENHR_T26_NATIVE") == "1", "No native openHR acceptance site configured")
class OpenHRNativeSiteAcceptanceTest(TestCase):
    def test_t26_through_native_shift_assignments_and_attendance(self):
        self._run_callout()

    def test_t26_keeps_overlapping_companies_isolated(self):
        self._run_callout(verify_company_isolation=True)

    def _run_callout(self, *, verify_company_isolation=False):
        url = os.environ["OPENHR_T26_URL"]

        def client(role):
            return FrappeClient(url, os.environ[f"OPENHR_T26_{role}_KEY"], os.environ[f"OPENHR_T26_{role}_SECRET"])

        service, manager, setup = map(client, ("SERVICE", "MANAGER", "SETUP"))
        identity_path = "/api/method/frappe.auth.get_logged_user"
        service_user = service.request(identity_path, response_key="message")
        manager_user = manager.request(identity_path, response_key="message")
        self.assertNotIn(service_user, ("Guest", "Administrator", manager_user))
        self.assertNotIn(manager_user, ("Guest", "Administrator"))

        adapter = OpenHRAdapter(service)
        series = f"native-t26-{uuid4().hex}"
        conformance = StaffingConformance(self, series)
        published, _, _, worked, candidates = rn_callout_scenario()
        first, called_out = published.assignments
        demand = calculate_demand(overlapping_procedures())
        interval, = demand
        self.assertEqual((interval.starts_at, interval.ends_at), (at(9, 10), at(9, 20)))
        summary, = summarize_demand(demand, REPORTING_PERIOD.starts_at, REPORTING_PERIOD.ends_at)
        self.assertEqual((summary.peak_headcount, summary.staff_minutes), (2, 20))

        site_zone = setup.get("System Settings", "System Settings").get("time_zone") or "Asia/Kolkata"
        zone = ZoneInfo(site_zone)
        native_day = interval.starts_at.astimezone(zone).date().isoformat()

        def local(value, fmt):
            return value.astimezone(zone).strftime(fmt)

        ensure(setup, "Warehouse Type", "Transit", {"name": "Transit"})

        def synthetic_company(name, abbr):
            return ensure(setup, "Company", name, {
                "company_name": name, "abbr": abbr, "default_currency": "USD", "country": "United States",
            })["name"]

        company = synthetic_company("Synthetic OpenPractice T26", "T26")
        gender = ensure(setup, "Gender", "Not Specified", {"gender": "Not Specified"})["name"]
        location = setup.create("Shift Location", {"location_name": series})["name"]
        shift_type = setup.create("Shift Type", {
            "name": series,
            "start_time": local(interval.starts_at, "%H:%M:%S"),
            "end_time": local(interval.ends_at, "%H:%M:%S"),
        })["name"]
        skills = {assignment.staff_id: (assignment.role, assignment.qualification) for assignment in published.assignments}
        skills.update({candidate.staff_id: (candidate.qualifications[0].role, candidate.qualifications[0].name)
                       for candidate in candidates})

        def create_roster(roster_series, employer, workers):
            employees = {
                staff_id: setup.create("Employee", {
                    "naming_series": "HR-EMP-", "first_name": staff_id, "gender": gender, "status": "Active",
                    "date_of_birth": "1990-01-01", "date_of_joining": "2024-01-01", "company": employer,
                })["name"]
                for staff_id in workers
            }
            roster = manager.create("OpenPractice Native Roster", {
                "roster_series": roster_series, "location_ref": interval.location,
                "shift_location": location, "work_date": native_day,
                "workers_json": json.dumps({
                    employees[staff_id]: {"staff_id": staff_id, "role": role, "qualification": qualification}
                    for staff_id, (role, qualification) in workers.items()
                }),
            })
            self.assertEqual(roster["time_zone"], site_zone)
            return employees, roster

        employees, scope = create_roster(series, company, skills)

        def shift(employee, employer, roster_name, request=None):
            fields = {
                "employee": employee, "company": employer, "shift_type": shift_type,
                "shift_location": location, "start_date": native_day, "end_date": native_day,
                "status": "Active", "custom_openpractice_roster": roster_name,
            }
            if request is not None:
                fields["custom_openpractice_coverage_request"] = request.correlation_id
            return manager.create("Shift Assignment", fields)

        def attendance(employee, employer, window):
            return manager.create("Attendance", {
                "employee": employee, "company": employer, "shift": shift_type,
                "attendance_date": native_day, "status": "Present",
                "in_time": local(window.starts_at, "%Y-%m-%d %H:%M:%S"),
                "out_time": local(window.ends_at, "%Y-%m-%d %H:%M:%S"),
            })

        def read():
            evidence = conformance.read(adapter)
            return evidence, compare_staffing(
                demand, evidence.published, period=REPORTING_PERIOD, revisions=evidence.revisions,
                attendance=evidence.attendance,
            )

        first_shift = submit(manager, shift(employees[first.staff_id], company, scope["name"]))
        called_out_shift = submit(manager, shift(employees[called_out.staff_id], company, scope["name"]))
        submit(manager, scope)
        evidence, baseline = read()
        self.assertEqual(baseline.published.assignments, published.assignments)
        self.assertEqual(baseline.revisions, ())
        self.assertEqual(baseline.published_gaps, ())

        cancel(manager, "Shift Assignment", called_out_shift["name"])
        evidence, after_callout = read()
        gap, = after_callout.revised_gaps
        self.assertEqual(
            (gap.starts_at, gap.ends_at, gap.headcount, gap.staff_minutes, gap.booking_ids),
            (at(9, 10), at(9, 20), 1, 10, ("procedure-1", "procedure-2")),
        )
        self.assertEqual(rank_replacements(gap, candidates, after_callout.revised), [candidates[1]])
        request = OpenShiftRequest(
            uuid4().hex, gap.location, gap.role, gap.qualification, TimeWindow(gap.starts_at, gap.ends_at), gap.headcount,
        )
        adapter.post_open_shift(request)
        self.assertEqual(service.get(REQUEST, request.correlation_id)["name"], request.correlation_id)
        evidence, pending = read()
        conformance.expect_open_shift(evidence, request, "pending")
        self.assertEqual(pending.revised_gaps, (gap,))

        if verify_company_isolation:
            other_company = synthetic_company("Synthetic OpenPractice T26 Isolation", "T26B")
            self.assertNotEqual(other_company, company)
            other_series = f"{series}-b"
            other_workers = {
                staff_id: (interval.role, interval.qualification)
                for staff_id in ("isolation-b-rn-1", "isolation-b-rn-2")
            }
            other_employees, other_scope = create_roster(other_series, other_company, other_workers)
            other_conformance = StaffingConformance(self, other_series)
            expected = {replace(first, staff_id=staff_id) for staff_id in other_workers}

            for employee in other_employees.values():
                submit(manager, shift(employee, other_company, other_scope["name"]))
            submit(manager, other_scope)
            other = other_conformance.read(adapter)
            self.assertEqual(set(other.published.assignments), expected)
            self.assertEqual(read(), (evidence, pending))

            for employee in other_employees.values():
                submit(manager, attendance(employee, other_company, first.window))
            other = other_conformance.read(adapter)
            self.assertEqual(set(other.attendance.worked_intervals), expected)
            self.assertEqual(read(), (evidence, pending))

        float_draft = shift(employees[candidates[1].staff_id], company, scope["name"], request)
        with self.assertRaises(FrappeHTTPError) as denied:
            submit(service, float_draft)
        self.assertEqual(denied.exception.status, 403)
        evidence, pending = read()
        conformance.expect_open_shift(evidence, request, "pending")
        self.assertEqual(pending.revised_gaps, (gap,))
        self.assertEqual(pending.revised, after_callout.revised)

        float_shift = submit(manager, float_draft)
        evidence, covered = read()
        conformance.expect_open_shift(evidence, request, "confirmed")
        self.assertEqual(covered.revised_gaps, ())
        self.assertEqual(covered.published, baseline.published)
        self.assertEqual(len(covered.revisions), 2)
        sources = json.loads(service.get(ROSTER, covered.revised.reference)["assignments_json"])
        self.assertEqual({row["source_shift_assignment"] for row in sources}, {first_shift["name"], float_shift["name"]})

        original_draft, float_attendance_draft = (
            attendance(employees[item.staff_id], company, item.window) for item in worked.worked_intervals
        )
        evidence, before_approval = read()
        self.assertIsNone(before_approval.approved_attendance_gaps)

        original = submit(manager, original_draft)
        self.assertEqual(original["custom_openpractice_shift_assignment"], first_shift["name"])
        evidence, partial = read()
        self.assertEqual(partial.attendance.unresolved_exceptions, (float_shift["name"],))
        self.assertEqual(partial.attendance.worked_intervals, (first,))

        float_attendance = submit(manager, float_attendance_draft)
        self.assertEqual(float_attendance["custom_openpractice_shift_assignment"], float_shift["name"])
        evidence, actual = read()
        self.assertEqual(actual.attendance.worked_intervals, worked.worked_intervals)
        self.assertEqual(actual.attendance.unresolved_exceptions, ())
        self.assertEqual(actual.approved_attendance_gaps, (replace(gap, ends_at=at(9, 12)),))
        self.assertEqual(actual.approved_attendance_gaps[0].staff_minutes, 2)
        self.assertEqual((actual.revised_to_actual[0].staff_id, actual.revised_to_actual[0].staff_minutes), ("float-rn", -2))
        self.assertEqual(actual.revised_gaps, ())
        self.assertEqual(actual.published, baseline.published)
        reports = adapter._submitted(ATTENDANCE, "roster_revision", actual.revised.reference)
        sources = json.loads(reports[-1]["worked_intervals_json"])
        self.assertEqual({row["source_attendance"] for row in sources}, {original["name"], float_attendance["name"]})

        # A native correction must withdraw the old approval from the adapter's current view.
        cancel(manager, "Attendance", float_attendance["name"])
        _, withdrawn = read()
        self.assertEqual(withdrawn.attendance.worked_intervals, (first,))
        self.assertEqual(withdrawn.attendance.unresolved_exceptions, (float_shift["name"],))
        last_worked = worked.worked_intervals[-1]
        submit(manager, attendance(employees[last_worked.staff_id], company, last_worked.window))
        _, corrected = read()
        self.assertEqual(corrected.approved_attendance_gaps, actual.approved_attendance_gaps)
        self.assertEqual(corrected.attendance.unresolved_exceptions, ())
