"""Verify the reusable QA roster and its configuration loader."""

import json
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from open_practice.tests.qa_staffing import QA_STAFF, T13_STAFFING, load_qa_run


class QAStaffingTest(TestCase):
    def test_roster_config_supports_all_eleven_identities(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "staffing.json"
            path.write_text(json.dumps({
                staff_id: {
                    "specialty": f"{role.lower()}-qa",
                    "assignment": "float" if staff_id.endswith("-02") else "scheduled",
                }
                for staff_id, role in QA_STAFF.items()
            }), encoding="utf-8")
            run = load_qa_run(path)

        self.assertEqual(Counter(row.role for row in run.values()),
                         {"VA": 2, "LVN": 2, "RN": 3, "NP": 2, "MD": 2})
        self.assertEqual(run["LVN-02"].assignment, "float")
        self.assertEqual(run["RN-03"].assignment, "scheduled")
        self.assertEqual(run["NP-01"].specialty, "np-qa")
        self.assertEqual(load_qa_run(T13_STAFFING)["RN-03"].assignment, "float")

    def test_invalid_staff_ids_and_assignments_fail(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "staffing.json"
            for staff_id, assignment, error in (
                ("RN-04", "float", "Unknown synthetic staff ID"),
                ("RN-03", "floating", "assignment must be scheduled or float"),
            ):
                with self.subTest(staff_id=staff_id, assignment=assignment):
                    path.write_text(json.dumps({
                        staff_id: {"specialty": "procedure-qualified", "assignment": assignment},
                    }), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, error):
                        load_qa_run(path)
