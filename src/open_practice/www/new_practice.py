from html import escape

import frappe
from frappe.sessions import get_csrf_token

from open_practice.frappe_practice_onboarding import require_admin
from open_practice.frappe_session_desk import enabled


no_cache = 1


def get_context(context):
    if not enabled():
        raise frappe.DoesNotExistError
    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/login?redirect-to=%2Fnew-practice"
        raise frappe.Redirect
    require_admin()
    context.no_cache = 1
    context.csrf_token = escape(get_csrf_token(), quote=True)
