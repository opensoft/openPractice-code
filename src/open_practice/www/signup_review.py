from html import escape

import frappe
from frappe.sessions import get_csrf_token

from open_practice.frappe_account_signup import pending_invites, signup_origin


no_cache = 1


def get_context(context):
    if not signup_origin():
        raise frappe.DoesNotExistError
    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/login?redirect-to=%2Fsignup-review"
        raise frappe.Redirect
    context.requests = pending_invites()
    context.csrf_token = escape(get_csrf_token(), quote=True)
