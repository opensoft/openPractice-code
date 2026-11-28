"""Synthetic QA signup using Frappe's existing User invitation."""

from datetime import timedelta
from hashlib import sha256
from hmac import compare_digest
import re
from secrets import token_hex

import frappe
from frappe.rate_limiter import rate_limit
from frappe.utils import get_datetime, now_datetime, validate_email_address

from open_practice.frappe_practice_onboarding import ROLE
from open_practice.frappe_session_desk import enabled as practicelab_enabled


DOCTYPE = "OpenPractice Signup Request"
QA_ORIGIN = "https://openpractice.opensoft.dev"
OPERATOR_ROLE = "System Manager"
HEX_KEY = re.compile(r"[0-9a-f]{64}")
TOKEN_LIFETIME = timedelta(minutes=30)
RESEND_INTERVAL = timedelta(minutes=2)
SEND_WINDOW = timedelta(days=1)
MAX_EMAILS = 5


def signup_origin():
    if not practicelab_enabled() or frappe.conf.get("open_practice_account_signup") != 1:
        return None
    origin = str(frappe.conf.get("host_name") or "").rstrip("/")
    if origin == QA_ORIGIN or re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}", origin):
        if frappe.db.get_single_value("Website Settings", "disable_signup"):
            return origin
    return None


def signup_home_page(user):
    if user == "Guest" and signup_origin():
        return "start"
    return None


def require_operator():
    if not signup_origin() or frappe.session.user == "Guest" or OPERATOR_ROLE not in frappe.get_roles():
        raise frappe.PermissionError


def _require_guest():
    origin = signup_origin()
    if not origin:
        raise frappe.DoesNotExistError
    if frappe.session.user != "Guest":
        raise frappe.PermissionError
    return origin


def _digest(value):
    return sha256(value.encode("utf-8")).hexdigest()


def _valid_key(value):
    return isinstance(value, str) and HEX_KEY.fullmatch(value) is not None


def _locked_request(request_id):
    return frappe.get_doc(DOCTYPE, request_id, for_update=True)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=10, seconds=600)
def request_signup(email):
    origin = _require_guest()
    if not isinstance(email, str):
        frappe.throw("Enter a valid work email.")
    email = email.strip().lower()
    if not email or len(email) > 140 or validate_email_address(email) != email:
        frappe.throw("Enter a valid work email.")

    accepted = {"accepted": True}
    if frappe.db.exists("User", {"email": email}):
        return accepted

    request_id = _digest(email)
    if not frappe.db.exists(DOCTYPE, request_id):
        frappe.get_doc({"doctype": DOCTYPE, "email": email}).insert(
            ignore_permissions=True, ignore_if_duplicate=True, set_name=request_id,
        )

    request = _locked_request(request_id)
    now = now_datetime()
    if request.verified_at or (
        request.sent_at and now < get_datetime(request.sent_at) + RESEND_INTERVAL
    ):
        return accepted
    if not request.window_started_at or now >= get_datetime(request.window_started_at) + SEND_WINDOW:
        request.window_started_at = now
        request.sent_count = 0
    if request.sent_count >= MAX_EMAILS:
        return accepted

    token = token_hex(32)
    link = f"{origin}/start#verify={request_id}.{token}"
    frappe.sendmail(
        recipients=[email],
        subject="Verify your openPractice QA email",
        message=(
            '<p>Confirm your work email to request an openPractice QA invitation.</p>'
            f'<p><a href="{link}">Verify work email</a></p>'
            '<p>This link expires after 30 minutes. If you did not request access, you can ignore it.</p>'
        ),
        delayed=True,
        add_unsubscribe_link=0,
    )
    request.token_digest = _digest(token)
    request.sent_at = now
    request.sent_count += 1
    request.save(ignore_permissions=True)
    return accepted


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=20, seconds=600)
def verify_signup(request_id, token):
    _require_guest()
    if not _valid_key(request_id) or not _valid_key(token):
        return {"verified": False}
    try:
        request = _locked_request(request_id)
    except frappe.DoesNotExistError:
        return {"verified": False}

    now = now_datetime()
    if (
        request.verified_at
        or not request.token_digest
        or not request.sent_at
        or now >= get_datetime(request.sent_at) + TOKEN_LIFETIME
        or not compare_digest(request.token_digest, _digest(token))
    ):
        return {"verified": False}

    request.verified_at = now
    request.token_digest = None
    request.save(ignore_permissions=True)
    return {"verified": True}


@frappe.whitelist(methods=["POST"])
def issue_invite(request_id):
    require_operator()
    if not _valid_key(request_id):
        frappe.throw("Choose a verified signup request.")
    request = _locked_request(request_id)
    if not request.verified_at:
        frappe.throw("The work email must be verified before sending an invitation.")
    if request.invited_at:
        return {"issued": True}
    if frappe.db.exists("User", {"email": request.email}):
        return {"issued": False}

    user = frappe.get_doc({
        "doctype": "User",
        "email": request.email,
        "first_name": "Practice administrator",
        "enabled": 1,
        "user_type": "Website User",
        "redirect_url": "/new-practice",
        "send_welcome_email": 0,
        "roles": [{"role": ROLE}],
    }).insert(ignore_permissions=True)
    user.flags.delay_emails = True
    user.send_welcome_mail_to_user()

    request.invited_at = now_datetime()
    request.invited_by = frappe.session.user
    request.save(ignore_permissions=True)
    return {"issued": True}


def pending_invites():
    require_operator()
    return frappe.get_all(
        DOCTYPE,
        filters={"verified_at": ["is", "set"], "invited_at": ["is", "not set"]},
        fields=["name", "email", "verified_at"],
        order_by="verified_at asc",
        limit=50,
    )
