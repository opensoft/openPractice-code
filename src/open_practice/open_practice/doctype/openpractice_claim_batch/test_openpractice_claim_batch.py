import json
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime

from claimlab import x837p
from open_practice.billing import build_test_batch
from open_practice.open_practice.doctype.openpractice_claim_batch.openpractice_claim_batch import (
    download_837p,
)
from open_practice.site import load_site_roster
from open_practice.tests.test_billing import FLU_SETTINGS, SYNTHETIC_ALF, SYNTHETIC_ROSTER_CSV, sample_flu_case


class TestOpenPracticeClaimBatch(FrappeTestCase):
    def test_releases_frozen_submission_and_reconstructs_after_live_data_and_exporter_change(self):
        roster_path = Path(frappe.get_site_path("private", "openpractice-novitas-synthetic-roster.csv"))
        with roster_path.open("x", encoding="utf-8") as roster_file:
            roster_file.write(SYNTHETIC_ROSTER_CSV)
        self.addCleanup(roster_path.unlink, missing_ok=True)
        roster_config = patch.dict(frappe.conf, {
            "open_practice_novitas_roster_mode": "approved-synthetic",
            "open_practice_novitas_roster_approval": "LOCAL-E2E",
        })
        roster_config.start()
        self.addCleanup(roster_config.stop)
        frappe.set_user("Administrator")
        self.addCleanup(frappe.set_user, "Administrator")
        settings = frappe.get_single("OpenPractice EDI Settings")
        settings.update({**FLU_SETTINGS, "first_interchange_control": 500_001})
        settings.save()
        biller = frappe.get_doc(
            doctype="User",
            email="openpractice-mvp-biller@example.com",
            first_name="Test Biller",
            user_type="System User",
            send_welcome_email=0,
            roles=[{"role": "OpenPractice Test Biller"}, {"role": "OpenPractice Billing Evidence"}],
        ).insert()
        reviewer = frappe.get_doc(
            doctype="User",
            email="openpractice-mvp-reviewer@example.com",
            first_name="Test Reviewer",
            user_type="System User",
            send_welcome_email=0,
            roles=[{"role": "OpenPractice Test Biller"}],
        ).insert()
        frappe.set_user(biller.name)

        values = {**sample_flu_case("60"), **SYNTHETIC_ALF}
        for field in ("name", "subscriber_ref", "chart_revision"):
            values.pop(field)
        case = frappe.get_doc(doctype="OpenPractice Billing Case", **values).insert()
        self.assertEqual(case.diagnosis_count, 1)
        for field in ("diagnoses", "member_id", "date_of_birth", "patient_first_name"):
            self.assertIsNone(case.meta.get_field(field))
        self.assertFalse(frappe.db.exists("DocType", "OpenPractice Visit"))
        case.submit()
        original_roster_digest = load_site_roster().sha256
        self.assertEqual(case.reload().synthetic_roster_sha256, original_roster_digest)
        rows = [{"billing_case": case.name}]
        batch = frappe.get_doc(doctype="OpenPractice Claim Batch", billing_cases=rows).insert()
        with self.assertRaises(frappe.ValidationError):
            download_837p(batch.name)
        revised_roster_csv = SYNTHETIC_ROSTER_CSV.replace("ROSTERTEST", "UPDATED")
        roster_path.write_text(revised_roster_csv, encoding="utf-8")
        with self.assertRaisesRegex(frappe.ValidationError, "approved against the current synthetic test roster"):
            batch.submit()
        self.assertFalse(batch.reload().phi_snapshot_ref)
        roster_path.write_text(SYNTHETIC_ROSTER_CSV, encoding="utf-8")
        batch.submit()
        self.assertEqual(case.reload().claim_batch, batch.name)
        self.assertEqual(batch.claim_count, 1)
        self.assertEqual(batch.billing_cases, [])
        self.assertIsNone(batch.meta.get_field("edi_payload"))
        for protected in (
            case.name, case.subscriber_ref, "ROSTERTEST", "1EG4TE5MK73", "Z23", "HC:G0008",
            "EXAMPLE ASSISTED LIVING", "100 DEMO ALF WAY", "802240123", "1999999992",
        ):
            self.assertNotIn(protected, batch.frozen_billing)
        self.assertEqual(json.loads(batch.frozen_billing)["synthetic_roster"], {
            "approval_reference": "LOCAL-E2E", "sha256": original_roster_digest,
        })
        self.assertRegex(batch.phi_snapshot_ref, r"^[0-9a-f]{32}$")
        snapshot = frappe.get_doc("OpenPractice Claim PHI Snapshot", batch.phi_snapshot_ref)
        self.assertEqual(snapshot.submission, batch.name)
        with self.assertRaises(frappe.PermissionError):
            snapshot.check_permission("read")

        download_837p(batch.name)
        payload = frappe.local.response["filecontent"]
        digest = sha256(payload).hexdigest()
        self.assertEqual(digest, batch.file_sha256)
        self.assertIn(f"CLM*{case.name}*25***60:B:1*".encode("ascii"), payload)
        self.assertIn(b"HI*ABK:Z23~", payload)
        self.assertIn(b"SV1*HC:G0008*25*UN*1***1~", payload)
        self.assertIn(b"NM1*85*2*MEDS RX INC*", payload)
        self.assertIn(b"NM1*IL*1*ROSTERTEST*EXAMPLE****MI*1EG4TE5MK73~", payload)
        self.assertIn(
            b"NM1*77*2*EXAMPLE ASSISTED LIVING*****XX*1999999992~"
            b"N3*100 DEMO ALF WAY*BUILDING B~N4*DENVER*CO*802240123~LX*1~",
            payload,
        )
        self.assertIn(b"*PI*04112~", payload)
        self.assertIn(b"*1*T*:", payload)

        duplicate = frappe.get_doc(doctype="OpenPractice Claim Batch", billing_cases=rows).insert()
        with self.assertRaises(frappe.ValidationError):
            duplicate.submit()

        batch.portal_reference = "TEST-TRANSMISSION-01"
        batch.file_ack = "Accepted"
        batch.save()
        self.assertEqual(batch.upload_file_sha256, digest)
        self.assertEqual(batch.portal_result, "Awaiting acknowledgments")
        batch.claim_ack = "Accepted"
        batch.save()
        self.assertEqual(batch.reload().portal_result, "Accepted")

        roster_path.write_text(revised_roster_csv, encoding="utf-8")
        new_roster = load_site_roster()
        new_case = {
            **case.as_dict(),
            "service_facility_address": "200 REVISED DEMO WAY",
            "service_facility_npi": "",
            "synthetic_roster_sha256": new_roster.sha256,
        }
        fresh = build_test_batch(FLU_SETTINGS, [new_case], control_number=500_001, now=now_datetime(), roster=new_roster)
        self.assertIn(b"NM1*IL*1*UPDATED*", fresh)
        self.assertIn(b"NM1*77*2*EXAMPLE ASSISTED LIVING~N3*200 REVISED DEMO WAY*BUILDING B~", fresh)
        download_837p(batch.name)
        self.assertEqual(frappe.local.response["filecontent"], payload)

        original_exporter = x837p.build_segments

        def upgraded_exporter(*args, **kwargs):
            return [segment.replace("BILLING TEST", "UPGRADED BILLER") for segment in original_exporter(*args, **kwargs)]

        with patch.object(x837p, "build_segments", side_effect=upgraded_exporter) as newer:
            fresh = build_test_batch(FLU_SETTINGS, [new_case], control_number=500_001, now=now_datetime(), roster=new_roster)
            self.assertIn(b"UPGRADED BILLER", fresh)
            newer.reset_mock()
            roster_path.unlink()
            with patch.dict(frappe.flags, in_test=False):
                download_837p(batch.name)
            newer.assert_not_called()
        reconstructed = frappe.local.response["filecontent"]
        self.assertEqual(reconstructed, payload)
        self.assertEqual(sha256(reconstructed).hexdigest(), digest)
        self.assertEqual(batch.reload().phi_snapshot_ref, snapshot.name)

        frappe.set_user(reviewer.name)
        batch.check_permission("read")
        # Exercise the request-time role check that Frappe skips in bench tests.
        with patch.dict(frappe.flags, in_test=False), self.assertRaises(frappe.PermissionError):
            download_837p(batch.name)
        events = frappe.get_all(
            "OpenPractice Reconstruction Event",
            filters={"submission": batch.name},
            fields=["performed_by", "file_sha256"],
        )
        self.assertEqual(len(events), 3)
        self.assertTrue(all(event.performed_by == biller.name and event.file_sha256 == digest for event in events))
