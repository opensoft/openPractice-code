import frappe
from frappe.model.document import Document


class OpenPracticeReconstructionEvent(Document):
    def validate(self):
        if not self.is_new():
            frappe.throw("A reconstruction event is immutable.")

    def on_trash(self):
        frappe.throw("A reconstruction event is retained with the submission.")
