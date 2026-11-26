from hashlib import sha256

import frappe
from frappe.tests.utils import FrappeTestCase

from open_practice.open_practice.doctype.openpractice_claim_batch.openpractice_claim_batch import (
    download_837p,
)
from open_practice.tests.test_billing import SETTINGS, sample_case


class TestOpenPracticeClaimBatch(FrappeTestCase):
    def test_chart_handoff_release_download_and_record_portal_result(self):
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
        batch.submit()
        self.assertEqual(case.reload().claim_batch, batch.name)
        self.assertEqual(batch.claim_count, 1)

        download_837p(batch.name)
        payload = frappe.local.response["filecontent"]
        self.assertEqual(sha256(payload).hexdigest(), batch.file_sha256)
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
        self.assertEqual(batch.portal_result, "Awaiting acknowledgments")
        batch.claim_ack = "Accepted"
        batch.save()
        self.assertEqual(batch.reload().portal_result, "Accepted")
        self.assertEqual(batch.edi_payload.encode("ascii"), payload)
