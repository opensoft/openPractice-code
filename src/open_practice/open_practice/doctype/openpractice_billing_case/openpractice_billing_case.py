import frappe
from frappe.model.document import Document
from frappe.utils import getdate

from open_practice.billing import case_to_claim
from open_practice.mock_openchart import read_mock_billing_facts
from open_practice.site import require_test_site


class OpenPracticeBillingCase(Document):
    def validate(self):
        require_test_site()
        if self.docstatus == 0:
            try:
                facts = read_mock_billing_facts(self.chart_encounter_ref)
            except ValueError as error:
                frappe.throw(str(error))
            previous = self.get_doc_before_save()
            if previous and previous.chart_revision != facts.revision:
                self.coverage_verified = 0
            self.subscriber_ref = facts.subscriber_ref
            self.service_date = facts.service_date
            self.diagnosis_count = len(facts.diagnoses)
            self.chart_revision = facts.revision
            self.claim_batch = None

    def before_submit(self):
        try:
            case_to_claim(self.as_dict(), today=getdate())
        except ValueError as error:
            frappe.throw(str(error))
