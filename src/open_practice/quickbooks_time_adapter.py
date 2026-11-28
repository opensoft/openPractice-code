"""Normalize QuickBooks Time workforce records into the shared scheduling contract."""

import json
import re
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from open_practice.quickbooks_time_client import QuickBooksTimeError, QuickBooksTimeRejected
from open_practice.staff_schedule_provider import OpenShiftRequest, StaffScheduleEvidence, assignment_fields, provider_message
from open_practice.staffing_variance import ApprovedAttendance, RosterSnapshot, StaffingAssignment, TimeWindow


_EVENT_TITLE_PREFIX = "openPractice coverage "
_DAY = timedelta(days=1)
_LAST_INSTANT = timedelta(microseconds=1)


@dataclass(frozen=True)
class QuickBooksWorker:
    """A QuickBooks user joined to an authorized workforce qualification."""

    staff_id: str
    role: str
    qualification: str
    skill_level: str
    expires_on: date | None

    def __post_init__(self):
        fields = self.staff_id, self.role, self.qualification, self.skill_level
        if any(not isinstance(value, str) or not value.strip() for value in fields):
            raise ValueError("A worker mapping needs a staff ID, role, qualification and skill level.")
        if self.expires_on is not None and type(self.expires_on) is not date:
            raise ValueError("Use a date or an explicitly nonexpiring qualification.")


@dataclass(frozen=True)
class QuickBooksRoster:
    """One account, calendar, site and effective window for a roster series."""

    connection: str
    series: str
    location: str
    calendar_id: int
    window: TimeWindow
    zone: ZoneInfo

    def __post_init__(self):
        if any(not isinstance(value, str) or not value.strip() for value in (self.connection, self.series, self.location)):
            raise ValueError("A roster needs an account reference, series and site.")
        if type(self.calendar_id) is not int or self.calendar_id < 1:
            raise ValueError("A roster needs a positive QuickBooks calendar ID.")


def _csv(ids):
    return ",".join(str(value) for value in sorted(ids))


def _timestamp(value):
    instant = datetime.fromisoformat(value)
    if instant.utcoffset() is None:
        raise ValueError("QuickBooks Time timestamps need an offset.")
    return instant.astimezone(timezone.utc)


def _window(row):
    return TimeWindow(_timestamp(row["start"]), _timestamp(row["end"]))


def _assignees(event):
    # The v1 examples use both CSV and integer arrays. Older single-user reads use user_id.
    unassigned = event.get("unassigned")
    if unassigned is not None and type(unassigned) is not bool:
        raise ValueError("Invalid QuickBooks Time unassigned state.")
    value = event.get("assigned_user_ids")
    if value is None and unassigned is not None:
        value = [] if unassigned else [event["user_id"]]
    if isinstance(value, str):
        if value and not re.fullmatch(r"[0-9]+(?:,[0-9]+)*", value):
            raise ValueError("Invalid QuickBooks Time assignment IDs.")
        value = list(map(int, value.split(","))) if value else []
    if not isinstance(value, list) or any(type(user_id) is not int or user_id < 1 for user_id in value):
        raise ValueError("Invalid QuickBooks Time assignment IDs.")
    if unassigned and value:
        raise ValueError("Inconsistent QuickBooks Time assignment.")
    return sorted(set(value))


def _assignment(record):
    return StaffingAssignment(
        record["staff_id"], record["location"], record["role"], record["qualification"],
        TimeWindow(_timestamp(record["starts_at"]), _timestamp(record["ends_at"])),
    )


class QuickBooksTimeAdapter:
    """Read a configured site and post unassigned draft coverage events."""

    def __init__(self, client, roster: QuickBooksRoster, *, workers, jobcodes, history_path: str | Path):
        self._client = client
        self._roster = roster
        self._workers = dict(workers)
        self._jobcodes = dict(jobcodes)
        self._history_path = str(history_path)
        if self._history_path in ("", ":memory:"):
            raise ValueError("A QuickBooks roster requires a durable history file.")
        if (not self._workers or not self._jobcodes
                or any(type(key) is not int or key < 1 for key in (*self._workers, *self._jobcodes))
                or len({row.staff_id for row in self._workers.values()}) != len(self._workers)):
            raise ValueError("Configure distinct workforce IDs, QuickBooks user IDs and site job codes.")
        identity = json.dumps((
            roster.connection, roster.series, roster.location, roster.calendar_id, roster.zone.key,
            roster.window.starts_at.isoformat(), roster.window.ends_at.isoformat(),
        ))
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS scope (slot INTEGER PRIMARY KEY CHECK (slot = 1), identity TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO scope VALUES (1, ?)", (identity,))
            if db.execute("SELECT identity FROM scope WHERE slot = 1").fetchone()[0] != identity:
                raise ValueError("Use a separate history file for each QuickBooks roster scope.")
            db.execute(
                "CREATE TABLE IF NOT EXISTS roster_observation "
                "(sequence INTEGER PRIMARY KEY, observed_at TEXT NOT NULL, assignments TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS shift_request "
                "(correlation TEXT PRIMARY KEY, requirements TEXT NOT NULL, event_id INTEGER UNIQUE)"
            )

    @contextmanager
    def _transaction(self):
        try:
            with closing(sqlite3.connect(self._history_path, timeout=30)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        except sqlite3.Error:
            raise QuickBooksTimeError("The QuickBooks Time roster history is unavailable.") from None

    def _scope_data(self, window):
        return {
            "location": self._roster.location,
            "starts_at": window.starts_at.isoformat(),
            "ends_at": window.ends_at.isoformat(),
        }

    def _days(self, window):
        return (window.starts_at.astimezone(self._roster.zone).date(),
                (window.ends_at - _LAST_INSTANT).astimezone(self._roster.zone).date())

    def _users(self):
        rows = self._client.rows("users", ids=_csv(self._workers), active="both", supplemental_data="no")
        if rows.keys() != self._workers.keys() or any(type(row.get("active")) is not bool for row in rows.values()):
            raise QuickBooksTimeError("QuickBooks Time did not return the configured workforce scope.")
        return rows

    def get_workers(self, window: TimeWindow) -> dict:
        users = self._users()
        return provider_message("workers", {
            **self._scope_data(window),
            "records": [
                {
                    "staff_id": worker.staff_id,
                    "roles": [worker.role],
                    "skills": [{"code": worker.qualification, "level": worker.skill_level}],
                    "certifications": [{
                        "code": worker.qualification,
                        "expires_on": worker.expires_on.isoformat() if worker.expires_on else None,
                    }],
                }
                for user_id, worker in sorted(self._workers.items())
                if users[user_id]["active"]
            ],
        })

    def get_leave(self, window: TimeWindow) -> dict:
        rows = self._client.rows(
            "time_off_request_entries", user_ids=_csv(self._workers), status="approved", supplemental_data="no",
        )
        first, last = self._days(window)
        approved = []
        try:
            for ref, row in sorted(rows.items()):
                if row["user_id"] not in self._workers or row["status"] != "approved" or not row["active"]:
                    continue
                if row["entry_method"] == "manual":
                    if first <= date.fromisoformat(row["date"]) <= last:
                        raise QuickBooksTimeError("Approved date-only time off has no exact availability interval.")
                    continue
                if row["entry_method"] != "regular" or row["active"] is not True:
                    raise ValueError
                leave = TimeWindow(_timestamp(row["start_time"]), _timestamp(row["end_time"]))
                if leave.overlaps(window):
                    approved.append({
                        "reference": f"time-off-entry/{ref}", "staff_id": self._workers[row["user_id"]].staff_id,
                        "starts_at": leave.starts_at.isoformat(), "ends_at": leave.ends_at.isoformat(),
                    })
        except (KeyError, TypeError, ValueError):
            raise QuickBooksTimeError("QuickBooks Time returned invalid leave evidence.") from None
        return provider_message("leave", {**self._scope_data(window), "approved": approved})

    def _timesheets(self, window):
        first, last = self._days(window)
        return self._client.rows(
            "timesheets", user_ids=_csv(self._workers), jobcode_ids=_csv(self._jobcodes),
            jobcode_type="regular", on_the_clock="both", start_date=(first - 7 * _DAY).isoformat(),
            end_date=last.isoformat(), supplemental_data="no",
        )

    @staticmethod
    def _punches(row):
        start = _timestamp(row["start"])
        if row["on_the_clock"] is True and row["end"] == "":
            return start, None
        if row["on_the_clock"] is not False:
            raise ValueError("Inconsistent clocked-in state.")
        end = _timestamp(row["end"])
        if end <= start:
            raise ValueError("Invalid timecard interval.")
        return start, end

    def get_clock_events(self, window: TimeWindow) -> dict:
        events = []
        try:
            for ref, row in self._timesheets(window).items():
                if (row["user_id"] not in self._workers or row["jobcode_id"] not in self._jobcodes
                        or row["type"] == "manual"):
                    continue
                if row["type"] != "regular":
                    raise ValueError
                start, end = self._punches(row)
                for direction, instant in (("in", start), ("out", end)):
                    if instant is not None and window.starts_at <= instant < window.ends_at:
                        events.append({
                            "reference": f"timesheet/{ref}/{direction}", "staff_id": self._workers[row["user_id"]].staff_id,
                            "at": instant.isoformat(), "direction": direction,
                        })
        except (KeyError, TypeError, ValueError):
            raise QuickBooksTimeError("QuickBooks Time returned invalid clock evidence.") from None
        events.sort(key=lambda event: (event["at"], event["reference"]))
        return provider_message("clock_events", {**self._scope_data(window), "records": events})

    def _events(self, *, published_only=False):
        # The API's start/end filters omit shifts crossing a range boundary.
        return self._client.rows(
            "schedule_events", schedule_calendar_ids=self._roster.calendar_id,
            modified_since="1970-01-01T00:00:00+00:00", active="yes" if published_only else "both", active_users=-1,
            draft="no" if published_only else "both", team_events="base", supplemental_data="no",
        )

    def _staff_assignment(self, user_id, jobcode, window):
        worker = self._workers[user_id]
        if self._jobcodes[jobcode] != (worker.role, worker.qualification):
            return None
        if worker.expires_on and worker.expires_on < self._days(window)[1]:
            return None
        return StaffingAssignment(worker.staff_id, self._roster.location, worker.role, worker.qualification, window)

    def _published_assignments(self, events, users, requests):
        published = []
        for event_id, row in sorted(events.items()):
            if row["schedule_calendar_id"] != self._roster.calendar_id or row["jobcode_id"] not in self._jobcodes:
                continue
            active, draft = row.get("active", True), row.get("draft", False)
            if type(active) is not bool or type(draft) is not bool:
                raise ValueError
            if not active or draft:
                continue
            assignees = _assignees(row)
            if not assignees:
                continue
            window = _window(row)
            if not window.overlaps(self._roster.window):
                continue
            if row.get("all_day", False) is not False:
                raise ValueError("A mapped workforce shift needs exact times.")
            for user_id in assignees:
                if user_id not in users:
                    raise QuickBooksTimeError("A published QuickBooks Time shift has an unmapped worker.")
                if not users[user_id]["active"]:
                    continue
                assignment = self._staff_assignment(user_id, row["jobcode_id"], window)
                if assignment is not None:
                    published.append({
                        "event_id": event_id, "user_id": user_id, "correlation": requests.get(event_id),
                        "assignment": assignment_fields(assignment),
                    })
        return published

    def _observations(self, db, assignments):
        history = db.execute(
            "SELECT sequence, observed_at, assignments FROM roster_observation ORDER BY sequence"
        ).fetchall()
        encoded = json.dumps(assignments, sort_keys=True)
        if not history or history[-1][2] != encoded:
            observed = datetime.now(timezone.utc)
            if history:
                observed = max(observed, _timestamp(history[-1][1]))
            sequence = len(history) + 1
            next_row = sequence, observed.isoformat(), encoded
            db.execute("INSERT INTO roster_observation VALUES (?, ?, ?)", next_row)
            history.append(next_row)
        return tuple(
            RosterSnapshot(
                f"quickbooks-time/{self._roster.connection}/{self._roster.series}/{sequence}",
                _timestamp(observed), "quickbooks-time:adapter",
                "Observed published schedule" if sequence == 1 else "Observed schedule revision",
                tuple(_assignment(item["assignment"]) for item in json.loads(payload)),
            )
            for sequence, observed, payload in history
        )

    @staticmethod
    def _approval_dates(users):
        return {
            user_id: date.fromisoformat(row["approved_to"])
            for user_id, row in users.items()
            if row.get("approved_to") not in (None, "", "0000-00-00")
        }

    def _attendance(self, users, published, current):
        scope = self._roster.window
        first, last = self._days(scope)
        approvals = self._approval_dates(users)
        if not any(through >= first for through in approvals.values()):
            return None
        timecards = self._timesheets(scope)
        if self._approval_dates(self._users()) != approvals:
            raise QuickBooksTimeError("QuickBooks Time approval changed during reconciliation.")
        intervals, exceptions = [], set()
        for ref, row in sorted(timecards.items()):
            user_id, jobcode = row["user_id"], row["jobcode_id"]
            if user_id not in users or jobcode not in self._jobcodes:
                continue
            if row["type"] == "manual":
                if first <= date.fromisoformat(row["date"]) <= last:
                    exceptions.add(f"timesheet/{ref}")
                continue
            if row["type"] != "regular":
                raise ValueError
            start, end = self._punches(row)
            if start >= scope.ends_at or (end is not None and end <= scope.starts_at):
                continue
            approved_to = approvals.get(user_id, date.min)
            if (end is None or date.fromisoformat(row["date"]) > approved_to
                    or self._days(TimeWindow(start, end))[1] > approved_to):
                exceptions.add(f"timesheet/{ref}")
                continue
            worked = TimeWindow(max(start, scope.starts_at), min(end, scope.ends_at))
            assignment = self._staff_assignment(user_id, jobcode, worked)
            if assignment is not None:
                intervals.append(assignment)
        for item in published:
            planned = _assignment(item["assignment"])
            if not any(work.staff_id == planned.staff_id and work.role == planned.role
                       and work.window.overlaps(planned.window) for work in intervals):
                exceptions.add(f"schedule-event/{item['event_id']}/user/{item['user_id']}")
        return ApprovedAttendance(current.reference, tuple(intervals), tuple(sorted(exceptions)))

    def get_staffing(self, roster_series: str) -> StaffScheduleEvidence:
        if roster_series != self._roster.series:
            raise ValueError("The roster series is outside the configured QuickBooks Time scope.")
        try:
            # Serialize observations so concurrent polls cannot append an older snapshot last.
            with self._transaction() as db:
                requests = dict(db.execute(
                    "SELECT event_id, correlation FROM shift_request WHERE event_id IS NOT NULL"
                ))
                users = self._users()
                published = self._published_assignments(self._events(published_only=True), users, requests)
                history = self._observations(db, published)
                attendance = self._attendance(users, published, history[-1])
                links = tuple((item["correlation"], _assignment(item["assignment"]))
                              for item in published if item["correlation"] and len(history) > 1)
                return StaffScheduleEvidence(history[0], history[1:], attendance, links)
        except (KeyError, TypeError, ValueError):
            raise QuickBooksTimeError("QuickBooks Time returned invalid scheduling evidence.") from None

    def _draft_fields(self, request):
        scope = self._roster
        if (request.location != scope.location or request.window.starts_at < scope.window.starts_at
                or request.window.ends_at > scope.window.ends_at):
            raise ValueError("The coverage request is outside the configured QuickBooks Time scope.")
        jobcodes = [key for key, pair in self._jobcodes.items() if pair == (request.role, request.qualification)]
        if len(jobcodes) != 1:
            raise ValueError("The coverage request needs one mapped QuickBooks Time job code.")
        return {
            "schedule_calendar_id": scope.calendar_id, "jobcode_id": jobcodes[0],
            "start": request.window.starts_at.isoformat(), "end": request.window.ends_at.isoformat(),
            "all_day": False, "draft": True, "active": True, "assigned_user_ids": "",
            "timezone": scope.zone.key, "title": _EVENT_TITLE_PREFIX + request.correlation_id,
        }

    def post_open_shift(self, request: OpenShiftRequest) -> None:
        fields = self._draft_fields(request)
        encoded = json.dumps(request.as_provider_message(), sort_keys=True)
        # Commit the reservation before POST. An ambiguous write is recovered by readback.
        with self._transaction() as db:
            existing = db.execute(
                "SELECT requirements, event_id FROM shift_request WHERE correlation = ?", (request.correlation_id,)
            ).fetchone()
            if existing and existing[0] != encoded:
                raise QuickBooksTimeError("The coverage reference is already bound to different requirements.")
            if existing and existing[1] is not None:
                return
            matching = [row for row in self._events().values() if row.get("title") == fields["title"]]
            if len(matching) > 1:
                raise QuickBooksTimeError("Multiple QuickBooks Time events match the coverage reference.")
            if not existing:
                db.execute("INSERT INTO shift_request VALUES (?, ?, NULL)", (request.correlation_id, encoded))

        if matching:
            event = matching[0]
        elif existing:
            raise QuickBooksTimeError("QuickBooks Time draft creation is awaiting provider readback.")
        else:
            try:
                event = self._client.create_schedule_event(fields)
            except QuickBooksTimeRejected:
                with self._transaction() as db:
                    db.execute(
                        "DELETE FROM shift_request WHERE correlation = ? AND event_id IS NULL",
                        (request.correlation_id,),
                    )
                raise

        try:
            if (event["schedule_calendar_id"] != fields["schedule_calendar_id"]
                    or event["jobcode_id"] != fields["jobcode_id"] or event["title"] != fields["title"]
                    or event["all_day"] is not False or event["active"] is not True or _window(event) != request.window
                    or (not matching and (event["draft"] is not True or _assignees(event)))):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise QuickBooksTimeError("The QuickBooks Time event does not match the coverage request.") from None

        with self._transaction() as db:
            result = db.execute(
                "UPDATE shift_request SET event_id = ? WHERE correlation = ? AND (event_id IS NULL OR event_id = ?)",
                (event["id"], request.correlation_id, event["id"]),
            )
            if result.rowcount != 1:
                raise QuickBooksTimeError("The coverage reference resolved to inconsistent QuickBooks Time events.")
