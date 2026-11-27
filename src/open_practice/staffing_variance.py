"""Compare AP-10 demand with published, revised and approved staffing records."""

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from itertools import pairwise

from open_practice.staffing_demand import ONE_MINUTE, DemandInterval


@dataclass(frozen=True)
class TimeWindow:
    starts_at: datetime
    ends_at: datetime

    def __post_init__(self):
        if self.starts_at.utcoffset() is None or self.ends_at.utcoffset() is None:
            raise ValueError("Staffing windows require time zones.")
        start, end = self.starts_at.astimezone(timezone.utc), self.ends_at.astimezone(timezone.utc)
        if end <= start:
            raise ValueError("A staffing window must end after it starts.")
        object.__setattr__(self, "starts_at", start)
        object.__setattr__(self, "ends_at", end)

    def overlaps(self, other: "TimeWindow") -> bool:
        return self.starts_at < other.ends_at and other.starts_at < self.ends_at


@dataclass(frozen=True)
class StaffingAssignment:
    """One staff member confirmed for a particular site, role and qualification."""

    staff_id: str
    location: str
    role: str
    qualification: str
    window: TimeWindow

    def __post_init__(self):
        if not all((self.staff_id, self.location, self.role, self.qualification)):
            raise ValueError("A staffing assignment needs a staff reference, location, role and qualification.")


def _checked_assignments(assignments: Iterable[StaffingAssignment]) -> tuple[StaffingAssignment, ...]:
    snapshot = tuple(assignments)
    ordered = sorted(snapshot, key=lambda item: (item.staff_id, item.window.starts_at))
    for earlier, later in pairwise(ordered):
        if earlier.staff_id == later.staff_id and earlier.window.overlaps(later.window):
            raise ValueError("A staff member cannot cover two assignments at the same time.")
    return snapshot


@dataclass(frozen=True)
class RosterSnapshot:
    reference: str
    recorded_at: datetime
    recorded_by: str
    reason: str
    assignments: tuple[StaffingAssignment, ...]

    def __post_init__(self):
        if not all((self.reference, self.recorded_by, self.reason)) or self.recorded_at.utcoffset() is None:
            raise ValueError("A roster snapshot needs a reference, timestamp, actor and reason.")
        object.__setattr__(self, "recorded_at", self.recorded_at.astimezone(timezone.utc))
        object.__setattr__(self, "assignments", _checked_assignments(self.assignments))


@dataclass(frozen=True)
class ApprovedAttendance:
    roster_reference: str
    worked_intervals: tuple[StaffingAssignment, ...]
    unresolved_exceptions: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "worked_intervals", _checked_assignments(self.worked_intervals))
        object.__setattr__(self, "unresolved_exceptions", tuple(self.unresolved_exceptions))


@dataclass(frozen=True)
class StaffTimeVariance:
    staff_id: str
    location: str
    role: str
    qualification: str
    difference: timedelta

    @property
    def staff_minutes(self) -> float:
        return self.difference / ONE_MINUTE


@dataclass(frozen=True)
class StaffingVariance:
    period: TimeWindow
    published: RosterSnapshot
    revisions: tuple[RosterSnapshot, ...]
    attendance: ApprovedAttendance | None
    published_gaps: tuple[DemandInterval, ...]
    revised_gaps: tuple[DemandInterval, ...]
    approved_attendance_gaps: tuple[DemandInterval, ...] | None
    removed_since_publication: tuple[StaffingAssignment, ...]
    added_since_publication: tuple[StaffingAssignment, ...]
    revised_to_actual: tuple[StaffTimeVariance, ...] | None

    @property
    def revised(self) -> RosterSnapshot:
        return self.revisions[-1] if self.revisions else self.published


def _coverage_gaps(demand, assignments, period):
    windows = defaultdict(list)
    for item in assignments:
        windows[item.location, item.role, item.qualification].append(item.window)

    gaps = []
    for interval in demand:
        start = max(interval.starts_at, period.starts_at)
        end = min(interval.ends_at, period.ends_at)
        if start >= end:
            continue
        changes = Counter({start: 0, end: 0})
        for window in windows[interval.location, interval.role, interval.qualification]:
            covered_from = max(start, window.starts_at)
            covered_until = min(end, window.ends_at)
            if covered_from < covered_until:
                changes[covered_from] += 1
                changes[covered_until] -= 1

        covered = 0
        boundaries = sorted(time for time, change in changes.items() if change or time in (start, end))
        for boundary, next_boundary in pairwise(boundaries):
            covered += changes[boundary]
            shortage = interval.headcount - covered
            if shortage > 0:
                gaps.append(replace(interval, starts_at=boundary, ends_at=next_boundary, headcount=shortage))
    return tuple(gaps)


def _worked_time_variance(revised, worked, period):
    changes = defaultdict(timedelta)
    for sign, assignments in ((-1, revised), (1, worked)):
        for item in assignments:
            start = max(item.window.starts_at, period.starts_at)
            end = min(item.window.ends_at, period.ends_at)
            if start < end:
                changes[item.staff_id, item.location, item.role, item.qualification] += sign * (end - start)
    return tuple(StaffTimeVariance(*key, change) for key, change in sorted(changes.items()) if change)


def compare_staffing(
    demand: Iterable[DemandInterval],
    published: RosterSnapshot,
    *,
    period: TimeWindow,
    revisions: Iterable[RosterSnapshot] = (),
    attendance: ApprovedAttendance | None = None,
) -> StaffingVariance:
    """Report shortages from confirmed rosters and approved work in one period."""
    demand, revisions = tuple(demand), tuple(revisions)
    history = (published, *revisions)
    if len({snapshot.reference for snapshot in history}) != len(history) or any(
        earlier.recorded_at > later.recorded_at for earlier, later in pairwise(history)
    ):
        raise ValueError("Roster revisions need unique references in chronological order.")
    revised = history[-1]
    if attendance is not None and attendance.roster_reference != revised.reference:
        raise ValueError("Attendance must be reconciled against the current roster revision.")
    published_set, revised_set = set(published.assignments), set(revised.assignments)
    return StaffingVariance(
        period=period,
        published=published,
        revisions=revisions,
        attendance=attendance,
        published_gaps=_coverage_gaps(demand, published.assignments, period),
        revised_gaps=_coverage_gaps(demand, revised.assignments, period),
        approved_attendance_gaps=(
            _coverage_gaps(demand, attendance.worked_intervals, period) if attendance is not None else None
        ),
        removed_since_publication=tuple(
            item for item in published.assignments if item not in revised_set and item.window.overlaps(period)
        ),
        added_since_publication=tuple(
            item for item in revised.assignments if item not in published_set and item.window.overlaps(period)
        ),
        revised_to_actual=(
            _worked_time_variance(revised.assignments, attendance.worked_intervals, period)
            if attendance is not None else None
        ),
    )


@dataclass(frozen=True)
class StaffQualification:
    role: str
    name: str
    expires_at: datetime | None = None

    def __post_init__(self):
        if self.expires_at is not None:
            if self.expires_at.utcoffset() is None:
                raise ValueError("Qualification expiration needs a time zone.")
            object.__setattr__(self, "expires_at", self.expires_at.astimezone(timezone.utc))


@dataclass(frozen=True)
class ReplacementCandidate:
    staff_id: str
    coverage_type: str
    locations: frozenset[str]
    qualifications: tuple[StaffQualification, ...]
    availability: tuple[TimeWindow, ...]
    roster_eligible: bool = True


def _available_for(availability, window):
    available_through = window.starts_at
    for slot in sorted(availability, key=lambda item: item.starts_at):
        if slot.starts_at > available_through:
            break
        available_through = max(available_through, slot.ends_at)
        if available_through >= window.ends_at:
            return True
    return False


def rank_replacements(
    gap: DemandInterval,
    candidates: Iterable[ReplacementCandidate],
    revised_roster: RosterSnapshot,
    *,
    coverage_order: tuple[str, ...] = ("float", "per-diem", "overtime"),
) -> list[ReplacementCandidate]:
    """Suggest eligible staff for the whole gap. Coverage changes arrive as roster revisions."""
    window = TimeWindow(gap.starts_at, gap.ends_at)
    busy = {item.staff_id for item in revised_roster.assignments if item.window.overlaps(window)}
    priority = {coverage_type: index for index, coverage_type in enumerate(coverage_order)}
    eligible = [
        candidate for candidate in candidates
        if candidate.roster_eligible
        and candidate.coverage_type in priority
        and candidate.staff_id not in busy
        and gap.location in candidate.locations
        and any(
            skill.role == gap.role and skill.name == gap.qualification
            and (skill.expires_at is None or skill.expires_at >= window.ends_at)
            for skill in candidate.qualifications
        )
        and _available_for(candidate.availability, window)
    ]
    return sorted(eligible, key=lambda candidate: (priority[candidate.coverage_type], candidate.staff_id))
