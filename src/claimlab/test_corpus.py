import unittest
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

from .corpus import CLAIM_FAULTS, bulk_batches, generate, verify


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
        for filename, _, cases, file_fault in bulk_batches(837):
            self.assertEqual(len(cases), 100)
            self.assertIsNone(file_fault)
            for case in cases:
                self.assertNotIn(case.claim_id, claim_ids)
                claim_ids.add(case.claim_id)
                counts[case.member_id] += 1
                if case.fault:
                    rejects[case.member_id] += 1
                    reasons[case.fault] += 1
        self.assertEqual(len(claim_ids), 35_000)
        self.assertEqual(Counter(counts.values()), {100: 250, 1_000: 10})
        self.assertEqual(sum(rejects.values()), 10_500)
        self.assertEqual(reasons, {fault: 2_625 for fault in CLAIM_FAULTS})
        for member, claim_count in counts.items():
            self.assertEqual(rejects[member], claim_count * 3 // 10)

    def test_bulk_seed_reproduces_claim_order_charges_and_faults(self):
        first = next(bulk_batches(837))
        self.assertEqual(first, next(bulk_batches(837)))
        self.assertNotEqual(first, next(bulk_batches(838)))

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
