from uuid import uuid4

import frappe
from frappe.model.document import Document


class OpenPracticeClaimPHISnapshot(Document):
    def autoname(self):
        self.name = uuid4().hex

    def validate(self):
        if not self.is_new():
            frappe.throw("A released claim PHI snapshot is immutable.")

    def on_trash(self):
        frappe.throw("A released claim PHI snapshot is retained for reconstruction.")
