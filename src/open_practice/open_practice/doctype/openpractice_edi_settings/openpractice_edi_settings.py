from frappe.model.document import Document

from open_practice.site import require_test_site


class OpenPracticeEDISettings(Document):
    def validate(self):
        require_test_site()
