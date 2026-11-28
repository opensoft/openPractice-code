"""Synthetic workforce identities and per-run staffing assignments for QA."""

import json
from dataclasses import dataclass
from pathlib import Path


QA_STAFF = {
    f"{role}-{number:02d}": role
    for role, count in (("VA", 2), ("LVN", 2), ("RN", 3), ("NP", 2), ("MD", 2))
    for number in range(1, count + 1)
}
T13_STAFFING = Path(__file__).parent / "fixtures" / "t13_staffing.json"


@dataclass(frozen=True)
class QAStaffAssignment:
    role: str
    specialty: str
    assignment: str


def load_qa_run(path):
    """Read the participating staff and their selected specialty for this run."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data:
        raise ValueError("A QA run needs a nonempty object keyed by synthetic staff ID.")

    run = {}
    for staff_id, entry in data.items():
        if staff_id not in QA_STAFF:
            raise ValueError(f"Unknown synthetic staff ID: {staff_id}.")
        if not isinstance(entry, dict) or entry.keys() != {"specialty", "assignment"}:
            raise ValueError(f"{staff_id} needs a specialty and an assignment.")
        specialty = entry["specialty"]
        assignment = entry["assignment"]
        if not isinstance(specialty, str) or not specialty.strip():
            raise ValueError(f"{staff_id} needs a nonempty specialty.")
        if assignment not in ("scheduled", "float"):
            raise ValueError(f"{staff_id} assignment must be scheduled or float.")
        run[staff_id] = QAStaffAssignment(QA_STAFF[staff_id], specialty.strip(), assignment)
    return run
