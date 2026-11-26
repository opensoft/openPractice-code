from copy import deepcopy
from datetime import datetime
from unittest import TestCase

from claimlab.x837p import parse, validate
from open_practice.billing import build_test_batch


NOW = datetime(2026, 11, 26, 10, 30)
SETTINGS = {
    "submitter_id": "DEMOCO01",
    "submitter_name": "EXAMPLE BILLING",
    "contact_name": "BILLING TEST",
    "contact_phone": "3035550100",
    "provider_type": "Organization",
    "provider_name": "EXAMPLE COLORADO PRACTICE",
    "billing_npi": "1234567893",
    "tax_qualifier": "EI",
    "tax_id": "123456789",
    "address": "10 EXAMPLE ST",
    "city": "DENVER",
    "state": "CO",
    "postal_code": "802010001",
    "rendering_first_name": "TEST",
    "rendering_last_name": "CLINICIAN",
    "rendering_npi": "1111111111",
}


def sample_visit(number=1):
    return {
        "name": f"OPV-{number:09d}",
        "service_date": "2026-11-20",
        "patient_first_name": "EXAMPLE",
        "patient_last_name": f"PATIENT{number}",
        "date_of_birth": "1950-01-01",
        "administrative_sex": "F",
        "address": "20 EXAMPLE AVE",
        "city": "DENVER",
        "state": "CO",
        "postal_code": "80201",
        "member_id": f"SYNTH{number:06d}",
        "coverage_verified": 1,
        "diagnoses": "E11.9, I10",
        "charges": [
            {"procedure_code": "99213", "charge": "149.90", "units": 1, "diagnosis_pointers": "1,2"},
            {"procedure_code": "G0008", "charge": "0.10", "units": 1, "diagnosis_pointers": "2"},
        ],
    }


class BillingTest(TestCase):
    def test_colorado_test_file_matches_the_visits(self):
        visits = [sample_visit(number) for number in range(1, 26)]
        text = build_test_batch(SETTINGS, visits, control_number=121, now=NOW).decode("ascii")
        segments = parse(text)
        claim_segments = [segment for segment in segments if segment[0] == "CLM"]
        payer_segments = [segment for segment in segments if segment[:2] == ["NM1", "PR"]]
        self.assertEqual(len(claim_segments), 25)
        self.assertEqual({segment[1] for segment in claim_segments}, {visit["name"] for visit in visits})
        self.assertEqual({segment[2] for segment in claim_segments}, {"150"})
        self.assertEqual({segment[9] for segment in payer_segments}, {"04112"})
        self.assertEqual(segments[0][13:16], ["000000121", "1", "T"])
        self.assertEqual(segments[0][7:9], ["27", "04112          "])
        self.assertEqual(segments[1][3], "04112")
        self.assertEqual(segments[-1][2], "000000121")
        self.assertIn("SV1*HC:99213*149.9*UN*1***1:2~", text)
        self.assertIn("NM1*82*1*CLINICIAN*TEST****XX*1111111111~", text)

    def test_novitas_edits_reject_malformed_test_files(self):
        text = build_test_batch(SETTINGS, [sample_visit()], control_number=121, now=NOW).decode("ascii")
        for original, changed, expected in [
            ("*27*04112", "*99*04112", "ISA05 and ISA07"),
            ("*1*T*:~", "*1*P*:~", "ISA15=T"),
            ("005010X222A1", "005010X222", "implementation version"),
            ("*2*1*22*0~", "*2*9*22*0~", "billing provider or its self subscriber"),
            ("*2*1*22*0~", "*2*1*23*0~", "billing provider or its self subscriber"),
            ("*P*18*", "*P*19*", "a self subscriber"),
            ("OPV-000000001*", "EXAMPLE-CLAIM-00001*", "CLM01"),
            ("*UN*1***1:2", "*UN*01***1:2", "without leading zeros"),
            ("*PI*04112~", "*PI*04112 ~", "trailing space"),
        ]:
            with self.subTest(expected=expected):
                self.assertTrue(any(expected in error for error in validate(text.replace(original, changed), novitas=True)))

    def test_visit_scrub_holds_invalid_inputs(self):
        cases = [
            ("coverage_verified", 0, "Verify primary"),
            ("service_date", "2027-01-01", "Service date"),
            ("charges", [], "At least one service"),
            ("member_id", "BAD~VALUE", "member id"),
        ]
        for field, value, error in cases:
            with self.subTest(field=field):
                visit = sample_visit()
                visit[field] = value
                with self.assertRaisesRegex(ValueError, error):
                    build_test_batch(SETTINGS, [visit], control_number=1, now=NOW)

        for field, value, error in [
            ("charge", "149.901", "amounts in cents"),
            ("diagnosis_pointers", "3", "distinct diagnoses"),
            ("units", 0, "positive whole number"),
        ]:
            with self.subTest(field=field):
                visit = deepcopy(sample_visit())
                visit["charges"][0][field] = value
                with self.assertRaisesRegex(ValueError, error):
                    build_test_batch(SETTINGS, [visit], control_number=1, now=NOW)

    def test_batch_requires_colorado_and_distinct_visits(self):
        visit = sample_visit()
        with self.assertRaisesRegex(ValueError, "appear once"):
            build_test_batch(SETTINGS, [visit, visit], control_number=1, now=NOW)
        with self.assertRaisesRegex(ValueError, "Colorado billing"):
            build_test_batch({**SETTINGS, "state": "PR"}, [visit], control_number=1, now=NOW)
