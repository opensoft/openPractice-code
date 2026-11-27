"""Reusable conformance assertions for any staff-schedule provider adapter."""

import json
import os
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator


def contract_validator():
    directory = os.environ.get("CONTRACTS_DIR")
    if not directory:
        raise RuntimeError("Set CONTRACTS_DIR to the openPractice-spec contracts directory.")
    path = Path(directory) / "staff-schedule-provider" / "1.0" / "schema.json"
    with path.open(encoding="utf-8") as source:
        schema = json.load(source)
    Draft202012Validator.check_schema(schema)
    checker = Draft202012Validator.FORMAT_CHECKER
    if "date-time" not in checker.checkers:
        raise RuntimeError("Install the conformance test requirements to validate timestamps.")
    return Draft202012Validator(schema, format_checker=checker)


def _require_interval(window):
    start = datetime.fromisoformat(window["starts_at"])
    end = datetime.fromisoformat(window["ends_at"])
    if end <= start:
        raise ValueError("A provider interval must end after it starts.")
    return start, end


def validate_provider_message(validator, message):
    """Check message shape and the time and identity rules for each feed."""
    validator.validate(message)
    kind, data = message["kind"], message["data"]
    if kind == "staffing":
        snapshots = (data["published"], *data["revisions"])
        recorded = [datetime.fromisoformat(snapshot["recorded_at"]) for snapshot in snapshots]
        if recorded != sorted(recorded):
            raise ValueError("Roster revisions must be in provider timestamp order.")
        for snapshot in snapshots:
            for assignment in snapshot["assignments"]:
                _require_interval(assignment)
        if data["attendance"] is not None:
            for interval in data["attendance"]["worked_intervals"]:
                _require_interval(interval)
        for link in data["coverage_links"]:
            _require_interval(link["assignment"])
        return
    if kind == "open_shift_state":
        _require_interval(data["request"])
        return

    start, end = _require_interval(data)
    if kind == "leave":
        for leave in data["approved"]:
            leave_start, leave_end = _require_interval(leave)
            if leave_end <= start or leave_start >= end:
                raise ValueError("An approved leave interval must overlap the requested interval.")
    if kind == "clock_events":
        for event in data["records"]:
            if not start <= datetime.fromisoformat(event["at"]) < end:
                raise ValueError("A clock event must fall inside the requested interval.")


class StaffingConformance:
    """Validate canonical messages and preserve the roster history across reads."""

    def __init__(self, test, roster_series):
        self.test = test
        self.roster_series = roster_series
        self.validator = contract_validator()
        self._history = ()

    def read(self, adapter):
        return self.observe(adapter.get_staffing(self.roster_series))

    def observe(self, evidence):
        message = evidence.as_provider_message(self.roster_series)
        validate_provider_message(self.validator, message)
        data = message["data"]
        snapshots = (data["published"], *data["revisions"])
        references = {snapshot["reference"] for snapshot in snapshots}
        self.test.assertEqual(len(references), len(snapshots))
        self.test.assertEqual(snapshots[:len(self._history)], self._history)

        current = snapshots[-1]
        if data["attendance"] is not None:
            self.test.assertEqual(data["attendance"]["roster_reference"], current["reference"])
        if data["coverage_links"]:
            self.test.assertTrue(data["revisions"])
        for link in data["coverage_links"]:
            self.test.assertIn(link["assignment"], current["assignments"])
        self._history = snapshots
        return evidence

    def expect_open_shift(self, evidence, request, expected_status):
        posted = request.as_provider_message()
        state = evidence.open_shift_message(request)
        validate_provider_message(self.validator, posted)
        validate_provider_message(self.validator, state)
        self.test.assertEqual(state["data"]["request"], posted["data"])
        self.test.assertEqual(state["data"]["status"], expected_status)
        reference = evidence.current.reference if expected_status == "confirmed" else None
        self.test.assertEqual(state["data"]["roster_reference"], reference)
