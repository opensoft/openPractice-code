from pathlib import Path

import frappe

from open_practice.test_subscriber_roster import load_approved_synthetic_roster


def require_test_site():
    if frappe.conf.get("open_practice_novitas_test") != 1:
        frappe.throw("OpenPractice claims are enabled only on a configured Novitas test site.")


def load_site_roster():
    require_test_site()
    mode = frappe.conf.get("open_practice_novitas_roster_mode", "rehearsal")
    if mode == "rehearsal":
        return None
    if mode != "approved-synthetic":
        raise ValueError("The Novitas test roster mode must be rehearsal or approved-synthetic.")
    path = Path(frappe.get_site_path("private", "openpractice-novitas-synthetic-roster.csv"))
    return load_approved_synthetic_roster(path, frappe.conf.get("open_practice_novitas_roster_approval"))
