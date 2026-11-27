"""QuickBooks Time v1 conformance over HTTP with synthetic staff and appointments."""

import json
import socket
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest import TestCase
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from open_practice.quickbooks_time_adapter import QuickBooksRoster, QuickBooksTimeAdapter, QuickBooksWorker
from open_practice.quickbooks_time_client import QuickBooksTimeClient, QuickBooksTimeError
from open_practice.staff_schedule_provider import OpenShiftRequest
from open_practice.staffing_demand import calculate_demand, summarize_demand
from open_practice.staffing_variance import TimeWindow, compare_staffing, rank_replacements
from open_practice.tests.staff_provider_conformance import StaffingConformance, contract_validator, validate_provider_message
from open_practice.tests.test_quickbooks_time_live import run_t12
from open_practice.tests.test_staffing_demand import at, overlapping_procedures
from open_practice.tests.test_staffing_variance import PROCEDURE_WINDOW, REPORTING_PERIOD, rn_callout_scenario


def schedule_event(event_id, assignees, **fields):
    return {
        "id": event_id, "schedule_calendar_id": 20, "jobcode_id": 30,
        "start": at(9, 10).isoformat(), "end": at(9, 20).isoformat(), "all_day": False,
        "active": True, "draft": False, "assigned_user_ids": assignees, "title": "Workforce-only shift",
        **fields,
    }


def timesheet(ref, user_id, start, end):
    return {
        "id": ref, "user_id": user_id, "jobcode_id": 30, "type": "regular",
        "date": start.date().isoformat(), "start": start.isoformat(), "end": end.isoformat() if end else "",
        "on_the_clock": end is None, "locked": 1, "notes": "synthetic text to discard",
    }


def leave_entry(ref, status, **fields):
    return {
        "id": ref, "user_id": 14, "active": True, "status": status, "entry_method": "regular",
        "start_time": at(8, 50).isoformat(), "end_time": at(9, 5).isoformat(),
        "date": at(8, 50).date().isoformat(), **fields,
    }


class SyntheticTimeAPI:
    def __init__(self):
        self.resources = {
            "users": {user: {"id": user, "active": True, "approved_to": "2026-11-25",
                             "email": "synthetic-do-not-export@example.invalid",
                             "permissions": {"time_tracking": True}} for user in (11, 12, 13, 14)},
            "schedule_events": {101: schedule_event(101, [11]), 102: schedule_event(102, [12])},
            "timesheets": {},
            "current_user": {10: {"id": 10, "client_url": "synthetic-account", "permissions": {"admin": True}}},
            "schedule_calendars": {20: {"id": 20, "name": "T-12 test calendar"}},
            "jobcodes": {30: {"id": 30, "type": "regular", "active": True}},
            "time_off_request_entries": {},
        }
        self.bearer = "synthetic-intuit-token"
        self.calls = []
        self.posts = []
        self.post_mode = "normal"
        self.error = None
        self.repeat_first_page = False
        self.revoke_approval_on_timecard_read = None

    def handler(self):
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def send_json(self, status, data):
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def resource(self):
                url = urlsplit(self.path)
                filters = {key: values[0] for key, values in parse_qs(url.query).items()}
                api.calls.append((self.command, url.path, filters))
                if self.headers.get("Authorization") != f"Bearer {api.bearer}":
                    self.send_json(401, {"message": "invalid test token"})
                    return None, filters
                resource = url.path.removeprefix("/api/v1/")
                if resource not in api.resources:
                    self.send_json(404, {})
                    return None, filters
                return resource, filters

            def do_GET(self):
                resource, filters = self.resource()
                if not resource:
                    return
                if api.error:
                    if api.error == 302:
                        self.send_response(302)
                        self.send_header("Location", "/redirect-target")
                        self.end_headers()
                    else:
                        self.send_json(api.error, {"message": "provider response to discard"})
                    return
                if resource == "timesheets" and api.revoke_approval_on_timecard_read is not None:
                    api.resources["users"][api.revoke_approval_on_timecard_read]["approved_to"] = "2026-11-25"
                    api.revoke_approval_on_timecard_read = None
                records = list(api.resources[resource].items())
                if "ids" in filters:
                    ids = set(map(int, filters["ids"].split(",")))
                    records = [(key, row) for key, row in records if key in ids]
                if "user_ids" in filters:
                    ids = set(map(int, filters["user_ids"].split(",")))
                    records = [(key, row) for key, row in records if row.get("user_id") in ids]
                if resource == "timesheets":
                    records = [
                        (key, row) for key, row in records
                        if filters.get("start_date", "") <= row["date"] <= filters.get("end_date", "9999")
                    ]
                if resource == "schedule_events":
                    records = [
                        (key, row) for key, row in records
                        if str(row["schedule_calendar_id"]) == filters["schedule_calendar_ids"]
                    ]
                    if filters.get("active") == "yes":
                        records = [(key, row) for key, row in records if row.get("active", True)]
                    if filters.get("draft") == "no":
                        records = [(key, row) for key, row in records if not row.get("draft", False)]
                    if filters.get("draft") == "yes":
                        records = [(key, row) for key, row in records if row.get("draft", False)]
                    if "start" in filters:
                        records = [(key, row) for key, row in records if row["start"] >= filters["start"]]
                # Use small pages to exercise the v1 envelope and page cursor.
                page = 1 if api.repeat_first_page else int(filters.get("page", "1"))
                start = (page - 1) * 2
                page_rows = dict(records[start:start + 2])
                more = start + 2 < len(records)
                result_key = "users" if resource == "current_user" else resource
                result = {"results": {result_key: page_rows}}
                if resource == "schedule_events":
                    result["results"]["more"] = more
                else:
                    result["more"] = more
                self.send_json(200, result)

            def do_POST(self):
                resource, _ = self.resource()
                if resource not in ("schedule_events", "timesheets"):
                    return
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fields, = payload["data"]
                if resource == "timesheets":
                    timecard_id = 501 + len(api.resources[resource])
                    row = {**fields, "id": timecard_id, "date": fields["start"][:10], "on_the_clock": False}
                    api.resources[resource][timecard_id] = row
                    self.send_json(200, {"results": {resource: {"1": {**row, "_status_code": 200}}}})
                    return
                api.posts.append(payload)
                event_id = 900 + len(api.posts)
                if api.post_mode == "rejected":
                    self.send_json(200, {"results": {"schedule_events": {"1": {"_status_code": 403}}}})
                    return
                row = {**fields, "id": event_id, "unassigned": not fields["assigned_user_ids"]}
                if row["unassigned"]:
                    del row["assigned_user_ids"]
                if api.post_mode != "drop_before_commit":
                    api.resources[resource][event_id] = row
                if api.post_mode.startswith("drop_"):
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.close_connection = True
                    return
                self.send_json(200, {
                    "results": {"schedule_events": {"1": {**row, "_status_code": 200}}},
                })

            def do_PUT(self):
                resource, _ = self.resource()
                if resource not in ("schedule_events", "users"):
                    return
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fields, = payload["data"]
                row = api.resources[resource][fields["id"]]
                row.update(fields)
                if "assigned_user_ids" in fields:
                    row["unassigned"] = not fields["assigned_user_ids"]
                self.send_json(200, {"results": {resource: {"1": {**row, "_status_code": 200}}}})

        return Handler


class QuickBooksTimeConformanceTest(TestCase):
    def setUp(self):
        self.api = SyntheticTimeAPI()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.api.handler())
        self.addCleanup(self.server.server_close)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.shutdown)
        self.client = QuickBooksTimeClient(
            lambda: self.api.bearer, base_url=f"http://127.0.0.1:{self.server.server_port}",
        )
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.history_path = Path(temporary.name) / "roster.sqlite3"
        self.roster = QuickBooksRoster(
            "synthetic-account", "synthetic-t26", "Denver", 20, REPORTING_PERIOD, ZoneInfo("America/Denver"),
        )
        self.workers = {
            11: QuickBooksWorker("assigned-rn-1", "RN", "procedure-qualified", "independent", None),
            12: QuickBooksWorker("assigned-rn-2", "RN", "procedure-qualified", "independent", None),
            13: QuickBooksWorker("float-rn", "RN", "procedure-qualified", "independent", date(2026, 11, 26)),
            14: QuickBooksWorker("unqualified-rn", "RN", "triage-qualified", "independent", None),
        }
        self.jobcodes = {30: ("RN", "procedure-qualified"), 31: ("RN", "triage-qualified")}
        self.adapter = self.make_adapter()
        self.check = StaffingConformance(self, self.roster.series)

    def make_adapter(self, roster=None):
        return QuickBooksTimeAdapter(
            self.client, roster or self.roster, workers=self.workers, jobcodes=self.jobcodes,
            history_path=self.history_path,
        )

    def request(self):
        return OpenShiftRequest(uuid4().hex, "Denver", "RN", "procedure-qualified", PROCEDURE_WINDOW, 1)

    def test_live_t12_runner_rehearses_provider_writes_and_readbacks(self):
        day = datetime.now(timezone.utc).date() - timedelta(days=1)
        self.api.resources["schedule_events"].clear()
        for user_id in (11, 12, 13):
            self.api.resources["users"][user_id]["approved_to"] = (day - timedelta(days=1)).isoformat()
        config = {"account": "synthetic-account", "calendar_id": 20, "jobcode_id": 30,
                  "user_ids": [11, 12, 13], "date": day.isoformat(), "time_zone": "UTC"}
        history = self.history_path.with_name("t12-rehearsal.sqlite3")
        with self.assertRaisesRegex(QuickBooksTimeError, "named test account"):
            run_t12(self, self.client, {**config, "account": "different-account"}, history)
        existing = datetime.combine(day, datetime.min.time(), timezone.utc)
        self.api.resources["timesheets"][799] = timesheet(799, 11, existing, existing + timedelta(minutes=10))
        with self.assertRaisesRegex(QuickBooksTimeError, "existing timecards"):
            run_t12(self, self.client, config, history)
        del self.api.resources["timesheets"][799]
        self.assertTrue(all(method == "GET" for method, _, _ in self.api.calls))
        with redirect_stdout(StringIO()) as progress:
            run_t12(self, self.client, config, history)
        self.assertIn("posted and assigned draft both stayed pending", progress.getvalue())
        self.assertIn("approved 09:12 arrival left two RN-minutes short", progress.getvalue())


    def test_rn_callout_and_approved_attendance_match_shared_t26(self):
        demand = calculate_demand(overlapping_procedures())
        summary, = summarize_demand(demand, REPORTING_PERIOD.starts_at, REPORTING_PERIOD.ends_at)
        self.assertEqual((summary.peak_headcount, summary.staff_minutes), (2, 20))
        self.assertEqual((demand[0].starts_at, demand[0].ends_at), (at(9, 10), at(9, 20)))
        published, _, _, expected_work, candidates = rn_callout_scenario()

        def read():
            evidence = self.check.read(self.adapter)
            return evidence, compare_staffing(
                demand, evidence.published, period=REPORTING_PERIOD,
                revisions=evidence.revisions, attendance=evidence.attendance,
            )

        _, baseline = read()
        self.assertEqual(baseline.published.assignments, published.assignments)
        self.assertEqual(baseline.published_gaps, ())

        shifts = self.api.resources["schedule_events"]
        shifts[102]["active"] = False
        _, callout = read()
        gap, = callout.revised_gaps
        self.assertEqual((gap.starts_at, gap.ends_at, gap.headcount, gap.staff_minutes, gap.booking_ids),
                         (at(9, 10), at(9, 20), 1, 10, ("procedure-1", "procedure-2")))
        self.assertEqual(rank_replacements(gap, candidates, callout.revised), [candidates[1]])

        request = self.request()
        self.adapter.post_open_shift(request)
        self.adapter = self.make_adapter()
        self.adapter.post_open_shift(request)
        payload, = self.api.posts
        self.assertEqual(payload["team_events"], "base")
        draft, = payload["data"]
        self.assertEqual(draft, {
            "schedule_calendar_id": 20, "jobcode_id": 30,
            "start": PROCEDURE_WINDOW.starts_at.isoformat(), "end": PROCEDURE_WINDOW.ends_at.isoformat(),
            "all_day": False, "draft": True, "active": True, "assigned_user_ids": "",
            "timezone": "America/Denver", "title": "openPractice coverage " + request.correlation_id,
        })
        with self.assertRaisesRegex(QuickBooksTimeError, "different requirements"):
            self.adapter.post_open_shift(replace(request, headcount=2))
        evidence, pending = read()
        self.check.expect_open_shift(evidence, request, "pending")
        self.assertEqual(pending.revised_gaps, (gap,))

        shifts[901].update(assigned_user_ids="13", unassigned=False)
        evidence, pending = read()
        self.check.expect_open_shift(evidence, request, "pending")
        self.assertEqual(pending.revised_gaps, (gap,))

        shifts[901].update(draft=False, assigned_user_ids="14", unassigned=False)
        evidence, pending = read()
        self.check.expect_open_shift(evidence, request, "pending")
        self.assertEqual(pending.revised_gaps, (gap,))

        shifts[901].update(assigned_user_ids="13", unassigned=False)
        evidence, confirmed = read()
        self.check.expect_open_shift(evidence, request, "confirmed")
        self.assertEqual(confirmed.revised_gaps, ())
        self.assertEqual(confirmed.published, baseline.published)
        self.assertEqual(len(confirmed.revisions), 2)

        self.api.resources["timesheets"].update(
            {501: timesheet(501, 11, at(9, 10), at(9, 20)), 502: timesheet(502, 13, at(9, 12), at(9, 20))},
        )
        raw = self.adapter.get_clock_events(REPORTING_PERIOD)
        validate_provider_message(self.check.validator, raw)
        self.assertEqual(len(raw["data"]["records"]), 4)
        _, before_approval = read()
        self.assertIsNone(before_approval.attendance)  # Locked timecards alone do not establish approval.

        users = self.api.resources["users"]
        users[11]["approved_to"] = "2026-11-26"
        _, partial = read()
        self.assertEqual(partial.attendance.worked_intervals, expected_work.worked_intervals[:1])
        self.assertEqual(set(partial.attendance.unresolved_exceptions), {"schedule-event/901/user/13", "timesheet/502"})

        users[13]["approved_to"] = "2026-11-26"
        evidence, after_approval = read()
        self.check.expect_open_shift(evidence, request, "confirmed")
        self.assertEqual(after_approval.attendance.worked_intervals, expected_work.worked_intervals)
        self.assertEqual(after_approval.attendance.unresolved_exceptions, ())
        shortage, = after_approval.approved_attendance_gaps
        self.assertEqual((shortage.starts_at, shortage.ends_at, shortage.headcount, shortage.staff_minutes),
                         (at(9, 10), at(9, 12), 1, 2))
        self.assertEqual(after_approval.published, baseline.published)

        users[13]["approved_to"] = "2026-11-25"
        _, withdrawn = read()
        self.assertEqual(withdrawn.attendance.worked_intervals, expected_work.worked_intervals[:1])
        self.assertEqual(withdrawn.revised, after_approval.revised)

    def test_approval_changed_during_the_timecard_read_requires_a_fresh_read(self):
        before = self.check.read(self.adapter)
        self.api.resources["users"][11]["approved_to"] = "2026-11-26"
        self.api.resources["timesheets"][501] = timesheet(501, 11, at(9, 10), at(9, 20))
        self.api.revoke_approval_on_timecard_read = 11
        with self.assertRaisesRegex(QuickBooksTimeError, "approval changed during reconciliation"):
            self.check.read(self.adapter)
        after = self.check.read(self.adapter)
        self.assertIsNone(after.attendance)
        self.assertEqual(after.published, before.published)
        self.assertEqual(after.revisions, ())


    def test_workers_leave_and_clock_evidence_match_canonical_feeds(self):
        validator = contract_validator()
        workers = self.adapter.get_workers(REPORTING_PERIOD)
        validate_provider_message(validator, workers)
        self.assertEqual([worker["staff_id"] for worker in workers["data"]["records"]],
                         ["assigned-rn-1", "assigned-rn-2", "float-rn", "unqualified-rn"])
        self.assertNotIn("email", json.dumps(workers))
        self.assertIn(("GET", "/api/v1/users", {
            "ids": "11,12,13,14", "active": "both", "supplemental_data": "no", "page": "2", "limit": "100",
        }), self.api.calls)

        self.api.resources["time_off_request_entries"].update({
            700: leave_entry(700, "approved"), 701: leave_entry(701, "pending"),
            702: leave_entry(702, "approved", start_time=at(10, 0).isoformat(), end_time=at(10, 10).isoformat()),
        })
        leave = self.adapter.get_leave(REPORTING_PERIOD)
        validate_provider_message(validator, leave)
        item, = leave["data"]["approved"]
        self.assertEqual(item, {
            "reference": "time-off-entry/700", "staff_id": "unqualified-rn",
            "starts_at": at(8, 50).astimezone(PROCEDURE_WINDOW.starts_at.tzinfo).isoformat(),
            "ends_at": at(9, 5).astimezone(PROCEDURE_WINDOW.starts_at.tzinfo).isoformat(),
        })

        rows = self.api.resources["timesheets"]
        rows[503] = timesheet(503, 13, at(9, 12), None)
        rows[504] = timesheet(504, 11, at(8, 50), at(9, 5))
        rows[505] = timesheet(505, 12, at(9, 15), at(9, 30))
        rows[506] = {**timesheet(506, 14, at(9, 10), at(9, 20)), "type": "manual", "start": "", "end": ""}
        clock = self.adapter.get_clock_events(REPORTING_PERIOD)
        validate_provider_message(validator, clock)
        self.assertEqual([(row["reference"], row["direction"]) for row in clock["data"]["records"]],
                         [("timesheet/504/out", "out"), ("timesheet/503/in", "in"), ("timesheet/505/in", "in")])
        self.assertNotIn("notes", json.dumps(clock))

    def test_recovers_ambiguous_creation_without_posting_twice(self):
        request = self.request()
        self.api.post_mode = "drop_after_commit"
        with self.assertRaisesRegex(QuickBooksTimeError, "Could not reach"):
            self.adapter.post_open_shift(request)
        new_process = self.make_adapter()
        new_process.post_open_shift(request)
        new_process.post_open_shift(request)
        self.assertEqual(len(self.api.posts), 1)
        self.assertEqual(len(self.api.resources["schedule_events"]), 3)

    def test_open_shift_counts_distinct_qualified_team_members(self):
        self.api.resources["schedule_events"].clear()
        self.check.read(self.adapter)
        request = replace(self.request(), headcount=2)
        self.adapter.post_open_shift(request)
        shift = self.api.resources["schedule_events"][901]

        shift.update(draft=False, unassigned=False, assigned_user_ids="13,13,14")
        self.check.expect_open_shift(self.check.read(self.adapter), request, "pending")

        shift["assigned_user_ids"] = "12,13,13,14"
        self.check.expect_open_shift(self.check.read(self.adapter), request, "confirmed")


    def test_ambiguous_creation_without_provider_evidence_is_never_reposted(self):
        request = self.request()
        self.api.post_mode = "drop_before_commit"
        with self.assertRaises(QuickBooksTimeError):
            self.adapter.post_open_shift(request)
        self.api.post_mode = "normal"
        with self.assertRaisesRegex(QuickBooksTimeError, "awaiting provider readback"):
            self.make_adapter().post_open_shift(request)
        self.assertEqual(len(self.api.posts), 1)

    def test_explicitly_rejected_creation_can_retry_after_permissions_change(self):
        request = self.request()
        self.api.post_mode = "rejected"
        with self.assertRaisesRegex(QuickBooksTimeError, "rejected the draft event with status 403"):
            self.adapter.post_open_shift(request)
        self.api.post_mode = "normal"
        self.make_adapter().post_open_shift(request)
        self.assertEqual(len(self.api.posts), 2)
        self.assertEqual(len(self.api.resources["schedule_events"]), 3)


    def test_unavailable_feeds_fail_and_redirects_do_not_forward_credentials(self):
        for status in (403, 302, 503):
            with self.subTest(status=status):
                self.api.error = status
                with self.assertRaisesRegex(QuickBooksTimeError, f"HTTP {status}"):
                    self.adapter.get_workers(REPORTING_PERIOD)
        self.assertFalse(any(path == "/redirect-target" for _, path, _ in self.api.calls))
        self.api.error = None
        self.api.repeat_first_page = True
        with self.assertRaisesRegex(QuickBooksTimeError, "inconsistent users pages"):
            self.adapter.get_workers(REPORTING_PERIOD)
        self.api.repeat_first_page = False
        self.api.resources["time_off_request_entries"][701] = leave_entry(701, "approved", entry_method="manual")
        with self.assertRaisesRegex(QuickBooksTimeError, "date-only"):
            self.adapter.get_leave(REPORTING_PERIOD)

    def test_history_is_scoped_and_preserves_shifts_crossing_the_query_boundary(self):
        self.api.resources["schedule_events"] = {
            101: schedule_event(101, [11], start=at(8, 40).isoformat(), end=at(9, 5).isoformat()),
            102: schedule_event(102, [12], start=at(9, 30).isoformat(), end=at(10, 0).isoformat()),
            103: schedule_event(103, [999], schedule_calendar_id=21),
        }
        legacy_row = self.api.resources["schedule_events"][101]
        for field in ("active", "draft", "all_day", "assigned_user_ids"):
            del legacy_row[field]
        legacy_row.update(unassigned=False, user_id=11)
        evidence = self.check.read(self.adapter)
        assignment, = evidence.published.assignments
        self.assertEqual((assignment.staff_id, assignment.window.starts_at, assignment.window.ends_at),
                         ("assigned-rn-1", at(8, 40), at(9, 5)))
        with self.assertRaisesRegex(ValueError, "separate history"):
            self.make_adapter(replace(self.roster, connection="other-account"))
        with self.assertRaisesRegex(ValueError, "outside"):
            self.adapter.get_staffing("other-series")
