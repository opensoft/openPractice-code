"""Contract conformance fixtures for every canonical feed."""

from dataclasses import replace
from unittest import TestCase

from jsonschema import ValidationError

from open_practice.staff_schedule_provider import CONTRACT, OpenShiftRequest, StaffScheduleEvidence
from open_practice.staffing_variance import TimeWindow
from open_practice.tests.staff_provider_conformance import StaffingConformance, contract_validator, validate_provider_message
from open_practice.tests.test_staffing_demand import at
from open_practice.tests.test_staffing_variance import rn_callout_scenario


SERIES = "synthetic-t26"
REQUEST_ID = "af6577263237429382d93e954a9b9298"


def synthetic_staffing():
    published, callout, revised, worked, _ = rn_callout_scenario()
    request = OpenShiftRequest(
        REQUEST_ID, "Denver", "RN", "procedure-qualified", TimeWindow(at(9, 10), at(9, 20)), 1,
    )
    evidence = StaffScheduleEvidence(published, (callout, revised), worked, ((REQUEST_ID, revised.assignments[-1]),))
    return evidence, request


class StaffProviderSchemaTest(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator = contract_validator()

    def example_messages(self):
        scope = {"location": "Denver", "starts_at": at(9, 0).isoformat(), "ends_at": at(9, 30).isoformat()}
        workers = {"records": [{
            "staff_id": "float-rn",
            "roles": ["RN"],
            "skills": [{"code": "procedure-qualified", "level": "independent"}],
            "certifications": [{"code": "procedure-qualified", "expires_on": "2027-01-01"}],
        }]}
        leave = {"approved": [{
            "reference": "leave-1", "staff_id": "called-out-rn",
            "starts_at": at(9, 10).isoformat(), "ends_at": at(9, 20).isoformat(),
        }]}
        clock_events = {"records": [
            {"reference": "clock-1", "staff_id": "float-rn", "at": at(9, 12).isoformat(), "direction": "in"},
            {"reference": "clock-2", "staff_id": "float-rn", "at": at(9, 20).isoformat(), "direction": "out"},
        ]}
        evidence, request = synthetic_staffing()
        return {
            **{
                kind: {"contract": CONTRACT, "kind": kind, "data": {**scope, **feed}}
                for kind, feed in (("workers", workers), ("leave", leave), ("clock_events", clock_events))
            },
            "staffing": evidence.as_provider_message(SERIES),
            "open_shift_request": request.as_provider_message(),
            "open_shift_state": evidence.open_shift_message(request),
        }

    def test_accepts_synthetic_examples_for_every_message_kind(self):
        messages = self.example_messages()
        required_kinds = {variant["properties"]["kind"]["const"] for variant in self.validator.schema["oneOf"]}
        self.assertEqual(set(messages), required_kinds)
        for kind, message in messages.items():
            with self.subTest(kind=kind):
                validate_provider_message(self.validator, message)

    def test_rejects_patient_fields_at_the_boundary(self):
        for kind, message in self.example_messages().items():
            with self.subTest(kind=kind):
                message["data"]["patient_id"] = "synthetic-should-remain-in-openpractice"
                with self.assertRaises(ValidationError):
                    validate_provider_message(self.validator, message)

    def test_rejects_wrong_version_invalid_time_and_unconfirmed_roster_reference(self):
        messages = self.example_messages()
        request = messages["open_shift_request"]
        state = messages["open_shift_state"]
        bad_cases = (
            {**request, "contract": "openpractice-staff-schedule-provider/2.0"},
            {**request, "data": {**request["data"], "starts_at": "2026-11-26T09:10:00"}},
            {**request, "data": {**request["data"], "correlation_id": "procedure-1"}},
            {**request, "data": {**request["data"], "headcount": 0}},
            {**state, "data": {**state["data"], "status": "pending"}},
            {**state, "data": {**state["data"], "roster_reference": None}},
        )
        for index, message in enumerate(bad_cases):
            with self.subTest(index=index), self.assertRaises(ValidationError):
                validate_provider_message(self.validator, message)

    def test_rejects_reversed_windows_and_events_outside_the_query(self):
        messages = self.example_messages()
        request = messages["open_shift_request"]
        request["data"]["ends_at"] = at(9, 9).isoformat()
        clock = messages["clock_events"]
        clock["data"]["records"][0]["at"] = clock["data"]["ends_at"]
        leave = messages["leave"]
        leave["data"]["approved"][0]["starts_at"] = at(9, 31).isoformat()
        leave["data"]["approved"][0]["ends_at"] = at(9, 32).isoformat()
        for message in (request, clock, leave):
            with self.subTest(kind=message["kind"]), self.assertRaises(ValueError):
                validate_provider_message(self.validator, message)


    def test_requires_current_qualified_coverage_and_preserves_published_history(self):
        evidence, request = synthetic_staffing()
        conformance = StaffingConformance(self, SERIES)
        conformance.observe(evidence)
        conformance.expect_open_shift(evidence, request, "confirmed")

        for alternate in (
            replace(request, role="LPN"),
            replace(request, qualification="general"),
            replace(request, headcount=2),
            replace(request, window=TimeWindow(at(9, 10), at(9, 21))),
        ):
            conformance.expect_open_shift(evidence, alternate, "pending")

        callout_only = replace(evidence, revisions=evidence.revisions[:1], attendance=None)
        conformance.expect_open_shift(callout_only, request, "pending")
        with self.assertRaises(AssertionError):
            conformance.observe(callout_only)
        rewritten = replace(evidence, published=replace(evidence.published, reason="rewritten"))
        with self.assertRaises(AssertionError):
            conformance.observe(rewritten)

        mismatched_attendance = replace(evidence.attendance, roster_reference=evidence.published.reference)
        with self.assertRaises(AssertionError):
            conformance.observe(replace(evidence, attendance=mismatched_attendance))
