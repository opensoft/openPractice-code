from hashlib import sha256

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

from open_practice.billing import build_test_batch
from open_practice.site import require_test_site


class OpenPracticeClaimBatch(Document):
    def validate(self):
        require_test_site()
        if self.docstatus == 0:
            self._clear_release()

    def _clear_release(self):
        self.edi_payload = None
        self.file_sha256 = None
        self.interchange_control = None
        self.claim_count = 0
        self.portal_reference = None
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
            payload = build_test_batch(settings.as_dict(), cases, control_number=control_number, now=now_datetime())
        except ValueError as error:
            frappe.throw(str(error))

        self.edi_payload = payload.decode("ascii")
        self.file_sha256 = sha256(payload).hexdigest()
        self.interchange_control = f"{control_number:09d}"
        self.claim_count = len(cases)

    def on_submit(self):
        for row in self.billing_cases:
            frappe.db.set_value("OpenPractice Billing Case", row.billing_case, "claim_batch", self.name)

    def before_update_after_submit(self):
        require_test_site()
        if not self.portal_reference:
            if (self.file_ack, self.claim_ack) != ("Pending", "Pending"):
                frappe.throw("Enter the portal transmission reference before recording acknowledgments.")
            self.portal_result = "Awaiting upload"
        elif "Rejected" in (self.file_ack, self.claim_ack):
            self.portal_result = "Rejected"
        elif (self.file_ack, self.claim_ack) == ("Accepted", "Accepted"):
            self.portal_result = "Accepted"
        else:
            self.portal_result = "Awaiting acknowledgments"


@frappe.whitelist(methods=["GET"])
def download_837p(name: str):
    require_test_site()
    batch = frappe.get_doc("OpenPractice Claim Batch", name)
    batch.check_permission("read")
    if batch.docstatus != 1 or not batch.edi_payload:
        frappe.throw("Release the claim batch before downloading its 837P.")
    payload = batch.edi_payload.encode("ascii")
    if sha256(payload).hexdigest() != batch.file_sha256:
        frappe.throw("The stored claim export does not match its release digest.")
    frappe.local.response.update(
        filename=f"{batch.name}-TEST.837",
        filecontent=payload,
        type="download",
    )
