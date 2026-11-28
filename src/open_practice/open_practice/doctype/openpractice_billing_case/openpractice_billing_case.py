import frappe
from frappe.model.document import Document
from frappe.utils import getdate

from open_practice.billing import case_to_claim
from open_practice.desk_billing import validate_mock_source
from open_practice.mock_openchart import read_mock_billing_facts
from open_practice.site import load_site_roster, require_test_site


class OpenPracticeBillingCase(Document):
    def validate(self):
        require_test_site()
        previous = self.get_doc_before_save()
        try:
            if previous and previous.source_appointment != self.source_appointment:
                raise ValueError("The source appointment is fixed for this billing case.")
            validate_mock_source(self.source_appointment, self.chart_encounter_ref)
        except ValueError as error:
            frappe.throw(str(error))
        if self.docstatus == 0:
            try:
                facts = read_mock_billing_facts(self.chart_encounter_ref)
            except ValueError as error:
                frappe.throw(str(error))
            if previous and previous.chart_revision != facts.revision:
                self.coverage_verified = 0
            self.subscriber_ref = facts.subscriber_ref
            self.service_date = facts.service_date
            self.diagnosis_count = len(facts.diagnoses)
            self.chart_revision = facts.revision
            self.claim_batch = None

    def before_submit(self):
        try:
            roster = load_site_roster()
            case_to_claim(self.as_dict(), today=getdate(), roster=roster)
        except ValueError as error:
            frappe.throw(str(error))
        self.synthetic_roster_sha256 = roster.sha256 if roster else None
