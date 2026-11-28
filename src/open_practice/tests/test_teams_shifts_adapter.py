"""T-13 against a loopback fixture with the Microsoft Graph v1.0 wire shape."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest import TestCase
from urllib.parse import urlsplit
from uuid import uuid4

from open_practice.staff_schedule_provider import OpenShiftRequest
from open_practice.staffing_demand import calculate_demand, summarize_demand
from open_practice.staffing_variance import TimeWindow, compare_staffing
from open_practice.teams_shifts_adapter import TeamsShiftsAdapter, TeamsShiftsRoster, TeamsShiftsWorker
from open_practice.teams_shifts_client import TeamsShiftsClient, TeamsShiftsError
from open_practice.tests.staff_provider_conformance import StaffingConformance, validate_provider_message
from open_practice.tests.test_staffing_demand import at, overlapping_procedures


TEAM = "11111111-0000-4000-8000-000000000013"
GROUP = "TAG-denver-procedure"
BASE_PATH = f"/v1.0/teams/{TEAM}/schedule"
WORKERS = {
    "11111111-0000-4000-8000-000000000001": TeamsShiftsWorker("assigned-rn-1", "RN", "procedure-qualified"),
    "11111111-0000-4000-8000-000000000002": TeamsShiftsWorker("assigned-rn-2", "RN", "procedure-qualified"),
    "11111111-0000-4000-8000-000000000003": TeamsShiftsWorker("float-rn", "RN", "procedure-qualified"),
}
ORIGINAL, CALLOUT, FLOAT = WORKERS
PERIOD = TimeWindow(at(9, 0), at(9, 30))
PROCEDURE = TimeWindow(at(9, 10), at(9, 20))


def shift(ref, worker, *, draft=False):
    interval = {"startDateTime": at(9, 10).isoformat(), "endDateTime": at(9, 20).isoformat()}
    return {
        "id": ref, "schedulingGroupId": GROUP, "userId": worker,
        "draftShift": interval if draft else None, "sharedShift": None if draft else interval,
    }


def card(ref, worker, start, *, confirmed="none", breaks=()):
    return {
        "id": ref, "userId": worker, "state": "clockedOut", "confirmedBy": confirmed,
        "clockInEvent": {"dateTime": start.isoformat()},
        "clockOutEvent": {"dateTime": at(9, 20).isoformat()},
        "breaks": list(breaks),
    }


class SyntheticGraph:
    def __init__(self):
        self.shifts = {
            "first": shift("first", ORIGINAL),
            "callout": shift("callout", CALLOUT),
        }
        self.open_shifts = {}
        self.claims = {}
        self.cards = {}
        self.posts = []
        self.queries = []
        self.drop_create_reply = False

    def handler(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, status, data):
                content = json.dumps(data).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def do_GET(self):
                target = urlsplit(self.path)
                collections = {
                    f"{BASE_PATH}/shifts": fixture.shifts,
                    f"{BASE_PATH}/openShifts": fixture.open_shifts,
                    f"{BASE_PATH}/openShiftChangeRequests": fixture.claims,
                    f"{BASE_PATH}/timeCards": fixture.cards,
                }
                if self.headers.get("Authorization") != "Bearer TEST_TEAMS_SHIFTS":
                    return self.reply(401, {})
                if target.path not in collections:
                    return self.reply(404, {})
                fixture.queries.append(self.path)
                rows = list(collections[target.path].values())
                if target.path.endswith("/shifts") and len(rows) > 1:
                    if not target.query:
                        return self.reply(200, {
                            "value": rows[:1],
                            "@odata.nextLink": f"http://127.0.0.1:{self.server.server_port}{target.path}?$skiptoken=next",
                        })
                    rows = rows[1:]
                self.reply(200, {"value": rows})

            def do_POST(self):
                if self.headers.get("Authorization") != "Bearer TEST_TEAMS_SHIFTS":
                    return self.reply(401, {})
                if self.path != f"{BASE_PATH}/openShifts":
                    return self.reply(404, {})
                values = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.posts.append(values)
                ref = "open-" + str(len(fixture.posts))
                row = {"id": ref, "sharedOpenShift": None, **values}
                fixture.open_shifts[ref] = row
                if fixture.drop_create_reply:
                    self.close_connection = True
                    return
                self.reply(200, row)

        return Handler


class TeamsShiftsConformanceTest(TestCase):
    def setUp(self):
        self.fixture = SyntheticGraph()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.fixture.handler())
        self.addCleanup(self.server.server_close)
        thread = Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.temp = self.enterContext(TemporaryDirectory())
        self.roster = TeamsShiftsRoster("T13", "Denver", GROUP, PERIOD)
        self.client = TeamsShiftsClient(
            TEAM, lambda: "TEST_TEAMS_SHIFTS", origin=f"http://127.0.0.1:{self.server.server_port}",
        )
        self.adapter = self.make_adapter()
        self.checks = StaffingConformance(self, self.roster.series)
        self.demand = calculate_demand(overlapping_procedures())

    def make_adapter(self):
        return TeamsShiftsAdapter(
            self.client, self.roster, workers=WORKERS, history_path=Path(self.temp) / "roster.sqlite3",
        )

    def read(self):
        evidence = self.checks.read(self.adapter)
        return evidence, compare_staffing(
            self.demand, evidence.published, period=PERIOD,
            revisions=evidence.revisions, attendance=evidence.attendance,
        )

    def request(self):
        return OpenShiftRequest(uuid4().hex, "Denver", "RN", "procedure-qualified", PROCEDURE, 1)

    def test_t13_published_float_and_manager_confirmed_0912_arrival(self):
        summary, = summarize_demand(self.demand, PERIOD.starts_at, PERIOD.ends_at)
        self.assertEqual((summary.peak_headcount, summary.staff_minutes), (2, 20))
        _, baseline = self.read()
        self.assertEqual(baseline.published_gaps, ())
        self.assertCountEqual([row.staff_id for row in baseline.published.assignments],
                              ["assigned-rn-1", "assigned-rn-2"])
        self.assertTrue(any("$skiptoken" in query for query in self.fixture.queries))

        del self.fixture.shifts["callout"]
        copied = shift("copied-without-assignee", FLOAT)
        del copied["userId"]
        self.fixture.shifts[copied["id"]] = copied
        _, after_callout = self.read()
        shortage, = after_callout.revised_gaps
        self.assertEqual(
            (shortage.starts_at, shortage.ends_at, shortage.headcount, shortage.staff_minutes, shortage.booking_ids),
            (at(9, 10), at(9, 20), 1, 10, ("procedure-1", "procedure-2")),
        )

        request = self.request()
        self.adapter.post_open_shift(request)
        self.make_adapter().post_open_shift(request)
        self.assertEqual(len(self.fixture.posts), 1)
        ref, = self.fixture.open_shifts
        open_shift = self.fixture.open_shifts[ref]
        posted = open_shift["draftOpenShift"]
        self.assertEqual(posted["openSlotCount"], 1)
        self.assertEqual(posted["notes"], "openPractice coverage " + request.correlation_id)
        self.assertNotIn("procedure-1", json.dumps(self.fixture.posts))
        evidence, draft = self.read()
        self.checks.expect_open_shift(evidence, request, "pending")
        self.assertEqual(draft.revised_gaps, (shortage,))

        open_shift["sharedOpenShift"], open_shift["draftOpenShift"] = posted, None
        self.fixture.claims["claim"] = {
            "id": "claim", "openShiftId": ref, "senderUserId": FLOAT, "state": "approved",
        }
        self.fixture.shifts["float"] = shift("float", FLOAT, draft=True)
        evidence, assigned_draft = self.read()
        self.checks.expect_open_shift(evidence, request, "pending")
        self.assertEqual(assigned_draft.revised_gaps, (shortage,))

        self.fixture.shifts["float"] = shift("float", FLOAT)
        self.adapter = self.make_adapter()
        evidence, published = self.read()
        self.checks.expect_open_shift(evidence, request, "confirmed")
        self.assertEqual(published.revised_gaps, ())
        self.assertEqual(published.published, baseline.published)
        self.assertCountEqual([row.staff_id for row in evidence.current.assignments],
                              ["assigned-rn-1", "float-rn"])

        self.fixture.cards.update({
            "first-card": card("first-card", ORIGINAL, at(9, 10)),
            "float-card": card("float-card", FLOAT, at(9, 12), confirmed="user"),
        })
        clock = self.adapter.get_clock_events(PERIOD)
        validate_provider_message(self.checks.validator, clock)
        self.assertIn({
            "reference": "timeCard/float-card/in", "staff_id": "float-rn", "direction": "in",
            "at": at(9, 12).astimezone(PERIOD.starts_at.tzinfo).isoformat(),
        }, clock["data"]["records"])
        evidence, employee_confirmed = self.read()
        self.assertIsNone(evidence.attendance)
        self.assertIsNone(employee_confirmed.approved_attendance_gaps)

        self.fixture.cards["first-card"]["confirmedBy"] = "manager"
        evidence, partially_approved = self.read()
        self.assertEqual([row.staff_id for row in evidence.attendance.worked_intervals], ["assigned-rn-1"])
        self.assertEqual(evidence.attendance.unresolved_exceptions, ("timeCard/float-card",))
        self.assertEqual(partially_approved.approved_attendance_gaps, (shortage,))

        self.fixture.cards["float-card"]["confirmedBy"] = "manager"
        evidence, approved = self.read()
        actual, = approved.approved_attendance_gaps
        self.assertEqual((actual.starts_at, actual.ends_at, actual.headcount, actual.staff_minutes),
                         (at(9, 10), at(9, 12), 1, 2))
        float_work, = [row.window for row in evidence.attendance.worked_intervals if row.staff_id == "float-rn"]
        self.assertEqual(float_work, TimeWindow(at(9, 12), at(9, 20)))
        self.assertEqual(evidence.attendance.unresolved_exceptions, ())
        self.assertEqual(approved.revised_gaps, ())
        self.assertEqual(approved.published, baseline.published)

    def test_open_shift_recovers_a_lost_post_acknowledgment(self):
        self.read()
        request = self.request()
        self.fixture.drop_create_reply = True
        with self.assertRaisesRegex(TeamsShiftsError, "Could not reach"):
            self.adapter.post_open_shift(request)
        self.make_adapter().post_open_shift(request)
        self.assertEqual(len(self.fixture.posts), 1)
        evidence, _ = self.read()
        self.checks.expect_open_shift(evidence, request, "pending")

    def test_manager_confirmed_break_leaves_a_coverage_gap(self):
        self.read()
        clock_break = {"start": {"dateTime": at(9, 14).isoformat()}, "end": {"dateTime": at(9, 16).isoformat()}}
        self.fixture.cards["first-card"] = card(
            "first-card", ORIGINAL, at(9, 10), confirmed="manager", breaks=(clock_break,),
        )
        self.fixture.cards["callout-card"] = card("callout-card", CALLOUT, at(9, 10), confirmed="manager")
        _, result = self.read()
        gap, = result.approved_attendance_gaps
        self.assertEqual((gap.starts_at, gap.ends_at, gap.staff_minutes), (at(9, 14), at(9, 16), 2))

    def test_one_published_assignment_cannot_confirm_two_claimed_requests(self):
        self.read()
        requests = (self.request(), self.request())
        for request in requests:
            self.adapter.post_open_shift(request)
        for ref in self.fixture.open_shifts:
            self.fixture.claims[ref] = {
                "id": ref, "openShiftId": ref, "senderUserId": FLOAT, "state": "approved",
            }
        del self.fixture.shifts["callout"]
        self.fixture.shifts["float"] = shift("float", FLOAT)
        evidence, _ = self.read()
        for request in requests:
            self.checks.expect_open_shift(evidence, request, "pending")
