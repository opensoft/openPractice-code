"""Browser acceptance for a synthetic scheduler account on a Frappe site."""

import os
import re

from playwright.sync_api import expect, sync_playwright


BASE = os.environ["PRACTICELAB_URL"].rstrip("/")
EMAIL = os.environ["PRACTICELAB_SMOKE_USER"]
PASSWORD = os.environ["PRACTICELAB_SMOKE_PASSWORD"]
API = "/api/method/open_practice.frappe_session_desk.request"


def sign_in(page, user):
    page.locator("#login_email").fill(user)
    page.locator("#login_password").fill(PASSWORD)
    page.locator(".btn-login").click()


def check_desk_workspace(browser, appointment_reference):
    page = browser.new_page()
    try:
        page.goto(BASE + "/app")
        expect(page).to_have_url(re.compile("/login"))
        sign_in(page, "Administrator")
        expect(page).to_have_url(re.compile("/app(?:[/?#]|$)"), timeout=15_000)
        page.get_by_role("link", name="openPractice", exact=True).click(timeout=15_000)
        expect(page).to_have_url(BASE + "/app/openpractice")
        page.get_by_role("link", name="Session Desk", exact=True).click()
        expect(page).to_have_url(BASE + "/practice")
        expect(page.get_by_role("button", name="New appointment")).to_be_enabled()
        page.locator(f'.appointment[data-appointment="{appointment_reference}"]').click()
        with page.expect_response(lambda response: response.request.method == "POST" and API in response.url) as result:
            page.get_by_role("button", name="Open test billing case", exact=True).click()
        assert result.value.ok
        case_name = result.value.json()["message"]["billing_case"]
        expect(page).to_have_url(BASE + "/app/openpractice-billing-case/" + case_name)
        response = page.request.get(BASE + "/api/resource/OpenPractice%20Billing%20Case/" + case_name)
        assert response.ok
        case = response.json()["data"]
        assert case["source_appointment"] == appointment_reference
        assert case["chart_encounter_ref"].startswith("OC-MOCK-DESK-000001-")
        assert case["docstatus"] == 0 and not case["coverage_verified"]
        print("Passed the openPractice Workspace shortcut and linked draft billing case.")
    finally:
        page.close()


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.goto(BASE + "/practice")
        expect(page).to_have_url(re.compile("/login"))
        denied = page.request.get(BASE + API, params={"route": "/api/patients"})
        if denied.status not in (401, 403):
            raise AssertionError(f"Anonymous API access should be denied, got HTTP {denied.status}")
        sign_in(page, EMAIL)
        expect(page).to_have_url(re.compile("/practice$"), timeout=15_000)

        page.get_by_role("button", name="New appointment").click()
        page.locator('#booking-form [name="patient"]').select_option("demo-01")
        page.locator('#booking-form [name="location"]').select_option("clinic")
        page.get_by_role("button", name="Book appointment").click()
        expect(page.locator("#notice")).to_have_text("Appointment booked.")
        appointment = page.locator(".appointment[aria-pressed=true]")
        reference = appointment.get_attribute("data-appointment")

        for action in ("Register arrival", "Mark patient ready", "Start session", "End session"):
            page.get_by_role("button", name=action, exact=True).click()
        wrap = page.get_by_role("button", name="Wrap up session", exact=True)
        expect(wrap).to_be_disabled()
        page.locator("#chart-complete").check()
        wrap.click()
        expect(page.get_by_role("heading", name="Ready for billing", exact=True)).to_be_visible()

        page.reload()
        expect(page.get_by_role("heading", name="Ready for billing", exact=True)).to_be_visible()
        result = page.request.get(BASE + API, params={"route": "/api/appointments"})
        if not result.ok:
            raise AssertionError(f"Appointment readback failed, got HTTP {result.status}")
        stored = next(app for app in result.json()["message"] if app["id"] == reference)
        assert stored["ready_for_billing"]
        assert [event["stage"] for event in stored["events"]] == [
            "scheduled", "available", "ready", "started", "ended", "wrapped",
        ]
        assert all(event["recorded_by"] == EMAIL for event in stored["events"])
        print("Passed Frappe sign-in, appointment booking, all six stages, and database readback.")
        if os.environ.get("PRACTICELAB_SMOKE_CHECK_DESK") == "1":
            expect(page.get_by_role("button", name="Open test billing case")).to_have_count(0)
            check_desk_workspace(browser, reference)
        browser.close()


if __name__ == "__main__":
    main()
