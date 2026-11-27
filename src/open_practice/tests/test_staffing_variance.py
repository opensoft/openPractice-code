from dataclasses import replace
from datetime import timedelta
from unittest import TestCase

from open_practice.staffing_demand import DemandInterval, calculate_demand
from open_practice.staffing_variance import (
    ApprovedAttendance,
    ReplacementCandidate,
    RosterSnapshot,
    StaffQualification,
    StaffTimeVariance,
    StaffingAssignment,
    TimeWindow,
    compare_staffing,
    rank_replacements,
)
from open_practice.tests.test_staffing_demand import at, overlapping_procedures


REPORTING_PERIOD = TimeWindow(at(9, 0), at(9, 30))
PROCEDURE_WINDOW = TimeWindow(at(9, 10), at(9, 20))
PROCEDURE_QUALIFICATION = StaffQualification("RN", "procedure-qualified")


def rn_assignment(staff_id, window=PROCEDURE_WINDOW):
    return StaffingAssignment(staff_id, "Denver", "RN", "procedure-qualified", window)


def rn_candidate(staff_id, coverage_type="float"):
    return ReplacementCandidate(
        staff_id, coverage_type, frozenset({"Denver"}), (PROCEDURE_QUALIFICATION,), (PROCEDURE_WINDOW,),
    )


def rn_callout_scenario():
    first_rn, called_out, float_rn = map(rn_assignment, ("assigned-rn-1", "assigned-rn-2", "float-rn"))
    published = RosterSnapshot("published", at(8, 0), "scheduler", "Published roster", (first_rn, called_out))
    callout = RosterSnapshot("callout", at(8, 30), "charge-rn", "Assigned RN called out", (first_rn,))
    revised = RosterSnapshot("float-confirmed", at(8, 40), "manager", "Confirmed float replacement", (first_rn, float_rn))
    worked = ApprovedAttendance(revised.reference, (
        first_rn, replace(float_rn, window=TimeWindow(at(9, 12), at(9, 20))),
    ))
    unqualified = replace(rn_candidate("unqualified-rn"), qualifications=(StaffQualification("RN", "triage-qualified"),))
    return published, callout, revised, worked, (unqualified, rn_candidate("float-rn"))


class StaffingVarianceTest(TestCase):
    def test_rn_callout_preserves_baseline_and_shows_revised_and_actual_shortfalls(self):
        demand = calculate_demand(overlapping_procedures())
        published, callout, revised, worked, candidates = rn_callout_scenario()
        first_rn, called_out = published.assignments

        after_callout = compare_staffing(demand, published, period=REPORTING_PERIOD, revisions=(callout,))
        self.assertEqual(after_callout.published_gaps, ())
        uncovered, = after_callout.revised_gaps
        self.assertEqual(uncovered, DemandInterval(
            "Denver", "RN", "procedure-qualified", at(9, 10), at(9, 20), 1,
            ("procedure-1", "procedure-2"),
        ))
        self.assertEqual(uncovered.staff_minutes, 10)
        self.assertIsNone(after_callout.approved_attendance_gaps)
        self.assertIsNone(after_callout.revised_to_actual)
        self.assertEqual(rank_replacements(uncovered, candidates, after_callout.revised), [candidates[1]])
        self.assertEqual(after_callout.revised_gaps, (uncovered,))

        result = compare_staffing(
            demand, published, period=REPORTING_PERIOD, revisions=(callout, revised), attendance=worked,
        )
        self.assertEqual(result.revised_gaps, ())
        self.assertEqual(result.published.assignments, (first_rn, called_out))
        self.assertEqual(result.revisions, (callout, revised))
        self.assertEqual(result.removed_since_publication, (called_out,))
        self.assertEqual(result.added_since_publication, (rn_assignment("float-rn"),))
        self.assertEqual(result.attendance.worked_intervals[1].window.starts_at, at(9, 12))

        actual_gap, = result.approved_attendance_gaps
        self.assertEqual(actual_gap, replace(uncovered, ends_at=at(9, 12)))
        self.assertEqual(actual_gap.staff_minutes, 2)
        self.assertEqual(result.revised_to_actual, (StaffTimeVariance(
            "float-rn", "Denver", "RN", "procedure-qualified", timedelta(minutes=-2),
        ),))

    def test_replacement_ranking_checks_full_availability_credentials_and_roster_commitments(self):
        demand = calculate_demand(overlapping_procedures())
        published, callout, _, _, _ = rn_callout_scenario()
        busy = replace(rn_assignment("busy-rn"), location="Boulder")
        busy_roster = replace(callout, assignments=(*callout.assignments, busy))
        gap, = compare_staffing(demand, published, period=REPORTING_PERIOD, revisions=(busy_roster,)).revised_gaps
        float_rn = replace(rn_candidate("eligible-float"), availability=(
            TimeWindow(at(9, 10), at(9, 15)), TimeWindow(at(9, 15), at(9, 20)),
        ))
        per_diem = rn_candidate("eligible-per-diem", "per-diem")
        overtime = rn_candidate("eligible-overtime", "overtime")
        candidates = (
            overtime,
            rn_candidate("busy-rn"),
            replace(rn_candidate("wrong-site-rn"), locations=frozenset({"Boulder"})),
            replace(rn_candidate("not-roster-eligible"), roster_eligible=False),
            replace(rn_candidate("not-fully-available"), availability=(TimeWindow(at(9, 12), at(9, 20)),)),
            replace(rn_candidate("expiring-qualification"), qualifications=(
                replace(PROCEDURE_QUALIFICATION, expires_at=at(9, 15)),
            )),
            per_diem,
            float_rn,
        )
        self.assertEqual(rank_replacements(gap, candidates, busy_roster), [float_rn, per_diem, overtime])
        self.assertEqual(rank_replacements(
            gap, candidates, busy_roster, coverage_order=("per-diem", "float"),
        ), [per_diem, float_rn])

    def test_attendance_preserves_exceptions_and_rejects_wrong_revision_or_double_count(self):
        demand = calculate_demand(overlapping_procedures())
        published, callout, revised, worked, _ = rn_callout_scenario()
        with self.assertRaisesRegex(ValueError, "current roster revision"):
            compare_staffing(demand, published, period=REPORTING_PERIOD, revisions=(callout,), attendance=worked)

        attendance = replace(worked, unresolved_exceptions=("attendance-review-1",))
        report = compare_staffing(
            demand, published, period=REPORTING_PERIOD, revisions=(callout, revised), attendance=attendance,
        )
        self.assertEqual(report.attendance.unresolved_exceptions, ("attendance-review-1",))
        self.assertEqual(sum(gap.staff_minutes for gap in report.approved_attendance_gaps), 2)

        first_assignment = published.assignments[0]
        conflicting_assignment = replace(first_assignment, location="Boulder")
        with self.assertRaisesRegex(ValueError, "two assignments at the same time"):
            replace(published, assignments=(first_assignment, conflicting_assignment))
