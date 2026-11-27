from html import escape

import frappe
from frappe.sessions import get_csrf_token

from open_practice.frappe_session_desk import enabled, require_scheduler
from open_practice.session_desk import DESK_HTML

no_cache = 1


def get_context(context):
    if not enabled():
        raise frappe.DoesNotExistError
    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/login?redirect-to=%2Fpractice"
        raise frappe.Redirect
    require_scheduler()
    context.no_cache = 1
    token = escape(get_csrf_token(), quote=True)
    context.desk_html = (
        DESK_HTML.read_text(encoding="utf-8")
        .replace("<meta charset=", f'<meta name="practicelab-csrf" content="{token}"><meta charset=', 1)
        .replace('href="/desk.css"', 'href="/assets/open_practice/session_desk/desk.css"', 1)
        .replace('src="/desk.js"', 'src="/assets/open_practice/session_desk/desk.js"', 1)
    )
