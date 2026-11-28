"""Exercise signup in a disposable Frappe site using a local SMTP capture service."""

from hashlib import sha256
from html import unescape
import json
import os
import re
import subprocess

from playwright.sync_api import expect, sync_playwright


SITE = "practicelab.localhost"
SIGNUP = "/api/method/open_practice.frappe_account_signup."
FRESH = "signup-admin@example.invalid"
EXISTING = "scheduler@example.invalid"
DOCTYPE = "OpenPractice Signup Request"
VERIFY_LINK = re.compile(r'https?://[^"\s]+/start#verify=[a-f0-9]{64}\.[a-f0-9]{64}')
INVITE_LINK = re.compile(r'https?://[^"\s]+/update-password\?key=[a-zA-Z0-9]+')


def bench(method, *args, **kwargs):
    subprocess.run([
        "docker", "exec", "practice", "bench", "--site", SITE, "execute", method,
        "--args", json.dumps(args), "--kwargs", json.dumps(kwargs),
    ], check=True)


def configure_mail(origin):
    bench("frappe.db.set_single_value", "Website Settings", "disable_signup", 1)
    bench("frappe.client.insert", {
        "doctype": "Email Account", "email_account_name": "Local signup capture",
        "email_id": "qa@example.invalid", "enable_outgoing": 1, "default_outgoing": 1,
        "smtp_server": "mailpit", "smtp_port": 1025, "no_smtp_authentication": 1,
        "always_use_account_email_id_as_sender": 1,
    })
    subprocess.run([
        "docker", "exec", "practice", "bench", "--site", SITE,
        "set-config", "host_name", origin,
    ], check=True)
    toggle_signup("1")


def toggle_signup(value):
    subprocess.run([
        "docker", "exec", "practice", "bench", "--site", SITE,
        "set-config", "-p", "open_practice_account_signup", value,
    ], check=True)
    subprocess.run([
        "docker", "exec", "practice", "bench", "--site", SITE, "clear-cache",
    ], check=True)


def flush():
    bench("frappe.email.queue.flush")


def messages(mailbox):
    return mailbox.get("/api/v1/messages").json()["messages"] or []


def mail_link(mailbox, message, pattern):
    parsed = mailbox.get("/api/v1/message/" + message["ID"]).json()
    match = pattern.search(unescape(parsed["HTML"] or parsed["Text"]))
    assert match is not None, "Expected link missing from synthetic message"
    return match[0]


def post(page, origin, method, data, *, csrf=None):
    if csrf is None:
        csrf = page.locator('meta[name="practicelab-csrf"]').get_attribute("content")
    return page.request.post(
        origin + SIGNUP + method, data=data, headers={"X-Frappe-CSRF-Token": csrf},
    )


def test_signup(playwright, origin):
    configure_mail(origin)
    browser = playwright.chromium.launch()
    mailbox = playwright.request.new_context(base_url="http://127.0.0.1:8025")
    guest = browser.new_page()
    guest.goto(origin + "/")
    expect(guest.get_by_role("heading", name="Sign in or sign up")).to_be_visible()
    expect(guest.get_by_role("link", name="Sign in with your password")).to_be_hidden()
    expect(guest.get_by_role("button", name="Next")).to_be_visible()
    guest.get_by_label("Work email").fill(FRESH.upper())
    guest.get_by_role("button", name="Next").click()
    expect(guest.get_by_text("check your inbox", exact=False)).to_be_visible()

    assert post(guest, origin, "request_signup", {"email": FRESH}).json()["message"] == {"accepted": True}
    assert post(guest, origin, "request_signup", {"email": EXISTING}).json()["message"] == {"accepted": True}

    existing = browser.new_page()
    existing.goto(origin + "/start")
    existing.get_by_label("Work email").fill(EXISTING)
    existing.get_by_role("button", name="Next").click()
    expect(existing.get_by_role("heading", name="Continue with openPractice")).to_be_visible()
    assert existing.locator("#access-options").inner_text() == guest.locator("#access-options").inner_text()
    existing.get_by_role("button", name="Change email or try again").click()
    expect(existing.get_by_label("Work email")).to_be_focused()
    expect(existing.get_by_label("Work email")).to_have_value(EXISTING)
    existing.close()

    flush()
    [original] = messages(mailbox)
    assert original["To"][0]["Address"] == FRESH
    expired = mail_link(mailbox, original, VERIFY_LINK)

    request_id = sha256(FRESH.encode()).hexdigest()
    bench("frappe.db.set_value", DOCTYPE, request_id, "sent_at", "2020-01-01 00:00:00")
    guest.goto(expired)
    guest.get_by_role("button", name="Verify work email").click()
    expect(guest.get_by_role("status")).to_contain_text("no longer valid")
    guest.get_by_label("Work email").fill(FRESH)
    guest.get_by_role("button", name="Next").click()
    expect(guest.get_by_text("check your inbox", exact=False)).to_be_visible()
    flush()
    reissued, _ = messages(mailbox)
    fresh_link = mail_link(mailbox, reissued, VERIFY_LINK)
    assert fresh_link != expired
    assert post(guest, origin, "verify_signup", {"request_id": request_id, "token": expired.rsplit(".", 1)[-1]}
                ).json()["message"] == {"verified": False}

    operator = browser.new_page()
    operator.goto(origin + "/signup-review")
    operator.locator("#login_email").fill("Administrator")
    operator.locator("#login_password").fill(os.environ["PRACTICELAB_SMOKE_PASSWORD"])
    operator.locator(".btn-login").click()
    expect(operator).to_have_url(re.compile("/signup-review$"), timeout=15_000)
    expect(operator.get_by_text("No verified signup requests are waiting.")).to_be_visible()
    assert not post(operator, origin, "issue_invite", {"request_id": request_id}).ok

    guest.goto(fresh_link)
    expect(guest.get_by_role("button", name="Verify work email")).to_be_visible()
    operator.reload()
    expect(operator.get_by_text("No verified signup requests are waiting.")).to_be_visible()
    guest.get_by_role("button", name="Verify work email").click()
    expect(guest.get_by_role("status")).to_contain_text("Work email verified")
    assert post(guest, origin, "verify_signup", {"request_id": request_id, "token": fresh_link.rsplit(".", 1)[-1]}
                ).json()["message"] == {"verified": False}
    assert post(guest, origin, "issue_invite", {"request_id": request_id}).status in (401, 403)

    operator.reload()
    expect(operator.get_by_text(FRESH, exact=True)).to_be_visible()
    operator.get_by_role("button", name="Send practice invite").click()
    expect(operator.get_by_role("status")).to_have_text("Practice invitation queued.")
    assert post(operator, origin, "issue_invite", {"request_id": request_id}).json()["message"] == {"issued": True}
    flush()
    invite, _, _ = messages(mailbox)
    assert invite["To"][0]["Address"] == FRESH

    invitee = browser.new_page()
    invitee.goto(mail_link(mailbox, invite, INVITE_LINK))
    invitee.locator("#new_password").press_sequentially(os.environ["PRACTICELAB_SMOKE_PASSWORD"])
    invitee.locator("#confirm_password").press_sequentially(os.environ["PRACTICELAB_SMOKE_PASSWORD"])
    invitee.locator("#update").click()
    expect(invitee).to_have_url(re.compile("/new-practice$"), timeout=15_000)
    expect(invitee.get_by_role("heading", name="New Practice")).to_be_visible()
    invitee.get_by_label("Office name").fill("Synthetic Signup Office")
    invitee.get_by_label("Office address").fill("1 Synthetic Avenue")
    invitee.get_by_label("Legal entity name").fill("Synthetic Signup LLC")
    invitee.get_by_label("Practice type").select_option("Single practitioner")
    invitee.get_by_role("button", name="Save practice").click()
    expect(invitee.get_by_role("status")).to_have_text("Practice saved")

    csrf = invitee.locator('meta[name="practicelab-csrf"]').get_attribute("content")
    assert post(invitee, origin, "issue_invite", {"request_id": request_id}, csrf=csrf).status == 403
    assert invitee.request.get(origin + "/signup-review").status == 403
    assert invitee.request.get(origin + "/api/resource/OpenPractice%20Signup%20Request").status == 403
    assert invitee.request.get(origin + "/api/resource/OpenPractice%20Claim%20Batch").status == 403
    assert invitee.request.get(origin + "/api/method/open_practice.frappe_session_desk.request",
                               params={"route": "/appointments"}).status == 403
    assert post(guest, origin, "request_signup", {"email": FRESH}).json()["message"] == {"accepted": True}

    returning = browser.new_page()
    returning.goto(origin + "/start")
    returning.get_by_label("Work email").fill(FRESH)
    returning.get_by_role("button", name="Next").click()
    returning.get_by_role("link", name="Sign in with your password").click()
    expect(returning.locator("#login_email")).to_have_value(FRESH)
    returning.locator("#login_password").fill(os.environ["PRACTICELAB_SMOKE_PASSWORD"])
    returning.locator(".btn-login").click()
    expect(returning).to_have_url(re.compile("/new-practice$"), timeout=15_000)
    expect(returning.get_by_label("Office name")).to_have_value("Synthetic Signup Office")

    toggle_signup("0")
    assert guest.request.get(origin + "/start").status == 404
    assert post(guest, origin, "request_signup", {"email": "disabled@example.invalid"}).status == 404
    flush()
    assert len(messages(mailbox)) == 3
    print("PASS: Shared email entry for signup and returning sign-in, and native invitation to New Practice.")


if __name__ == "__main__":
    with sync_playwright() as runtime:
        test_signup(runtime, os.environ["PRACTICELAB_URL"].rstrip("/"))
