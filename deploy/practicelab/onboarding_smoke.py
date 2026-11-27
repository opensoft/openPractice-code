"""Browser and native openHR readback for three synthetic practice setups."""

import json
import os
import re
from urllib.parse import quote

from playwright.sync_api import expect, sync_playwright


PRACTICE = os.environ["PRACTICELAB_URL"].rstrip("/")
OPENHR = "http://127.0.0.1:8010"
API = "/api/method/open_practice.frappe_practice_onboarding."
COMPANY = "/api/resource/Company"
REQUEST_FIELD = "custom_openpractice_request_id"


def company_record(api, identifier):
    response = api.get(OPENHR + COMPANY + "/" + quote(identifier, safe=""))
    assert response.ok, f"Company readback HTTP {response.status}"
    return response.json()["data"]


def assert_one_request(api, company):
    assert company[REQUEST_FIELD]
    response = api.get(OPENHR + COMPANY, params={
        "filters": json.dumps([[REQUEST_FIELD, "=", company[REQUEST_FIELD]]]),
        "fields": json.dumps(["name"]),
    })
    assert response.ok, f"Request-key readback HTTP {response.status}"
    assert response.json()["data"] == [{"name": company["name"]}]


def sign_in(browser, alias):
    page = browser.new_page()
    page.goto(PRACTICE + "/new-practice")
    expect(page).to_have_url(re.compile("/login"))
    page.locator("#login_email").fill(f"onboarding-{alias}@example.invalid")
    page.locator("#login_password").fill(os.environ["PRACTICELAB_SMOKE_PASSWORD"])
    page.locator(".btn-login").click()
    expect(page).to_have_url(re.compile("/new-practice$"), timeout=15_000)
    return page


def save_practice(page, office, company, practice_type):
    page.get_by_label("Office name").fill(office)
    page.get_by_label("Office address").fill("1 Synthetic Avenue, Denver, CO")
    page.get_by_label("Legal entity name").fill(company)
    page.get_by_label("Practice type").select_option(practice_type)
    page.get_by_role("button", name="Save practice").click()
    expect(page.get_by_role("status")).to_have_text("Practice saved")
    page.reload()
    expect(page.get_by_label("Practice type")).to_have_value(practice_type)
    expect(page.get_by_label("Office name")).to_have_value(office)


def expect_connected(page, company):
    expect(page.get_by_role("status")).to_have_text("Company connected", timeout=20_000)
    expect(page.locator("#company")).to_contain_text(company)
    page.reload()
    expect(page.get_by_role("status")).to_have_text("Company connected")
    expect(page.locator("#company")).to_contain_text(company)


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        token = os.environ["PRACTICELAB_COMPANY_KEY"] + ":" + os.environ["PRACTICELAB_COMPANY_SECRET"]
        native = playwright.request.new_context(extra_http_headers={"Authorization": "token " + token})
        anonymous = playwright.request.new_context()
        denied = anonymous.get(PRACTICE + API + "get_setup")
        assert denied.status in (401, 403), denied.status

        solo = sign_in(browser, "solo")
        save_practice(solo, "Synthetic Solo Office", "Synthetic Solo LLC", "Single practitioner")
        solo.get_by_role("button", name="Connect Company").click()
        expect_connected(solo, "Synthetic Solo LLC")
        assert_one_request(native, company_record(native, "Synthetic Solo LLC"))
        solo.close()

        group = sign_in(browser, "group")
        save_practice(group, "Synthetic Group Office", "Synthetic Group LLC", "Group")
        existing = company_record(native, "Synthetic Group LLC")
        group.get_by_label("Company action").select_option("Link existing Company")
        group.get_by_label("Existing Company ID").fill(existing["name"])
        group.get_by_label(re.compile("I confirm")).check()
        group.get_by_role("button", name="Connect Company").click()
        expect_connected(group, existing["name"])
        assert company_record(native, existing["name"]) == existing
        group.close()

        lost = sign_in(browser, "lost")
        save_practice(lost, "Synthetic Retry Office", "Synthetic Lost Response LLC", "Single practitioner")
        lost.get_by_role("button", name="Connect Company").click()
        expect(lost.get_by_role("status")).to_have_text("Connection unresolved", timeout=20_000)
        original = company_record(native, "Synthetic Lost Response LLC")
        assert_one_request(native, original)
        lost.reload()
        lost.get_by_role("button", name="Retry connection").click()
        expect_connected(lost, original["name"])
        assert company_record(native, original["name"]) == original
        assert_one_request(native, original)
        browser.close()
        print("Passed solo Company create, group Company link without modification, and retry after a dropped create response.")


if __name__ == "__main__":
    main()
