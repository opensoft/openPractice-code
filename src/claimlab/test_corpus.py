import csv
import io
import json
import unittest
from collections import Counter
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from .carrier_profile import CarrierProfile, build_profile, load_profile
from .corpus import CLAIM_FAULTS, bulk_batches, generate, render_batch, verify


class CorpusTest(unittest.TestCase):
    def test_regression_suite_checks_ta1_file_and_claim_outcomes(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "regression"
            manifest = generate(root, suite="regression")
            summary = verify(root)["regression"]
            self.assertEqual((summary["files"], summary["claims"]), (11, 15))
            self.assertEqual((summary["accepted"], summary["rejected"], summary["blocked"]), (5, 4, 6))
            stages = Counter()
            for entry in manifest["files"]:
                acks = entry["acknowledgments"]
                if acks["TA1"]["status"] == "rejected":
                    stages["TA1"] += 1
                    self.assertEqual(acks["999"]["status"], "not_generated")
                elif acks["999"]["status"] == "rejected":
                    stages["999"] += 1
                    self.assertEqual(acks["277CA"]["status"], "not_generated")
                elif acks["277CA"]["status"] == "mixed":
                    stages["277CA"] += 1
                    self.assertEqual([claim["status"] for claim in entry["claims"]], ["accepted", "rejected"])
            self.assertEqual(stages, {"TA1": 2, "999": 4, "277CA": 4})

    def test_bulk_has_exact_totals_and_fault_balance(self):
        counts = Counter()
        rejects = Counter()
        reasons = Counter()
        claim_ids = set()
        line_counts = Counter()
        codes = set()
        locations = set()
        profile = load_profile()
        for filename, _, cases, file_fault in bulk_batches(837):
            self.assertEqual(len(cases), 100)
            self.assertIsNone(file_fault)
            for case in cases:
                self.assertNotIn(case.claim_id, claim_ids)
                claim_ids.add(case.claim_id)
                counts[case.member_id] += 1
                line_counts[len(case.lines)] += 1
                procedures = [line["cpt"] for line in case.lines]
                codes.update(procedures)
                locations.add(case.pos)
                self.assertIn((procedures[0], case.pos), profile.pairs)
                if len(procedures) > 1:
                    patterns = profile.patterns[procedures[0], case.pos][len(procedures)]
                    self.assertIn(sorted(procedures), [list(pattern) for pattern, _ in patterns])
                if case.fault:
                    rejects[case.member_id] += 1
                    reasons[case.fault] += 1
        self.assertEqual(len(claim_ids), 35_000)
        self.assertIn(1, line_counts)
        self.assertGreaterEqual(max(line_counts), 3)
        self.assertTrue({"11", "22", "81"} <= locations)
        self.assertTrue({"99213", "36415", "85025"} <= codes)
        self.assertEqual(Counter(counts.values()), {100: 250, 1_000: 10})
        self.assertEqual(sum(rejects.values()), 10_500)
        self.assertEqual(reasons, {fault: 2_625 for fault in CLAIM_FAULTS})
        for member, claim_count in counts.items():
            self.assertEqual(rejects[member], claim_count * 3 // 10)

    def test_bulk_seed_reproduces_claim_order_charges_and_faults(self):
        first = next(bulk_batches(837))
        self.assertEqual(first, next(bulk_batches(837)))
        self.assertNotEqual(first, next(bulk_batches(838)))

    def test_bulk_renders_office_services_from_the_packaged_profile(self):
        _, control, cases, fault = next(bulk_batches(837))
        payload = render_batch(control, cases, fault).decode("ascii")
        self.assertIn("NM1*85*2*CLAIMLAB DEMO CLINIC", payload)
        self.assertIn("*11:B:1*", payload)
        self.assertIn("HI*ABK:E119~", payload)
        self.assertIn("SV1*HC:99213*", payload)
        self.assertEqual(load_profile().reference["profile"], "cms-carrier-combined-1")

    def test_carrier_extraction_joins_priced_patterns_with_weighted_pos_counts(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "carrier.zip"
            csv_data = io.StringIO()
            writer = csv.writer(csv_data)
            fields = ["DESYNPUF_ID"]
            for slot in range(1, 14):
                fields.extend([f"HCPCS_CD_{slot}", f"LINE_ALOWD_CHRG_AMT_{slot}"])
            writer.writerow(fields)
            for _ in range(10):
                writer.writerow([
                    "DO-NOT-COPY", "99213", "44.00", "85025", "8.40", "J1234", "100.00",
                    "80053", "0.00", "", "5.00", *(["", "0.00"] * 8),
                ])
            with ZipFile(path, "w") as archive:
                archive.writestr("carrier.csv", csv_data.getvalue())
            bsa_2010 = Path(temporary) / "2010.zip"
            bsa_2008 = Path(temporary) / "2008.zip"
            with ZipFile(bsa_2010, "w") as archive:
                archive.writestr("carrier.csv", (
                    "CAR_LINE_HCPCS_CD,CAR_LINE_PLACE_OF_SRVC_CD,CAR_LINE_CNT\n"
                    "99213,11,40\n99213,21,80\n85025,11,60\n80053,11,20\n99213,1,50\n"
                ))
            with ZipFile(bsa_2008, "w") as archive:
                archive.writestr("carrier.csv", (
                    "CAR_LINE_ID,CAR_LINE_HCPCS_CD,CAR_LINE_PLACE_OF_SRVC_CD\n"
                    "DO-NOT-COPY-2008,99213,11\n"
                ))
            profile = build_profile([path], [bsa_2010, bsa_2008])
        self.assertEqual(profile["priced_line_counts"], [(3, 10)])
        self.assertEqual(profile["office_lab_patterns"], [[["85025", "99213"], 10]])
        self.assertEqual(profile["allowed_charge_bands"], {"85025": [(1, 10)], "99213": [(8, 10)]})
        self.assertEqual(profile["procedure_pos_counts"], [
            ["85025", "11", 60], ["99213", "11", 41], ["99213", "21", 80],
        ])
        volumes = [(source["rows"], source["lines"]) for source in profile["frequency_archives"]]
        self.assertEqual(volumes, [(1, 1), (5, 250)])
        self.assertNotIn("DO-NOT-COPY", json.dumps(profile))

        sampler = CarrierProfile(profile, "fixture-digest")
        rng = Random(837)
        observed = Counter()
        for _ in range(1_000):
            pos, lines = sampler.service(rng, "20261120")
            procedures = [line["cpt"] for line in lines]
            observed[pos, tuple(sorted(procedures))] += 1
        self.assertEqual(set(observed), {("11", ("85025", "99213")), ("21", ("99213",))})
        self.assertGreater(observed["11", ("85025", "99213")], observed["21", ("99213",)])

    def test_verifier_detects_changed_fixture_and_generation_refuses_overwrite(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "corpus"
            generate(root, suite="regression")
            with self.assertRaises(FileExistsError):
                generate(root, suite="bulk")
            fixture = root / "regression" / "accepted.837"
            fixture.write_bytes(fixture.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "file digest differs"):
                verify(root)


if __name__ == "__main__":
    unittest.main()
