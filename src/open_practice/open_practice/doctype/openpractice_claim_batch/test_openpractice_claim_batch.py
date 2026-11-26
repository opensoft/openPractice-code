from hashlib import sha256
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime

from claimlab import x837p
from open_practice.billing import build_test_batch
from open_practice.open_practice.doctype.openpractice_claim_batch.openpractice_claim_batch import (
    download_837p,
)
from open_practice.test_subscriber_roster import get_test_subscriber
from open_practice.tests.test_billing import SETTINGS, sample_case


class TestOpenPracticeClaimBatch(FrappeTestCase):
    def test_releases_frozen_submission_and_reconstructs_after_live_data_and_exporter_change(self):
        frappe.set_user("Administrator")
        self.addCleanup(frappe.set_user, "Administrator")
        settings = frappe.get_single("OpenPractice EDI Settings")
        settings.update({**SETTINGS, "first_interchange_control": 500_001})
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

        values = sample_case()
        for field in ("name", "subscriber_ref", "chart_revision"):
            values.pop(field)
        case = frappe.get_doc(doctype="OpenPractice Billing Case", **values).insert()
        self.assertEqual(case.diagnosis_count, 2)
        for field in ("diagnoses", "member_id", "date_of_birth", "patient_first_name"):
            self.assertIsNone(case.meta.get_field(field))
        self.assertFalse(frappe.db.exists("DocType", "OpenPractice Visit"))
        case.submit()
        rows = [{"billing_case": case.name}]
        batch = frappe.get_doc(doctype="OpenPractice Claim Batch", billing_cases=rows).insert()
        with self.assertRaises(frappe.ValidationError):
            download_837p(batch.name)
        roster = get_test_subscriber(case.subscriber_ref)
        roster_patch = patch("open_practice.billing.get_test_subscriber", return_value=roster)
        roster_patch.start()
        self.addCleanup(roster_patch.stop)
        batch.submit()
        self.assertEqual(case.reload().claim_batch, batch.name)
        self.assertEqual(batch.claim_count, 1)
        self.assertEqual(batch.billing_cases, [])
        self.assertIsNone(batch.meta.get_field("edi_payload"))
        for protected in (case.name, case.subscriber_ref, "PATIENT1", "SYNTH000001", "E119", "HC:99213"):
            self.assertNotIn(protected, batch.frozen_billing)
        self.assertRegex(batch.phi_snapshot_ref, r"^[0-9a-f]{32}$")
        snapshot = frappe.get_doc("OpenPractice Claim PHI Snapshot", batch.phi_snapshot_ref)
        self.assertEqual(snapshot.submission, batch.name)
        with self.assertRaises(frappe.PermissionError):
            snapshot.check_permission("read")

        download_837p(batch.name)
        payload = frappe.local.response["filecontent"]
        digest = sha256(payload).hexdigest()
        self.assertEqual(digest, batch.file_sha256)
        self.assertIn(f"CLM*{case.name}*150*".encode("ascii"), payload)
        self.assertIn(b"HI*ABK:E119*ABF:I10~", payload)
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

        roster["patient_last_name"] = "UPDATED"
        fresh = build_test_batch(SETTINGS, [case.as_dict()], control_number=500_001, now=now_datetime())
        self.assertIn(b"NM1*IL*1*UPDATED*", fresh)
        self.assertIn(b"NM1*IL*1*PATIENT1*", payload)
        download_837p(batch.name)
        self.assertEqual(frappe.local.response["filecontent"], payload)

        original_exporter = x837p.build_segments

        def upgraded_exporter(*args, **kwargs):
            return [segment.replace("BILLING TEST", "UPGRADED BILLER") for segment in original_exporter(*args, **kwargs)]

        with patch.object(x837p, "build_segments", side_effect=upgraded_exporter) as newer:
            fresh = build_test_batch(SETTINGS, [case.as_dict()], control_number=500_001, now=now_datetime())
            self.assertIn(b"UPGRADED BILLER", fresh)
            newer.reset_mock()
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
