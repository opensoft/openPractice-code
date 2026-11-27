"""Browser acceptance for a synthetic scheduler account on a Frappe site."""

import os
import re

from playwright.sync_api import expect, sync_playwright


BASE = os.environ["PRACTICELAB_URL"].rstrip("/")
EMAIL = os.environ["PRACTICELAB_SMOKE_USER"]
PASSWORD = os.environ["PRACTICELAB_SMOKE_PASSWORD"]
API = "/api/method/open_practice.frappe_session_desk.request"


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.goto(BASE + "/practice")
        expect(page).to_have_url(re.compile("/login"))
        denied = page.request.get(BASE + API, params={"route": "/api/patients"})
        if denied.status not in (401, 403):
            raise AssertionError(f"Anonymous API access should be denied, got HTTP {denied.status}")
        page.locator("#login_email").fill(EMAIL)
        page.locator("#login_password").fill(PASSWORD)
        page.locator(".btn-login").click()
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
        browser.close()


if __name__ == "__main__":
    main()
