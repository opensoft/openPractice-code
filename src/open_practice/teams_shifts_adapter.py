"""Map Microsoft Teams Shifts evidence into openPractice AP-11 records."""

import json
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from open_practice.staff_schedule_provider import StaffScheduleEvidence, assignment_fields, provider_message
from open_practice.staffing_variance import ApprovedAttendance, RosterSnapshot, StaffingAssignment, TimeWindow
from open_practice.teams_shifts_client import TeamsShiftsError, TeamsShiftsHTTPError, TeamsShiftsTokenError


_REQUEST_PREFIX = "openPractice coverage "
# Graph returns 429 when throttling prevents the request from taking effect.
_REJECTED_CREATES = {400, 401, 403, 404, 422, 429}


@dataclass(frozen=True)
class TeamsShiftsWorker:
    staff_id: str
    role: str
    qualification: str

    def __post_init__(self):
        if any(not isinstance(field, str) or not field.strip()
               for field in (self.staff_id, self.role, self.qualification)):
            raise ValueError("A Shifts worker needs a staff reference, role and qualification.")


@dataclass(frozen=True)
class TeamsShiftsRoster:
    series: str
    location: str
    scheduling_group_id: str
    window: TimeWindow

    def __post_init__(self):
        if any(not isinstance(field, str) or not field.strip()
               for field in (self.series, self.location, self.scheduling_group_id)):
            raise ValueError("A Shifts roster needs a series, location and scheduling group.")


def _timestamp(value):
    instant = datetime.fromisoformat(value)
    if instant.utcoffset() is None:
        raise ValueError("Graph scheduling timestamps must include an offset.")
    return instant.astimezone(timezone.utc)


def _shift_window(item):
    return TimeWindow(_timestamp(item["startDateTime"]), _timestamp(item["endDateTime"]))


def _punch(card, field):
    item = card.get(field)
    return _timestamp(item["dateTime"]) if item is not None else None


def _worked_windows(window, breaks):
    break_windows = sorted(
        (TimeWindow(_punch(row, "start"), _punch(row, "end")) for row in breaks),
        key=lambda item: item.starts_at,
    )
    start = window.starts_at
    for item in break_windows:
        if item.starts_at < start or item.ends_at > window.ends_at:
            raise ValueError("Timecard breaks overlap or fall outside the worked interval.")
        if start < item.starts_at:
            yield TimeWindow(start, item.starts_at)
        start = item.ends_at
    if start < window.ends_at:
        yield TimeWindow(start, window.ends_at)


class TeamsShiftsAdapter:
    """Read one scheduling group and its mapped workers in a finite roster window."""

    def __init__(self, client, roster, *, workers, history_path):
        self._client = client
        self._roster = roster
        self._workers = dict(workers)
        self._history_path = str(history_path)
        if not self._workers or len({row.staff_id for row in self._workers.values()}) != len(self._workers):
            raise ValueError("Map Graph user IDs to distinct workforce references.")
        if self._history_path in {"", ":memory:"}:
            raise ValueError("Each Shifts roster requires a durable history file.")
        identity = json.dumps((
            client.team_id, roster.series, roster.location, roster.scheduling_group_id,
            roster.window.starts_at.isoformat(), roster.window.ends_at.isoformat(),
        ))
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS scope (slot INTEGER PRIMARY KEY CHECK (slot = 1), identity TEXT)")
            db.execute("INSERT OR IGNORE INTO scope VALUES (1, ?)", (identity,))
            if db.execute("SELECT identity FROM scope WHERE slot = 1").fetchone()[0] != identity:
                raise ValueError("Use a separate Shifts history file for each roster scope.")
            db.execute(
                "CREATE TABLE IF NOT EXISTS observation "
                "(sequence INTEGER PRIMARY KEY, observed_at TEXT NOT NULL, assignments TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS coverage_request "
                "(correlation TEXT PRIMARY KEY, requirements TEXT NOT NULL, existing_shifts TEXT NOT NULL, "
                "open_shift_id TEXT UNIQUE)"
            )

    @contextmanager
    def _transaction(self):
        try:
            with closing(sqlite3.connect(self._history_path, timeout=30)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        except sqlite3.Error:
            raise TeamsShiftsError("The Teams Shifts roster history is unavailable.") from None

    def _assignment(self, worker, window):
        return StaffingAssignment(worker.staff_id, self._roster.location, worker.role, worker.qualification, window)

    def _published_shifts(self):
        assignments = {}
        for shift_id, row in self._client.rows("shifts").items():
            if row.get("schedulingGroupId") != self._roster.scheduling_group_id:
                continue
            shared = row.get("sharedShift")
            # A copied or incomplete shift may lack an assignee. Count only mapped Graph users.
            user_id = row.get("userId")
            worker = self._workers.get(user_id)
            if shared is None or worker is None:
                continue
            window = _shift_window(shared)
            if window.overlaps(self._roster.window):
                assignments[shift_id] = (user_id, self._assignment(worker, window))
        return assignments

    def _snapshots(self, db, assignments):
        history = db.execute("SELECT * FROM observation ORDER BY sequence").fetchall()
        serialized = json.dumps([assignment_fields(item) for item in assignments])
        if not history or history[-1][2] != serialized:
            instant = datetime.now(timezone.utc)
            if history:
                instant = max(instant, _timestamp(history[-1][1]))
            row = (len(history) + 1, instant.isoformat(), serialized)
            db.execute("INSERT INTO observation VALUES (?, ?, ?)", row)
            history.append(row)
        return tuple(
            RosterSnapshot(
                f"teams-shifts/{self._roster.series}/{sequence}", _timestamp(instant), "microsoft-graph",
                "Published schedule captured" if sequence == 1 else "Shared schedule changed",
                tuple(
                    StaffingAssignment(
                        row["staff_id"], row["location"], row["role"], row["qualification"],
                        TimeWindow(_timestamp(row["starts_at"]), _timestamp(row["ends_at"])),
                    )
                    for row in json.loads(serialized)
                ),
            )
            for sequence, instant, serialized in history
        )

    def _coverage_links(self, db, shifts):
        requests = {
            ref: (correlation, json.loads(requirements), frozenset(json.loads(prior)))
            for correlation, requirements, prior, ref in db.execute("SELECT * FROM coverage_request")
            if ref is not None
        }
        if not requests:
            return ()
        matches = {}
        for claim in self._client.rows("openShiftChangeRequests").values():
            match = requests.get(claim.get("openShiftId"))
            if match is None or claim.get("state") != "approved":
                continue
            correlation, fields, prior = match
            window = TimeWindow(_timestamp(fields["starts_at"]), _timestamp(fields["ends_at"]))
            candidates = [
                assignment for key, (user_id, assignment) in shifts.items()
                if key not in prior and user_id == claim.get("senderUserId")
                and assignment.window.starts_at <= window.starts_at
                and assignment.window.ends_at >= window.ends_at
                and (assignment.role, assignment.qualification) == (fields["role"], fields["qualification"])
            ]
            if len(candidates) == 1:
                matches.setdefault(candidates[0], set()).add(correlation)
        links = [(next(iter(claims)), assignment) for assignment, claims in matches.items() if len(claims) == 1]
        return tuple(sorted(links, key=lambda link: (link[0], link[1].staff_id)))

    def _attendance(self, snapshots):
        roster = self._roster.window
        assigned = {}
        for snapshot in snapshots:
            for assignment in snapshot.assignments:
                assigned.setdefault(assignment.staff_id, set()).add(assignment.window)
        worked = []
        unresolved = []
        for ref, row in self._client.rows("timeCards").items():
            worker = self._workers.get(row.get("userId"))
            windows = assigned.get(worker.staff_id) if worker else None
            if not windows:
                continue
            start, end = _punch(row, "clockInEvent"), _punch(row, "clockOutEvent")
            if start is None or start >= roster.ends_at or (end is not None and end <= roster.starts_at):
                continue
            if row.get("state") != "clockedOut" or row.get("confirmedBy") != "manager" or end is None:
                unresolved.append(f"timeCard/{ref}")
                continue
            card = TimeWindow(start, end)
            if not any(window.overlaps(card) for window in windows):
                unresolved.append(f"timeCard/{ref}")
                continue
            for window in _worked_windows(card, row.get("breaks") or ()):
                left, right = max(window.starts_at, roster.starts_at), min(window.ends_at, roster.ends_at)
                if left < right:
                    worked.append(self._assignment(worker, TimeWindow(left, right)))
        if not worked:
            return None
        return ApprovedAttendance(snapshots[-1].reference, tuple(worked), tuple(sorted(unresolved)))

    def get_staffing(self, roster_series):
        if roster_series != self._roster.series:
            raise ValueError("Unknown Teams Shifts roster series.")
        try:
            with self._transaction() as db:
                shifts = self._published_shifts()
                assignments = tuple(sorted(
                    {assignment for _, assignment in shifts.values()},
                    key=lambda item: (item.window.starts_at, item.staff_id, item.window.ends_at),
                ))
                snapshots = self._snapshots(db, assignments)
                links = self._coverage_links(db, shifts) if len(snapshots) > 1 else ()
                attendance = self._attendance(snapshots)
                return StaffScheduleEvidence(snapshots[0], snapshots[1:], attendance, links)
        except (AttributeError, KeyError, TypeError, ValueError):
            raise TeamsShiftsError("Microsoft Graph returned invalid staffing evidence.") from None

    def get_clock_events(self, window):
        events = []
        try:
            for ref, row in self._client.rows("timeCards").items():
                worker = self._workers.get(row.get("userId"))
                if not worker:
                    continue
                for direction, field in (("in", "clockInEvent"), ("out", "clockOutEvent")):
                    instant = _punch(row, field)
                    if instant is not None and window.starts_at <= instant < window.ends_at:
                        events.append({
                            "reference": f"timeCard/{ref}/{direction}", "staff_id": worker.staff_id,
                            "at": instant.isoformat(), "direction": direction,
                        })
        except (AttributeError, KeyError, TypeError, ValueError):
            raise TeamsShiftsError("Microsoft Graph returned invalid clock evidence.") from None
        return provider_message("clock_events", {
            "location": self._roster.location, "starts_at": window.starts_at.isoformat(),
            "ends_at": window.ends_at.isoformat(),
            "records": sorted(events, key=lambda item: (item["at"], item["reference"])),
        })

    def _recover_open_shift(self, request, draft):
        candidates = []
        try:
            for key, row in self._client.rows("openShifts").items():
                if row.get("schedulingGroupId") != self._roster.scheduling_group_id:
                    continue
                if any(
                    item is not None and item.get("notes") == draft["notes"]
                    and _shift_window(item) == request.window
                    for item in (row.get("draftOpenShift"), row.get("sharedOpenShift"))
                ):
                    candidates.append(key)
        except (AttributeError, KeyError, TypeError, ValueError):
            raise TeamsShiftsError("Microsoft Graph returned invalid open-shift evidence.") from None
        if len(candidates) != 1:
            raise TeamsShiftsError("Open-shift submission is awaiting unambiguous provider readback.")
        return candidates[0]

    def post_open_shift(self, request):
        roster = self._roster
        if (request.location != roster.location or request.window.starts_at < roster.window.starts_at
                or request.window.ends_at > roster.window.ends_at):
            raise ValueError("The coverage request must belong to the configured Teams roster.")
        correlation = request.correlation_id
        requirements = json.dumps(request.as_provider_message()["data"], sort_keys=True)
        draft = {
            "displayName": f"{request.role} coverage", "notes": _REQUEST_PREFIX + correlation,
            "startDateTime": request.window.starts_at.isoformat(),
            "endDateTime": request.window.ends_at.isoformat(),
            "openSlotCount": request.headcount, "theme": "blue", "activities": [],
        }
        with self._transaction() as db:
            stored = db.execute(
                "SELECT requirements, open_shift_id FROM coverage_request WHERE correlation = ?", (correlation,)
            ).fetchone()
            if stored and stored[0] != requirements:
                raise ValueError("An existing correlation ID has different coverage requirements.")
            if stored and stored[1] is not None:
                return
            if stored is None:
                try:
                    prior = json.dumps(list(self._published_shifts()))
                except (AttributeError, KeyError, TypeError, ValueError):
                    raise TeamsShiftsError("Microsoft Graph returned invalid shared shifts.") from None
                # Commit the reservation before POST so an ambiguous failure never blindly reposts.
                db.execute("INSERT INTO coverage_request VALUES (?, ?, ?, NULL)", (correlation, requirements, prior))

        if stored:
            ref = self._recover_open_shift(request, draft)
        else:
            try:
                ref = self._client.create_open_shift({
                    "schedulingGroupId": roster.scheduling_group_id, "draftOpenShift": draft, "sharedOpenShift": None,
                })
            except (TeamsShiftsHTTPError, TeamsShiftsTokenError) as error:
                if isinstance(error, TeamsShiftsTokenError) or error.status in _REJECTED_CREATES:
                    with self._transaction() as db:
                        db.execute("DELETE FROM coverage_request WHERE correlation = ? AND open_shift_id IS NULL",
                                   (correlation,))
                raise
        with self._transaction() as db:
            existing, = db.execute(
                "SELECT open_shift_id FROM coverage_request WHERE correlation = ?", (correlation,)
            ).fetchone()
            if existing not in (None, ref):
                raise TeamsShiftsError("Microsoft Graph returned conflicting open-shift references.")
            db.execute("UPDATE coverage_request SET open_shift_id = ? WHERE correlation = ?", (ref, correlation))
