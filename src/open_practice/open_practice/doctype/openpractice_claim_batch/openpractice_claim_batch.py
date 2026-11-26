import json
import re
from hashlib import sha256

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

from open_practice.billing import prepare_test_batch, validate_test_batch
from open_practice.frozen_837p import freeze_837p, render_frozen_837p
from open_practice.site import require_test_site


class OpenPracticeClaimBatch(Document):
    def validate(self):
        require_test_site()
        if self.docstatus == 0:
            self._clear_release()

    def _clear_release(self):
        self.frozen_billing = None
        self.phi_snapshot_ref = None
        self.file_sha256 = None
        self.interchange_control = None
        self.claim_count = 0
        self.portal_reference = None
        self.upload_file_sha256 = None
        self.file_ack = "Pending"
        self.claim_ack = "Pending"
        self.portal_result = "Awaiting upload"

    def before_submit(self):
        self._clear_release()
        case_names = [row.billing_case for row in self.billing_cases]
        if not case_names or len(case_names) != len(set(case_names)):
            frappe.throw("Select distinct approved billing cases for this batch.")
        cases = []
        for name in sorted(case_names):
            case = frappe.get_doc("OpenPractice Billing Case", name, for_update=True)
            case.check_permission("read")
            if case.docstatus != 1 or case.claim_batch:
                frappe.throw("A billing case is still a draft or already belongs to a released claim batch.")
            cases.append(case.as_dict())

        settings = frappe.get_single("OpenPractice EDI Settings")
        try:
            first_control = int(settings.first_interchange_control or 0)
            if not 1 <= first_control <= 999_999_999:
                raise ValueError("Configure the first unused interchange control number in EDI Settings.")
            control_number = first_control + int(self.name.removeprefix("OPB-")) - 1
            now = now_datetime()
            data = prepare_test_batch(settings.as_dict(), cases, control_number=control_number, now=now)
            billing, phi = freeze_837p(data, control_number=control_number, now=now)
        except ValueError as error:
            frappe.throw(str(error))

        self.frozen_billing = json.dumps(billing, separators=(",", ":"))
        snapshot = frappe.get_doc(
            doctype="OpenPractice Claim PHI Snapshot",
            submission=self.name,
            frozen_phi=json.dumps(phi, separators=(",", ":")),
        ).insert(ignore_permissions=True)
        self.phi_snapshot_ref = snapshot.name
        payload = _read_frozen_pair(self)
        try:
            validate_test_batch(payload)
        except ValueError as error:
            frappe.throw(str(error))
        self.file_sha256 = sha256(payload).hexdigest()
        self.interchange_control = f"{control_number:09d}"
        self.claim_count = len(cases)
        self.flags.released_cases = case_names
        self.set("billing_cases", [])

    def on_submit(self):
        for name in self.flags.released_cases:
            frappe.db.set_value("OpenPractice Billing Case", name, "claim_batch", self.name)

    def before_cancel(self):
        frappe.throw("A released submission is retained for reconstruction.")

    def before_update_after_submit(self):
        require_test_site()
        previous = self.get_doc_before_save()
        if previous.portal_reference and self.portal_reference != previous.portal_reference:
            frappe.throw("The recorded portal transmission reference is fixed for this submission.")
        if not self.portal_reference:
            if (self.file_ack, self.claim_ack) != ("Pending", "Pending"):
                frappe.throw("Enter the portal transmission reference before recording acknowledgments.")
            self.portal_result = "Awaiting upload"
            self.upload_file_sha256 = None
            return
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}", self.portal_reference):
            frappe.throw("Enter the portal transmission reference only.")
        if not self.phi_snapshot_ref or not self.file_sha256:
            frappe.throw("Upload evidence requires a frozen submission.")
        self.upload_file_sha256 = self.file_sha256
        if "Rejected" in (self.file_ack, self.claim_ack):
            self.portal_result = "Rejected"
        elif (self.file_ack, self.claim_ack) == ("Accepted", "Accepted"):
            self.portal_result = "Accepted"
        else:
            self.portal_result = "Awaiting acknowledgments"


def _read_frozen_pair(batch):
    snapshot = frappe.get_doc("OpenPractice Claim PHI Snapshot", batch.phi_snapshot_ref)
    if snapshot.submission != batch.name:
        frappe.throw("The restricted snapshot belongs to a different submission.")
    try:
        return render_frozen_837p(json.loads(batch.frozen_billing), json.loads(snapshot.frozen_phi))
    except (ValueError, KeyError, TypeError):
        frappe.throw("The stored submission could not be reconstructed.")


@frappe.whitelist(methods=["POST"])
def download_837p(name: str):
    require_test_site()
    batch = frappe.get_doc("OpenPractice Claim Batch", name)
    batch.check_permission("read")
    frappe.only_for("OpenPractice Billing Evidence")
    if batch.docstatus != 1 or not batch.phi_snapshot_ref or not batch.frozen_billing:
        frappe.throw("Release the claim batch before downloading its 837P.")
    payload = _read_frozen_pair(batch)
    if sha256(payload).hexdigest() != batch.file_sha256:
        frappe.throw("The stored claim export does not match its release digest.")
    frappe.get_doc(
        doctype="OpenPractice Reconstruction Event",
        submission=batch.name,
        performed_by=frappe.session.user,
        file_sha256=batch.file_sha256,
    ).insert(ignore_permissions=True)
    frappe.local.response.update(
        filename=f"{batch.name}-TEST.837",
        filecontent=payload,
        type="download",
    )
