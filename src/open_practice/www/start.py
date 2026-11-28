from html import escape

import frappe
from frappe.sessions import get_csrf_token

from open_practice.frappe_account_signup import signup_origin


no_cache = 1


def get_context(context):
    if not signup_origin():
        raise frappe.DoesNotExistError
    context.csrf_token = escape(get_csrf_token(), quote=True)
    context.signed_in = frappe.session.user != "Guest"
