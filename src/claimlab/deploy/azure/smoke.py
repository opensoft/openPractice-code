import argparse
import json
from http.client import HTTPSConnection
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


parser = argparse.ArgumentParser(description="Smoke-test a deployed ClaimLab sandbox.")
parser.add_argument("origin", help="HTTPS origin of the sandbox.")
parser.add_argument("--browser-channel", help="Installed Chromium channel, such as msedge.")
args = parser.parse_args()
origin = args.origin.rstrip("/")
public_origin = "https://claimlab.opensoft.dev"
target = urlsplit(origin)
assert target.scheme == "https" and not target.path

def fetch(method, path, body=None, content_type=None):
    headers = {"Content-Type": content_type} if content_type else {}
    connection = HTTPSConnection(target.netloc, timeout=25)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, dict((key.lower(), value) for key, value in response.getheaders()), response.read()
    finally:
        connection.close()


status, headers, page = fetch("GET", "/")
assert status == 200 and b"<title>ClaimLab Sandbox</title>" in page
assert "content-security-policy" in headers
assert b'href="/agents"' in page and b'href="/openapi.json"' in page
assert (public_origin + "/api/v1/validate").encode() in page

status, headers, guide = fetch("GET", "/agents")
assert status == 200 and headers["content-type"].startswith("text/html")
assert b"ClaimLab agent guide" in guide and b"<script" not in guide
assert (public_origin + "/api/v1/validate").encode() in guide

status, headers, index = fetch("GET", "/llms.txt")
assert status == 200 and headers["content-type"].startswith("text/plain")
assert (public_origin + "/openapi.json").encode() in index

status, headers, body = fetch("GET", "/openapi.json")
assert status == 200 and headers["content-type"].startswith("application/json")
spec = json.loads(body)
assert spec["servers"][0]["url"] == public_origin
operations = spec["paths"]
assert "post" in operations["/api/v1/validate"]
assert "get" in operations["/api/v1/examples/{name}"]
accepted_file = None
for name, statuses in {
    "accepted.837": ("not_generated", "accepted", "accepted", 1),
    "accepted-25.837": ("not_generated", "accepted", "accepted", 25),
    "claim-rejected.837": ("not_generated", "accepted", "rejected", 1),
    "file-rejected.837": ("rejected", "not_generated", "not_generated", 0),
    "missing-clm02.837": ("not_generated", "rejected", "not_generated", 0),
    "invalid-total-charge.837": ("not_generated", "accepted", "rejected", 1),
}.items():
    status, headers, edi = fetch("GET", "/api/v1/examples/" + name)
    assert status == 200 and headers["content-disposition"] == f'attachment; filename="{name}"', (name, status)
    assert "no-store" in headers["cache-control"]
    if name == "accepted-25.837":
        accepted_file = edi
    status, headers, body = fetch("POST", "/api/v1/validate", edi, "application/edi-x12")
    assert status == 200 and "no-store" in headers["cache-control"], (name, status)
    result = json.loads(body)
    assert result["simulated"] is True and b"TAYLOR" not in body
    assert tuple(result[key]["status"] for key in ("interchange_ack", "file_ack", "claim_ack")) + (
        len(result["claim_ack"]["claims"]),) == statuses, (name, result)
    print(name, statuses, flush=True)
for body, media_type, expected in (
    (b"", "application/edi-x12", 400),
    (b"test", "text/plain", 415),
    (b"A" * (1024 * 1024 + 1), "application/edi-x12", 413),
):
    status, _, _ = fetch("POST", "/api/v1/validate", body, media_type)
    assert status == expected, (status, expected)
for path in ("/manifest.json", "/api/v1/examples/bulk-0001.837", "/carrier-profile.json"):
    status, _, _ = fetch("GET", path)
    assert status == 404, (path, status)

with sync_playwright() as browser_tools:
    browser = browser_tools.chromium.launch(channel=args.browser_channel)
    page = browser.new_page()
    script_errors = []
    page.on("pageerror", lambda error: script_errors.append(str(error)))
    page.goto(origin + "/", wait_until="networkidle")
    page.get_by_label("Choose an 837P file").set_input_files({
        "name": "accepted-25.837", "mimeType": "application/edi-x12", "buffer": accepted_file
    })
    page.get_by_role("button", name="Run check").click()
    expect(page.get_by_role("status")).to_have_text("25 claims passed the simulation.", timeout=25000)
    page.get_by_role("button", name="Missing CLM02").click()
    expect(page.locator("#file-status")).to_have_text("Rejected", timeout=25000)
    expect(page.locator("#claim-status")).to_have_text("Not generated")
    expect(page.locator("#api-example")).to_contain_text(origin + "/api/v1/validate")
    assert not script_errors, script_errors
    browser.close()

print("HTTPS agent guide, OpenAPI specification, browser upload, and API responses verified", flush=True)
