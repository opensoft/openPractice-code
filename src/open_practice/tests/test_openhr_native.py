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

        company_name = "Synthetic OpenPractice T26"
        ensure(setup, "Warehouse Type", "Transit", {"name": "Transit"})
        company = ensure(setup, "Company", company_name, {
            "company_name": company_name, "abbr": "T26", "default_currency": "USD", "country": "United States",
        })["name"]
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
        employees = {
            staff_id: setup.create("Employee", {
                "naming_series": "HR-EMP-", "first_name": staff_id, "gender": gender, "status": "Active",
                "date_of_birth": "1990-01-01", "date_of_joining": "2024-01-01", "company": company,
            })["name"]
            for staff_id in skills
        }
        scope = manager.create("OpenPractice Native Roster", {
            "roster_series": series, "location_ref": interval.location, "shift_location": location, "work_date": native_day,
            "workers_json": json.dumps({
                employees[staff_id]: {"staff_id": staff_id, "role": role, "qualification": qualification}
                for staff_id, (role, qualification) in skills.items()
            }),
        })
        self.assertEqual(scope["time_zone"], site_zone)

        def shift(staff_id, request=None):
            fields = {
                "employee": employees[staff_id], "company": company, "shift_type": shift_type,
                "shift_location": location, "start_date": native_day, "end_date": native_day,
                "status": "Active", "custom_openpractice_roster": scope["name"],
            }
            if request is not None:
                fields["custom_openpractice_coverage_request"] = request.correlation_id
            return manager.create("Shift Assignment", fields)

        def read():
            evidence = adapter.get_staffing(series)
            return evidence, compare_staffing(
                demand, evidence.published, period=REPORTING_PERIOD, revisions=evidence.revisions,
                attendance=evidence.attendance,
            )

        first_shift = submit(manager, shift(first.staff_id))
        called_out_shift = submit(manager, shift(called_out.staff_id))
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
        self.assertEqual((evidence.coverage_status(request), pending.revised_gaps), ("pending", (gap,)))

        float_draft = shift(candidates[1].staff_id, request)
        with self.assertRaises(FrappeHTTPError) as denied:
            submit(service, float_draft)
        self.assertEqual(denied.exception.status, 403)
        evidence, pending = read()
        self.assertEqual((evidence.coverage_status(request), pending.revised_gaps), ("pending", (gap,)))
        self.assertEqual(pending.revised, after_callout.revised)

        float_shift = submit(manager, float_draft)
        evidence, covered = read()
        self.assertEqual(evidence.coverage_status(request), "confirmed")
        self.assertEqual(covered.revised_gaps, ())
        self.assertEqual(covered.published, baseline.published)
        self.assertEqual(len(covered.revisions), 2)
        sources = json.loads(service.get(ROSTER, covered.revised.reference)["assignments_json"])
        self.assertEqual({row["source_shift_assignment"] for row in sources}, {first_shift["name"], float_shift["name"]})

        def attendance(assignment):
            return manager.create("Attendance", {
                "employee": employees[assignment.staff_id], "company": company, "shift": shift_type,
                "attendance_date": native_day, "status": "Present",
                "in_time": local(assignment.window.starts_at, "%Y-%m-%d %H:%M:%S"),
                "out_time": local(assignment.window.ends_at, "%Y-%m-%d %H:%M:%S"),
            })

        original_draft, float_attendance_draft = map(attendance, worked.worked_intervals)
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
        submit(manager, attendance(worked.worked_intervals[-1]))
        _, corrected = read()
        self.assertEqual(corrected.approved_attendance_gaps, actual.approved_attendance_gaps)
        self.assertEqual(corrected.attendance.unresolved_exceptions, ())
