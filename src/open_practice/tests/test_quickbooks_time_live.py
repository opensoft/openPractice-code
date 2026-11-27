"""Opt-in T-12 acceptance on a QuickBooks Time v1 test tenant."""

import json
import os
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
from uuid import uuid4
from zoneinfo import ZoneInfo

from open_practice.quickbooks_time_adapter import QuickBooksRoster, QuickBooksTimeAdapter, QuickBooksWorker, _assignees, _window
from open_practice.quickbooks_time_client import QuickBooksTimeClient, QuickBooksTimeError
from open_practice.staff_schedule_provider import OpenShiftRequest
from open_practice.staffing_demand import calculate_demand, summarize_demand
from open_practice.staffing_variance import TimeWindow, compare_staffing
from open_practice.tests.staff_provider_conformance import StaffingConformance, validate_provider_message
from open_practice.tests.test_staffing_demand import overlapping_procedures


CONFIG_ENV = "OPENPRACTICE_T12_CONFIG"
TOKEN_ENV = "OPENPRACTICE_T12_TIME_TOKEN"
STAFF_LABELS = ("assigned-rn-1", "assigned-rn-2", "float-rn")


def _write_fixture(client, method, resource, fields, **options):
    response = client._request(resource, method=method, payload={"data": [fields], **options})
    try:
        row = response["results"][resource]["1"]
        status = row["_status_code"]
        if type(status) is not int:
            raise ValueError
        if status not in (200, 201):
            raise QuickBooksTimeError(f"QuickBooks Time rejected the synthetic {resource} write ({status}).")
        result = row["id"]
        if type(result) is not int or result < 1 or ("id" in fields and fields["id"] != result):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise QuickBooksTimeError(f"QuickBooks Time returned an invalid synthetic {resource} acknowledgment.") from None
    print(f"T-12 fixture {method} {resource} ID {result}", flush=True)
    return result


def _pending_timecards(client, approvals, end_date):
    cards = set()
    for user_id, approved_to in approvals.items():
        rows = client.rows(
            "timesheets", user_ids=user_id, start_date=(approved_to + timedelta(days=1)).isoformat(),
            end_date=end_date.isoformat(), on_the_clock="both", supplemental_data="no",
        )
        cards.update(rows)
    return cards


def _preflight(client, account, calendar_id, jobcode_id, users, date_of_test, window):
    current = client._rows(client._request("current_user"), "users")
    if len(current) != 1:
        raise QuickBooksTimeError("The test token did not identify one QuickBooks Time account.")
    actor, = current.values()
    if actor.get("client_url") != account or actor["id"] in users:
        raise QuickBooksTimeError("The token must belong to the named test account and a separate test manager.")
    rights = actor.get("permissions", {})
    can_schedule = rights.get("manage_company_schedules") is True or rights.get("manage_schedules") is True
    if rights.get("admin") is not True and not (
        can_schedule and rights.get("manage_timesheets") is True and rights.get("approve_timesheets") is True
    ):
        raise QuickBooksTimeError("The test token needs schedule, timecard, and approval access.")

    # The documented calendar response can omit the pagination flag when requested by ID.
    calendar = client._rows(client._request("schedule_calendars", query={"ids": calendar_id}), "schedule_calendars")
    jobcodes = client.rows("jobcodes", ids=jobcode_id, type="regular", active="yes")
    if calendar.keys() != {calendar_id} or jobcode_id not in jobcodes:
        raise QuickBooksTimeError("The test calendar or active workforce job code is unavailable.")

    selected = client.rows("users", ids=",".join(map(str, users)), active="yes", supplemental_data="no")
    if selected.keys() != set(users):
        raise QuickBooksTimeError("The test manager cannot read all three configured synthetic users.")
    approvals = {}
    for user_id in users:
        row = selected[user_id]
        email = row.get("email") or ""
        if row.get("mobile_number") or (email and not email.casefold().endswith((".invalid", ".test", ".example"))):
            raise QuickBooksTimeError("Use synthetic staff without deliverable email or phone destinations.")
        if row.get("active") is not True or row.get("permissions", {}).get("time_tracking") is not True:
            raise QuickBooksTimeError("All fixture users must be active and enabled for time tracking.")
        try:
            approved_to = date.fromisoformat(row["approved_to"])
        except (KeyError, TypeError, ValueError):
            raise QuickBooksTimeError("The test tenant must expose approval dates for the fixture users.") from None
        if approved_to >= date_of_test:
            raise QuickBooksTimeError("Choose a test date after the fixture users' existing approvals.")
        approvals[user_id] = approved_to

    events = client.rows(
        "schedule_events", schedule_calendar_ids=calendar_id, modified_since="1970-01-01T00:00:00+00:00",
        active="yes", draft="both", team_events="base", supplemental_data="no",
    )
    if any(_window(event).overlaps(window) for event in events.values()):
        raise QuickBooksTimeError("The test calendar has an existing shift in the requested window.")
    if _pending_timecards(client, approvals, date_of_test):
        raise QuickBooksTimeError("The fixture users have existing timecards that this approval could include.")
    return approvals


def run_t12(test, client, config, history_path):
    """Drive provider-only fixture actions and verify all readbacks through the adapter."""
    account = config["account"]
    calendar_id = config["calendar_id"]
    jobcode_id = config["jobcode_id"]
    users = tuple(config["user_ids"])
    if not isinstance(account, str) or not account.strip():
        raise ValueError("Configure the QuickBooks Time test account client_url.")
    if len(users) != 3 or len(set(users)) != 3 or any(
        type(value) is not int or value < 1 for value in (calendar_id, jobcode_id, *users)
    ):
        raise ValueError("Configure positive calendar/job-code IDs and three distinct synthetic user IDs.")
    zone = ZoneInfo(config["time_zone"])
    day = date.fromisoformat(config["date"])
    if day >= datetime.now(zone).date():
        raise ValueError("Choose a completed local test date.")
    opening, returning, closing = (datetime.combine(day, time(9, minute), zone) for minute in (10, 12, 20))
    period_start, period_end = (datetime.combine(day, time(9, minute), zone) for minute in (0, 30))
    period = TimeWindow(period_start, period_end)
    procedure = TimeWindow(opening, closing)
    approvals = _preflight(client, account, calendar_id, jobcode_id, users, day, period)
    print(f"T-12 preflight passed for calendar {calendar_id} on {day.isoformat()}", flush=True)

    run_id = uuid4().hex
    roster = QuickBooksRoster(account, f"t12-{run_id}", "T-12 synthetic site", calendar_id, period, zone)
    workers = {
        user_id: QuickBooksWorker(label, "RN", "procedure-qualified", "independent", None)
        for user_id, label in zip(users, STAFF_LABELS, strict=True)
    }
    adapter = QuickBooksTimeAdapter(
        client, roster, workers=workers, jobcodes={jobcode_id: ("RN", "procedure-qualified")}, history_path=history_path,
    )
    checks = StaffingConformance(test, roster.series)
    demand = calculate_demand([
        replace(booking, starts_at=start, location=roster.location)
        for booking, start in zip(overlapping_procedures(), (period_start, opening), strict=True)
    ])
    summary, = summarize_demand(demand, period.starts_at, period.ends_at)
    test.assertEqual((summary.peak_headcount, summary.staff_minutes), (2, 20))

    def read():
        evidence = checks.read(adapter)
        return evidence, compare_staffing(
            demand, evidence.published, period=period, revisions=evidence.revisions, attendance=evidence.attendance,
        )

    def update_shift(event_id, **fields):
        _write_fixture(client, "PUT", "schedule_events", {
            "id": event_id, "schedule_calendar_id": calendar_id, **fields,
        }, team_events="base")

    baseline_events = []
    for index, user_id in enumerate(users[:2], 1):
        event_id = client.create_schedule_event({
            "schedule_calendar_id": calendar_id, "jobcode_id": jobcode_id,
            "start": opening.isoformat(), "end": closing.isoformat(), "timezone": zone.key,
            "title": f"T12 synthetic baseline {index} {run_id}", "all_day": False,
            "draft": False, "active": True, "assigned_user_ids": [user_id],
        })["id"]
        baseline_events.append(event_id)
        print(f"T-12 baseline schedule event ID {event_id}", flush=True)
    _, baseline = read()
    test.assertCountEqual([item.staff_id for item in baseline.published.assignments], STAFF_LABELS[:2])
    test.assertEqual(baseline.published_gaps, ())

    update_shift(baseline_events[1], active=False)
    _, after_callout = read()
    gap, = after_callout.revised_gaps
    test.assertEqual(
        (gap.starts_at, gap.ends_at, gap.headcount, gap.staff_minutes, gap.booking_ids),
        (opening, closing, 1, 10, ("procedure-1", "procedure-2")),
    )

    request = OpenShiftRequest(uuid4().hex, roster.location, "RN", "procedure-qualified", procedure, 1)
    print(f"T-12 coverage correlation {request.correlation_id}", flush=True)
    adapter.post_open_shift(request)
    evidence, posted = read()
    checks.expect_open_shift(evidence, request, "pending")
    test.assertEqual(posted.revised_gaps, (gap,))

    def read_float_draft():
        events = client.rows(
            "schedule_events", schedule_calendar_ids=calendar_id, start=opening.isoformat(),
            draft="yes", active="yes", team_events="base", supplemental_data="no",
        )
        draft, = [row for row in events.values()
                  if row.get("title") == f"openPractice coverage {request.correlation_id}"]
        test.assertTrue(draft.get("draft", True))
        return draft

    draft = read_float_draft()
    float_event = draft["id"]
    test.assertEqual(_assignees(draft), [])
    update_shift(float_event, assigned_user_ids=[users[2]], draft=True)
    assigned_draft = read_float_draft()
    test.assertEqual((assigned_draft["id"], _assignees(assigned_draft)), (float_event, [users[2]]))
    evidence, drafted = read()
    checks.expect_open_shift(evidence, request, "pending")
    test.assertEqual(drafted.revised_gaps, (gap,))
    print("T-12 posted and assigned draft both stayed pending", flush=True)

    update_shift(float_event, draft=False)
    evidence, covered = read()
    checks.expect_open_shift(evidence, request, "confirmed")
    test.assertEqual(covered.revised_gaps, ())
    test.assertEqual(covered.published, baseline.published)
    print("T-12 published float closed the planned gap and preserved the baseline", flush=True)

    cards = {
        _write_fixture(client, "POST", "timesheets", {
            "user_id": user_id, "jobcode_id": jobcode_id, "type": "regular",
            "start": start.isoformat(), "end": closing.isoformat(), "notes": f"T12 synthetic {run_id}",
        })
        for user_id, start in ((users[0], opening), (users[2], returning))
    }
    clock = adapter.get_clock_events(period)
    validate_provider_message(checks.validator, clock)
    _, unapproved = read()
    test.assertIsNone(unapproved.attendance)

    approval_scope = {user_id: approvals[user_id] for user_id in (users[0], users[2])}
    if _pending_timecards(client, approval_scope, day) != cards:
        raise QuickBooksTimeError("Approval would include timecards outside this synthetic run.")
    for user_id in approval_scope:
        _write_fixture(client, "PUT", "users", {"id": user_id, "approved_to": day.isoformat()})

    evidence, approved = read()
    checks.expect_open_shift(evidence, request, "confirmed")
    test.assertEqual(approved.published, baseline.published)
    test.assertEqual(approved.revised_gaps, ())
    test.assertEqual(approved.attendance.unresolved_exceptions, ())
    actual, = approved.approved_attendance_gaps
    test.assertEqual((actual.starts_at, actual.ends_at, actual.headcount, actual.staff_minutes),
                     (opening, returning, 1, 2))
    float_attendance, = [item for item in approved.attendance.worked_intervals if item.staff_id == "float-rn"]
    test.assertEqual((float_attendance.window.starts_at, float_attendance.window.ends_at), (returning, closing))
    print("T-12 approved 09:12 arrival left two RN-minutes short", flush=True)


@skipUnless(os.environ.get(CONFIG_ENV) and os.environ.get(TOKEN_ENV), "No authenticated QuickBooks Time test tenant configured")
class QuickBooksTimeLiveTest(TestCase):
    def test_t12_live_pending_published_and_approved_attendance(self):
        with Path(os.environ[CONFIG_ENV]).open(encoding="utf-8") as source:
            config = json.load(source)
        client = QuickBooksTimeClient(lambda: os.environ[TOKEN_ENV])
        with TemporaryDirectory() as directory:
            run_t12(self, client, config, Path(directory) / "t12.sqlite3")
