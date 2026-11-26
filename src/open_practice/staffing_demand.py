"""Calculate staffing demand from the resource windows captured at booking."""

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


ONE_MINUTE = timedelta(minutes=1)


@dataclass(frozen=True)
class StaffingWindow:
    """A role's staffing requirement relative to an appointment's start."""

    role: str
    qualification: str
    start_offset: timedelta
    end_offset: timedelta
    headcount: int = 1

    def __post_init__(self):
        if not self.role or not self.qualification:
            raise ValueError("A staffing window needs a role and qualification.")
        if self.end_offset <= self.start_offset:
            raise ValueError("A staffing window must end after it starts.")
        if type(self.headcount) is not int or self.headcount < 1:
            raise ValueError("Staffing headcount must be a positive whole number.")


@dataclass(frozen=True)
class BookedAppointment:
    booking_id: str
    location: str
    starts_at: datetime
    staffing_windows: tuple[StaffingWindow, ...]

    def __post_init__(self):
        if not self.booking_id or not self.location:
            raise ValueError("A booking needs a reference and location.")
        object.__setattr__(self, "staffing_windows", tuple(self.staffing_windows))


@dataclass(frozen=True)
class DemandInterval:
    location: str
    role: str
    qualification: str
    starts_at: datetime
    ends_at: datetime
    headcount: int
    booking_ids: tuple[str, ...]

    @property
    def staff_minutes(self) -> float:
        return (self.ends_at - self.starts_at) * self.headcount / ONE_MINUTE


@dataclass(frozen=True)
class DemandSummary:
    location: str
    role: str
    qualification: str
    starts_at: datetime
    ends_at: datetime
    peak_headcount: int
    staff_time: timedelta

    @property
    def staff_minutes(self) -> float:
        return self.staff_time / ONE_MINUTE


def _utc(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("Staffing demand requires a time zone on each timestamp.")
    return value.astimezone(timezone.utc)


def calculate_demand(bookings: Iterable[BookedAppointment]) -> list[DemandInterval]:
    """Return exact half-open UTC intervals for each location, role and qualification."""
    events = defaultdict(lambda: defaultdict(Counter))
    booking_ids = set()
    for booking in bookings:
        if booking.booking_id in booking_ids:
            raise ValueError("A booking can only appear once in the demand calculation.")
        booking_ids.add(booking.booking_id)
        booking_start = _utc(booking.starts_at)
        for window in booking.staffing_windows:
            timeline = events[booking.location, window.role, window.qualification]
            timeline[booking_start + window.start_offset][booking.booking_id] += window.headcount
            timeline[booking_start + window.end_offset][booking.booking_id] -= window.headcount

    demand = []
    for group, timeline in sorted(events.items()):
        active = Counter()
        previous = None
        for boundary, changes in sorted(timeline.items()):
            if previous is not None and active:
                demand.append(DemandInterval(
                    *group, previous, boundary, active.total(), tuple(sorted(active)),
                ))

            # Apply arrivals and departures together before measuring the next interval.
            for booking_id, delta in changes.items():
                headcount = active[booking_id] + delta
                if headcount:
                    active[booking_id] = headcount
                else:
                    active.pop(booking_id, None)
            previous = boundary
    return demand


def summarize_demand(
    demand: Iterable[DemandInterval], starts_at: datetime, ends_at: datetime,
) -> list[DemandSummary]:
    """Summarize a reporting interval from the exact intervals produced by calculate_demand."""
    starts_at, ends_at = _utc(starts_at), _utc(ends_at)
    if ends_at <= starts_at:
        raise ValueError("A reporting interval must end after it starts.")

    totals = {}
    for interval in demand:
        overlap = min(ends_at, interval.ends_at) - max(starts_at, interval.starts_at)
        if overlap <= timedelta(0):
            continue
        group = (interval.location, interval.role, interval.qualification)
        peak, staff_time = totals.get(group, (0, timedelta(0)))
        totals[group] = max(peak, interval.headcount), staff_time + overlap * interval.headcount

    return [
        DemandSummary(*group, starts_at, ends_at, peak, staff_time)
        for group, (peak, staff_time) in sorted(totals.items())
    ]
