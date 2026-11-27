from datetime import datetime, timedelta, timezone
from unittest import TestCase

from open_practice.staffing_demand import (
    BookedAppointment,
    DemandInterval,
    DemandSummary,
    StaffingWindow,
    calculate_demand,
    summarize_demand,
)


DENVER_STANDARD_TIME = timezone(timedelta(hours=-7))


def at(hour, minute):
    return datetime(2026, 11, 26, hour, minute, tzinfo=DENVER_STANDARD_TIME)


def rn_window(start, end, *, qualification="procedure-qualified", headcount=1):
    return StaffingWindow("RN", qualification, timedelta(minutes=start), timedelta(minutes=end), headcount)


def overlapping_procedures():
    return [
        BookedAppointment("procedure-1", "Denver", at(9, 0), (rn_window(10, 20),)),
        BookedAppointment("procedure-2", "Denver", at(9, 10), (rn_window(0, 10),)),
    ]


class StaffingDemandTest(TestCase):
    def test_overlapping_procedures_need_two_rns_and_twenty_rn_minutes(self):
        demand = calculate_demand(overlapping_procedures())
        self.assertEqual(demand, [DemandInterval(
            "Denver", "RN", "procedure-qualified", at(9, 10), at(9, 20), 2,
            ("procedure-1", "procedure-2"),
        )])
        self.assertEqual(demand[0].staff_minutes, 20)

        summary = summarize_demand(demand, at(9, 0), at(9, 30))
        self.assertEqual(summary, [DemandSummary(
            "Denver", "RN", "procedure-qualified", at(9, 0), at(9, 30), 2, timedelta(minutes=20),
        )])
        self.assertEqual(summary[0].staff_minutes, 20)

    def test_pre_visit_and_adjacent_windows_preserve_each_booking_and_clip_to_report(self):
        demand = calculate_demand([
            BookedAppointment("procedure-1", "Denver", at(9, 0), (rn_window(-10, -5), rn_window(0, 10))),
            BookedAppointment("procedure-2", "Denver", at(9, 10), (rn_window(0, 10),)),
        ])
        self.assertEqual([
            (interval.starts_at, interval.ends_at, interval.headcount, interval.booking_ids)
            for interval in demand
        ], [
            (at(8, 50), at(8, 55), 1, ("procedure-1",)),
            (at(9, 0), at(9, 10), 1, ("procedure-1",)),
            (at(9, 10), at(9, 20), 1, ("procedure-2",)),
        ])
        summary = summarize_demand(demand, at(8, 53), at(9, 12))
        self.assertEqual((summary[0].peak_headcount, summary[0].staff_minutes), (1, 14))

    def test_summaries_keep_sites_roles_and_qualifications_separate(self):
        demand = calculate_demand([
            BookedAppointment("procedure-1", "Denver", at(9, 10), (
                rn_window(0, 10, headcount=2),
                rn_window(0, 10, qualification="triage-qualified"),
                StaffingWindow("MA", "procedure-qualified", timedelta(0), timedelta(minutes=5)),
            )),
            BookedAppointment("procedure-2", "Boulder", at(9, 10), (rn_window(0, 10),)),
        ])
        summary = summarize_demand(demand, at(9, 0), at(9, 30))
        self.assertEqual({
            (row.location, row.role, row.qualification): (row.peak_headcount, row.staff_minutes)
            for row in summary
        }, {
            ("Denver", "RN", "procedure-qualified"): (2, 20),
            ("Denver", "RN", "triage-qualified"): (1, 10),
            ("Denver", "MA", "procedure-qualified"): (1, 5),
            ("Boulder", "RN", "procedure-qualified"): (1, 10),
        })
