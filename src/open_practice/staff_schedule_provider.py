"""Provider-neutral AP-11 evidence and canonical 1.0 messages."""

import re
from dataclasses import dataclass

from open_practice.staffing_variance import ApprovedAttendance, RosterSnapshot, StaffingAssignment, TimeWindow


CONTRACT = "openpractice-staff-schedule-provider/1.0"
CORRELATION_ID = re.compile(r"[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}")


def _message(kind, data):
    return {"contract": CONTRACT, "kind": kind, "data": data}


def assignment_fields(assignment: StaffingAssignment) -> dict:
    return {
        "staff_id": assignment.staff_id,
        "location": assignment.location,
        "role": assignment.role,
        "qualification": assignment.qualification,
        "starts_at": assignment.window.starts_at.isoformat(),
        "ends_at": assignment.window.ends_at.isoformat(),
    }


def _snapshot_fields(snapshot):
    return {
        "reference": snapshot.reference,
        "recorded_at": snapshot.recorded_at.isoformat(),
        "recorded_by": snapshot.recorded_by,
        "reason": snapshot.reason,
        "assignments": [assignment_fields(item) for item in snapshot.assignments],
    }


@dataclass(frozen=True)
class OpenShiftRequest:
    correlation_id: str
    location: str
    role: str
    qualification: str
    window: TimeWindow
    headcount: int

    def __post_init__(self):
        if not isinstance(self.correlation_id, str) or not CORRELATION_ID.fullmatch(self.correlation_id):
            raise ValueError("Use a random UUID hex value for the coverage request reference.")
        workforce_fields = (self.location, self.role, self.qualification)
        if any(not isinstance(value, str) or not value.strip() for value in workforce_fields):
            raise ValueError("A coverage request needs a site, role and qualification.")
        if type(self.headcount) is not int or self.headcount < 1:
            raise ValueError("A coverage request needs a positive headcount.")

    def as_provider_message(self) -> dict:
        return _message("open_shift_request", {
            "correlation_id": self.correlation_id,
            "location": self.location,
            "role": self.role,
            "qualification": self.qualification,
            "starts_at": self.window.starts_at.isoformat(),
            "ends_at": self.window.ends_at.isoformat(),
            "headcount": self.headcount,
        })


@dataclass(frozen=True)
class StaffScheduleEvidence:
    published: RosterSnapshot
    revisions: tuple[RosterSnapshot, ...]
    attendance: ApprovedAttendance | None
    coverage_links: tuple[tuple[str, StaffingAssignment], ...]

    @property
    def current(self) -> RosterSnapshot:
        return self.revisions[-1] if self.revisions else self.published

    def coverage_status(self, request: OpenShiftRequest) -> str:
        confirmed = {
            assignment.staff_id for reference, assignment in self.coverage_links
            if self.revisions
            and assignment in self.current.assignments
            and reference == request.correlation_id
            and (assignment.location, assignment.role, assignment.qualification)
            == (request.location, request.role, request.qualification)
            and assignment.window.starts_at <= request.window.starts_at
            and assignment.window.ends_at >= request.window.ends_at
        }
        return "confirmed" if len(confirmed) >= request.headcount else "pending"

    def as_provider_message(self, roster_series: str) -> dict:
        attendance = None
        if self.attendance is not None:
            attendance = {
                "roster_reference": self.attendance.roster_reference,
                "worked_intervals": [assignment_fields(item) for item in self.attendance.worked_intervals],
                "unresolved_exceptions": list(self.attendance.unresolved_exceptions),
            }
        return _message("staffing", {
            "roster_series": roster_series,
            "published": _snapshot_fields(self.published),
            "revisions": [_snapshot_fields(item) for item in self.revisions],
            "attendance": attendance,
            "coverage_links": [
                {"correlation_id": reference, "assignment": assignment_fields(item)}
                for reference, item in self.coverage_links
            ],
        })

    def open_shift_message(self, request: OpenShiftRequest) -> dict:
        status = self.coverage_status(request)
        return _message("open_shift_state", {
            "request": request.as_provider_message()["data"],
            "status": status,
            "roster_reference": self.current.reference if status == "confirmed" else None,
        })
