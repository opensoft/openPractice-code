from copy import deepcopy
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from claimlab.x837p import parse, validate
from open_practice.billing import build_test_batch, prepare_test_batch, validate_test_batch
from open_practice.charting import read_billing_facts
from open_practice.frozen_837p import freeze_837p, render_frozen_837p
from open_practice.mock_openchart import desk_encounter_reference, read_mock_billing_facts, read_mock_response
from open_practice.test_subscriber_roster import load_approved_synthetic_roster


NOW = datetime(2026, 11, 26, 10, 30)
SYNTHETIC_ROSTER_CSV = (
    "subscriber_ref,patient_first_name,patient_last_name,date_of_birth,administrative_sex,"
    "address,city,state,postal_code,member_id\n"
    "BILLING-TEST-000001,EXAMPLE,ROSTERTEST,1950-01-01,F,20 EXAMPLE AVE,DENVER,CO,80201,1EG4TE5MK73\n"
)
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
FLU_SETTINGS = {**SETTINGS, "provider_name": "MEDS RX INC"}
SYNTHETIC_ALF = {
    "service_facility_name": "EXAMPLE ASSISTED LIVING",
    "service_facility_address": "100 DEMO ALF WAY",
    "service_facility_address_2": "BUILDING B",
    "service_facility_city": "DENVER",
    "service_facility_state": "CO",
    "service_facility_postal_code": "80224-0123",
    "service_facility_npi": "1999999992",
}


def sample_case(number=1):
    encounter_ref = f"OC-MOCK-{number:06d}"
    facts = read_mock_billing_facts(encounter_ref)
    return {
        "name": f"OPC-{number:09d}",
        "chart_encounter_ref": encounter_ref,
        "subscriber_ref": facts.subscriber_ref,
        "chart_revision": facts.revision,
        "coverage_verified": 1,
        "charges": [
            {"procedure_code": "99213", "charge": "149.90", "units": 1, "diagnosis_pointers": "1,2"},
            {"procedure_code": "G0008", "charge": "0.10", "units": 1, "diagnosis_pointers": "2"},
        ],
    }


def sample_flu_case(place_of_service):
    encounter_ref = "OC-MOCK-FLU-000001"
    facts = read_mock_billing_facts(encounter_ref)
    return {
        "name": "OPC-000000001",
        "chart_encounter_ref": encounter_ref,
        "subscriber_ref": facts.subscriber_ref,
        "chart_revision": facts.revision,
        "coverage_verified": 1,
        "place_of_service": place_of_service,
        "charges": [{"procedure_code": "G0008", "charge": "25.00", "units": 1, "diagnosis_pointers": "1"}],
    }


class BillingTest(TestCase):
    def test_ready_demo_office_appointment_freezes_its_booked_service_date(self):
        appointment = {
            "patient": "demo-02", "location": "clinic", "mode": "in-person",
            "stage": "wrapped", "day": "2026-11-27", "slot": "10:15",
        }
        reference = desk_encounter_reference(appointment)
        self.assertEqual(reference, "OC-MOCK-DESK-000002-20261127T1015")
        facts = read_mock_billing_facts(reference)
        self.assertEqual(facts.subscriber_ref, "BILLING-TEST-000002")
        self.assertEqual(facts.service_date.isoformat(), "2026-11-27")
        self.assertEqual(facts.diagnoses, ("E11.9", "I10"))

        case = {
            **sample_case(2), "chart_encounter_ref": reference, "chart_revision": facts.revision,
            "charges": [{"procedure_code": "99213", "charge": "149.90", "units": 1, "diagnosis_pointers": "1,2"}],
        }
        now = NOW.replace(day=27)
        data = prepare_test_batch(SETTINGS, [case], control_number=121, now=now)
        billing, restricted = freeze_837p(data, control_number=121, now=now)
        reconstructed = render_frozen_837p(billing, restricted)
        validate_test_batch(reconstructed)
        self.assertEqual(reconstructed, build_test_batch(SETTINGS, [case], control_number=121, now=now))
        segments = parse(reconstructed.decode("ascii"))
        self.assertEqual(segments[0][15], "T")
        self.assertIn(["DTP", "472", "D8", "20261127"], segments)
        self.assertIn(["HI", "ABK:E119", "ABF:I10"], segments)

        for changed in ({"stage": "ended"}, {"patient": "patient-" + "a" * 32},
                        {"mode": "telemedicine"}, {"location": "home-" + "a" * 32}):
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, "wrapped in-person"):
                desk_encounter_reference({**appointment, **changed})

    def test_colorado_test_file_matches_the_billing_cases(self):
        cases = [sample_case(number) for number in range(1, 26)]
        text = build_test_batch(SETTINGS, cases, control_number=121, now=NOW).decode("ascii")
        segments = parse(text)
        claim_segments = [segment for segment in segments if segment[0] == "CLM"]
        payer_segments = [segment for segment in segments if segment[:2] == ["NM1", "PR"]]
        self.assertEqual(len(claim_segments), 25)
        self.assertEqual({segment[1] for segment in claim_segments}, {case["name"] for case in cases})
        self.assertEqual({segment[2] for segment in claim_segments}, {"150"})
        self.assertEqual({segment[9] for segment in payer_segments}, {"04112"})
        self.assertEqual(segments[0][13:16], ["000000121", "1", "T"])
        self.assertEqual(segments[0][7:9], ["27", "04112          "])
        self.assertEqual(segments[1][3], "04112")
        self.assertEqual(segments[-1][2], "000000121")
        self.assertIn("SV1*HC:99213*149.9*UN*1***1:2~", text)
        self.assertIn("NM1*82*1*CLINICIAN*TEST****XX*1111111111~", text)
        self.assertEqual({segment[5] for segment in claim_segments}, {"11:B:1"})

    def test_flu_administration_uses_chart_diagnosis_and_selected_place_of_service(self):
        for pos in ("01", "60"):
            with self.subTest(place_of_service=pos):
                payload = build_test_batch(FLU_SETTINGS, [sample_flu_case(pos)], control_number=121, now=NOW)
                segments = parse(payload.decode("ascii"))
                self.assertEqual([segment[5] for segment in segments if segment[0] == "CLM"], [f"{pos}:B:1"])
                self.assertEqual([segment[1:] for segment in segments if segment[0] == "HI"], [["ABK:Z23"]])
                self.assertEqual([segment[1:] for segment in segments if segment[0] == "SV1"],
                                 [["HC:G0008", "25", "UN", "1", "", "", "1"]])
                self.assertIn(b"NM1*85*2*MEDS RX INC*", payload)
                self.assertIn(b"*PI*04112~", payload)
                self.assertEqual(segments[0][15], "T")
                self.assertNotIn(b"NM1*77*", payload)

    def test_flu_administration_exports_2310c_with_optional_facility_npi(self):
        for npi in (SYNTHETIC_ALF["service_facility_npi"], ""):
            with self.subTest(facility_npi_supplied=bool(npi)):
                case = {**sample_flu_case("60"), **SYNTHETIC_ALF, "service_facility_npi": npi}
                payload = build_test_batch(FLU_SETTINGS, [case], control_number=121, now=NOW)
                segments = parse(payload.decode("ascii"))
                rendering_index = next(i for i, row in enumerate(segments) if row[:2] == ["NM1", "82"])
                facility_name = ["NM1", "77", "2", "EXAMPLE ASSISTED LIVING"]
                if npi:
                    facility_name += ["", "", "", "", "XX", npi]
                self.assertEqual(segments[rendering_index + 1:rendering_index + 5], [
                    facility_name,
                    ["N3", "100 DEMO ALF WAY", "BUILDING B"],
                    ["N4", "DENVER", "CO", "802240123"],
                    ["LX", "1"],
                ])
                self.assertIn(b"N3*10 EXAMPLE ST~N4*DENVER*CO*802010001~REF*EI*", payload)
                self.assertIn(b"N3*20 EXAMPLE AVE~N4*DENVER*CO*80201~DMG*", payload)

    def test_service_facility_rejects_incomplete_or_invalid_values(self):
        for change, error in [
            ({"service_facility_name": ""}, "service facility name"),
            ({"service_facility_address": "P.O. BOX 200"}, "physical street address"),
            ({"service_facility_postal_code": "80224"}, "service facility postal code"),
            ({"service_facility_npi": "123456789"}, "service facility npi"),
            ({"service_facility_npi": "1999999993"}, "service facility NPI has an invalid check digit"),
        ]:
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                case = {**sample_flu_case("60"), **SYNTHETIC_ALF, **change}
                build_test_batch(FLU_SETTINGS, [case], control_number=121, now=NOW)

    def test_flu_administration_rejects_missing_pos_and_other_service_lines(self):
        for pos in ("", "11"):
            with self.subTest(place_of_service=pos), self.assertRaisesRegex(ValueError, "place of service 01 or 60"):
                build_test_batch(FLU_SETTINGS, [sample_flu_case(pos)], control_number=121, now=NOW)

        for change in ({"procedure_code": "90662"}, {"procedure_code": "99213"}, {"units": 2}):
            with self.subTest(line_change=change):
                case = sample_flu_case("60")
                case["charges"][0].update(change)
                with self.assertRaisesRegex(ValueError, "single G0008 line"):
                    build_test_batch(FLU_SETTINGS, [case], control_number=121, now=NOW)

    def test_novitas_edits_reject_malformed_test_files(self):
        text = build_test_batch(SETTINGS, [sample_case()], control_number=121, now=NOW).decode("ascii")
        for original, changed, expected in [
            ("*27*04112", "*99*04112", "ISA05 and ISA07"),
            ("*1*T*:~", "*1*P*:~", "ISA15=T"),
            ("005010X222A1", "005010X222", "implementation version"),
            ("*2*1*22*0~", "*2*9*22*0~", "billing provider or its self subscriber"),
            ("*2*1*22*0~", "*2*1*23*0~", "billing provider or its self subscriber"),
            ("*P*18*", "*P*19*", "a self subscriber"),
            ("OPC-000000001*", "EXAMPLE-CLAIM-00001*", "CLM01"),
            ("*UN*1***1:2", "*UN*01***1:2", "without leading zeros"),
            ("*PI*04112~", "*PI*04112 ~", "trailing space"),
        ]:
            with self.subTest(expected=expected):
                self.assertTrue(any(expected in error for error in validate(text.replace(original, changed), novitas=True)))

    def test_billing_scrub_holds_invalid_inputs(self):
        cases = [
            ("coverage_verified", 0, "Verify primary"),
            ("charges", [], "At least one service"),
            ("chart_revision", "STALE", "handoff changed"),
            ("chart_encounter_ref", "UNKNOWN", "mock openChart encounter"),
        ]
        for field, value, error in cases:
            with self.subTest(field=field):
                case = sample_case()
                case[field] = value
                with self.assertRaisesRegex(ValueError, error):
                    build_test_batch(SETTINGS, [case], control_number=1, now=NOW)

        for field, value, error in [
            ("charge", "149.901", "amounts in cents"),
            ("diagnosis_pointers", "3", "distinct diagnoses"),
            ("units", 0, "positive whole number"),
        ]:
            with self.subTest(field=field):
                case = deepcopy(sample_case())
                case["charges"][0][field] = value
                with self.assertRaisesRegex(ValueError, error):
                    build_test_batch(SETTINGS, [case], control_number=1, now=NOW)

    def test_batch_requires_colorado_and_distinct_cases(self):
        case = sample_case()
        with self.assertRaisesRegex(ValueError, "appear once"):
            build_test_batch(SETTINGS, [case, case], control_number=1, now=NOW)
        with self.assertRaisesRegex(ValueError, "Colorado billing"):
            build_test_batch({**SETTINGS, "state": "PR"}, [case], control_number=1, now=NOW)

    def test_export_uses_chart_diagnosis_and_subscriber_roster(self):
        case = {**sample_case(), "diagnoses": "Z00.0", "member_id": "OVERRIDE001"}
        text = build_test_batch(SETTINGS, [case], control_number=1, now=NOW).decode("ascii")
        self.assertIn("HI*ABK:E119*ABF:I10~", text)
        self.assertIn("*MI*SYNTH000001~", text)
        self.assertNotIn("OVERRIDE001", text)
        self.assertNotIn("Z000", text)

    def test_approved_roster_requires_valid_rows_and_never_falls_back(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "subscriber-roster.csv"
            with self.assertRaisesRegex(ValueError, "readable UTF-8 CSV"):
                load_approved_synthetic_roster(path, "LOCAL-E2E")
            for csv_text, error in [
                ("name,identifier\n", "headers"),
                (SYNTHETIC_ROSTER_CSV.replace("1EG4TE5MK73", "SYNTH000001"), "invalid MBI"),
                (SYNTHETIC_ROSTER_CSV + SYNTHETIC_ROSTER_CSV.splitlines()[1], "duplicate reference"),
            ]:
                with self.subTest(error=error):
                    path.write_text(csv_text, encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, error):
                        load_approved_synthetic_roster(path, "LOCAL-E2E")
            path.write_text(SYNTHETIC_ROSTER_CSV, encoding="utf-8")
            roster = load_approved_synthetic_roster(path, "LOCAL-E2E")

        for case, supplied_roster, error in [
            (sample_case(), roster, "approved against the current synthetic test roster"),
            ({**sample_case(), "synthetic_roster_sha256": roster.sha256}, None, "approved against the current synthetic test roster"),
            ({**sample_case(2), "synthetic_roster_sha256": roster.sha256}, roster, "no matching billing subscriber"),
        ]:
            with self.subTest(error=error):
                with self.assertRaisesRegex(ValueError, error):
                    build_test_batch(SETTINGS, [case], control_number=1, now=NOW, roster=supplied_roster)

    def test_export_holds_when_chart_facts_are_withdrawn(self):
        case = sample_case()
        response = read_mock_response(case["chart_encounter_ref"])
        response["payload"] = {"result": "no_billable_facts"}
        with patch("open_practice.mock_openchart.read_mock_response", return_value=response):
            with self.assertRaisesRegex(ValueError, "finalized, signed billing facts"):
                build_test_batch(SETTINGS, [case], control_number=1, now=NOW)

    def test_handoff_rejects_extra_clinical_content(self):
        ref = "OC-MOCK-000001"
        response = read_mock_response(ref)
        response["payload"]["intake"] = {}
        with self.assertRaisesRegex(ValueError, "only the diagnosis billing subset"):
            read_billing_facts(ref, response)
