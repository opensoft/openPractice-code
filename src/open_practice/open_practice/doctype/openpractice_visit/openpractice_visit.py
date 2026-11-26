import frappe
from frappe.model.document import Document
from frappe.utils import getdate

from open_practice.billing import visit_to_claim
from open_practice.site import require_test_site


class OpenPracticeVisit(Document):
    def validate(self):
        require_test_site()
        if self.docstatus == 0:
            self.claim_batch = None

    def before_submit(self):
        try:
            visit_to_claim(self.as_dict(), today=getdate())
        except ValueError as error:
            frappe.throw(str(error))
