import frappe


def require_test_site():
    if frappe.conf.get("open_practice_novitas_test") != 1:
        frappe.throw("OpenPractice claims are enabled only on a configured Novitas test site.")
