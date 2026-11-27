"""T-26 rehearsal over Frappe REST and opt-in acceptance against a test site."""

import json
import os
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest import TestCase, skipUnless
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

from open_practice.openhr_adapter import (
    ATTENDANCE, REQUEST, ROSTER, FrappeClient, FrappeHTTPError, OpenHRAdapter, OpenHRError, OpenShiftRequest,
)
from open_practice.staff_schedule_provider import assignment_fields
from open_practice.staffing_demand import calculate_demand, summarize_demand
from open_practice.staffing_variance import TimeWindow, compare_staffing, rank_replacements
from open_practice.tests.staff_provider_conformance import StaffingConformance
from open_practice.tests.test_staffing_demand import at, overlapping_procedures
from open_practice.tests.test_staffing_variance import REPORTING_PERIOD, rn_callout_scenario


def submit(manager, document):
    return manager.request(
        "/api/method/frappe.client.submit", method="POST", values={"doc": document}, response_key="message",
    )


def run_t26(test, service, manager):
    adapter = OpenHRAdapter(service)
    series = f"synthetic-t26-{uuid4().hex}"
    conformance = StaffingConformance(test, series)
    demand = calculate_demand(overlapping_procedures())
    interval, = demand
    test.assertEqual((interval.starts_at, interval.ends_at), (at(9, 10), at(9, 20)))
    summary, = summarize_demand(demand, REPORTING_PERIOD.starts_at, REPORTING_PERIOD.ends_at)
    test.assertEqual((summary.peak_headcount, summary.staff_minutes), (2, 20))

    published, callout, revised, worked, candidates = rn_callout_scenario()

    def roster_document(snapshot, sequence, request=None):
        rows = [assignment_fields(assignment) for assignment in snapshot.assignments]
        if request:
            rows[-1]["coverage_request"] = request.correlation_id
        return manager.create(ROSTER, {
            "roster_series": series,
            "sequence_number": sequence,
            "recorded_at_iso": snapshot.recorded_at.isoformat(),
            "reason": snapshot.reason,
            "assignments_json": json.dumps(rows),
        })

    def read():
        evidence = conformance.read(adapter)
        return evidence, compare_staffing(
            demand, evidence.published, period=REPORTING_PERIOD, revisions=evidence.revisions,
            attendance=evidence.attendance,
        )

    submit(manager, roster_document(published, 1))
    submit(manager, roster_document(callout, 2))
    evidence, after_callout = read()
    baseline = evidence.published
    test.assertEqual(baseline.assignments, published.assignments)
    test.assertEqual(after_callout.published_gaps, ())
    gap, = after_callout.revised_gaps
    test.assertEqual(
        (gap.starts_at, gap.ends_at, gap.headcount, gap.staff_minutes, gap.booking_ids),
        (at(9, 10), at(9, 20), 1, 10, ("procedure-1", "procedure-2")),
    )
    test.assertEqual(rank_replacements(gap, candidates, after_callout.revised), [candidates[1]])

    request = OpenShiftRequest(
        uuid4().hex, gap.location, gap.role, gap.qualification, TimeWindow(gap.starts_at, gap.ends_at), gap.headcount,
    )
    adapter.post_open_shift(request)
    adapter.post_open_shift(request)
    posted = service.get(REQUEST, request.correlation_id)
    test.assertEqual(posted["name"], request.correlation_id)
    with test.assertRaises(OpenHRError):
        adapter.post_open_shift(replace(request, headcount=2))
    evidence, pending = read()
    conformance.expect_open_shift(evidence, request, "pending")
    test.assertEqual(pending.revised_gaps, (gap,))

    draft = roster_document(revised, 3, request)
    with test.assertRaises(FrappeHTTPError) as denied:
        submit(service, draft)
    test.assertEqual(denied.exception.status, 403)
    evidence, pending = read()
    conformance.expect_open_shift(evidence, request, "pending")
    test.assertEqual(pending.revised_gaps, (gap,))

    confirmed = submit(manager, draft)
    evidence, covered = read()
    conformance.expect_open_shift(evidence, request, "confirmed")
    test.assertEqual(covered.revised.reference, confirmed["name"])
    test.assertEqual(covered.revised_gaps, ())
    test.assertEqual(covered.published, baseline)
    test.assertIsNone(covered.approved_attendance_gaps)

    attendance = manager.create(ATTENDANCE, {
        "roster_revision": confirmed["name"],
        "sequence_number": 1,
        "worked_intervals_json": json.dumps([assignment_fields(interval) for interval in worked.worked_intervals]),
        "exceptions_json": json.dumps(["synthetic-exception-follow-up"]),
    })
    evidence, before_approval = read()
    test.assertIsNone(before_approval.approved_attendance_gaps)

    submit(manager, attendance)
    evidence, actual = read()
    test.assertEqual(evidence.attendance.worked_intervals, worked.worked_intervals)
    test.assertEqual(evidence.attendance.unresolved_exceptions, ("synthetic-exception-follow-up",))
    short, = actual.approved_attendance_gaps
    test.assertEqual((short.starts_at, short.ends_at, short.headcount, short.staff_minutes), (at(9, 10), at(9, 12), 1, 2))
    test.assertEqual((actual.revised_to_actual[0].staff_id, actual.revised_to_actual[0].staff_minutes), ("float-rn", -2))
    test.assertEqual(actual.revised_gaps, ())
    test.assertEqual(actual.published, baseline)


@dataclass
class TestResources:
    records: dict
    posted_requests: list

    def make_handler(self):
        store = self.records
        posted_requests = self.posted_requests

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def respond(self, value=None, status=200, key="data"):
                data = json.dumps({key: value}).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def actor(self):
                return {"token service:test": "service", "token manager:test": "manager"}.get(
                    self.headers.get("Authorization")
                )

            def do_GET(self):
                if self.actor() is None:
                    self.respond(status=401)
                    return
                url = urlsplit(self.path)
                _, _, _, doctype, *name = (unquote(part) for part in url.path.split("/"))
                records = store[doctype]
                if name:
                    document = records.get(name[0])
                    self.respond(document, 200 if document else 404)
                    return
                query = parse_qs(url.query)
                filters = json.loads(query["filters"][0])
                rows = [doc for doc in records.values() if all(doc.get(key) == value for key, _, value in filters)]
                rows.sort(key=lambda row: (row["sequence_number"], row["name"]))
                start, page_size = int(query["limit_start"][0]), int(query["limit_page_length"][0])
                self.respond([{"name": row["name"]} for row in rows[start:start + page_size]])

            def do_POST(self):
                actor = self.actor()
                if actor is None:
                    self.respond(status=401)
                    return
                values = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/api/method/frappe.client.submit":
                    if actor != "manager":
                        self.respond(status=403)
                        return
                    document = values["doc"]
                    persisted = store[document["doctype"]][document["name"]]
                    persisted["docstatus"] = 1
                    persisted["modified_by"] = actor
                    self.respond(persisted, key="message")
                    return
                doctype = unquote(self.path.rsplit("/", 1)[-1])
                if (doctype == REQUEST) != (actor == "service"):
                    self.respond(status=403)
                    return
                if doctype == REQUEST:
                    posted_requests.append(values)
                name = values.get("correlation_id") or uuid4().hex
                if name in store[doctype]:
                    self.respond(status=409)
                    return
                document = {**values, "name": name, "docstatus": 0, "doctype": doctype, "owner": actor}
                store[doctype][name] = document
                self.respond(document)

        return Handler


class OpenHRAdapterTest(TestCase):
    def test_t26_workflow_over_loopback_frappe_rest(self):
        resources = TestResources({doctype: {} for doctype in (ROSTER, ATTENDANCE, REQUEST)}, [])
        server = self.enterContext(ThreadingHTTPServer(("127.0.0.1", 0), resources.make_handler()))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}"
        run_t26(self, FrappeClient(url, "service", "test"), FrappeClient(url, "manager", "test"))
        posted, = resources.posted_requests
        self.assertEqual(set(posted), {
            "correlation_id", "location_ref", "staff_role", "qualification", "starts_at_iso", "ends_at_iso", "headcount",
        })


@skipUnless(os.environ.get("OPENHR_T26_URL"), "No openHR test site configured")
class OpenHRSiteAcceptanceTest(TestCase):
    def test_t26_against_openhr_test_site(self):
        url = os.environ["OPENHR_T26_URL"]
        service = FrappeClient(url, os.environ["OPENHR_T26_SERVICE_KEY"], os.environ["OPENHR_T26_SERVICE_SECRET"])
        manager = FrappeClient(url, os.environ["OPENHR_T26_MANAGER_KEY"], os.environ["OPENHR_T26_MANAGER_SECRET"])
        identity_path = "/api/method/frappe.auth.get_logged_user"
        service_user = service.request(identity_path, response_key="message")
        manager_user = manager.request(identity_path, response_key="message")
        self.assertNotIn(service_user, ("Guest", "Administrator", manager_user))
        self.assertNotIn(manager_user, ("Guest", "Administrator"))
        run_t26(self, service, manager)
