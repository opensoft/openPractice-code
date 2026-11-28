"""Create and independently read a synthetic QuickBooks Online sandbox TimeActivity."""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sandbox_auth import SandboxOAuthError, SandboxVault, connection_lock, get_sandbox_access


ORIGIN = "https://sandbox-quickbooks.api.intuit.com"


class SandboxError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class SandboxClient:
    def __init__(self, realm, token):
        if not re.fullmatch(r"[0-9]{1,32}", realm):
            raise SandboxError("The sandbox Realm ID must be numeric.")
        if not re.fullmatch(r"[A-Za-z0-9._~+/=-]+", token):
            raise SandboxError("The sandbox access token is invalid.")
        self.realm = realm
        self._token = token
        self._opener = build_opener(_NoRedirect)

    def request(self, path, *, query=None, body=None):
        url = f"{ORIGIN}/v3/company/{self.realm}/{path}"
        if query:
            url += "?" + urlencode(query)
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self._token}"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        try:
            with self._opener.open(Request(url, data=data, headers=headers), timeout=15) as response:
                result = json.load(response)
        except HTTPError as error:
            with error:
                try:
                    fault = json.load(error)["Fault"]["Error"][0]["code"]
                    fault = f", code {fault}" if str(fault).isdigit() else ""
                except (KeyError, IndexError, TypeError, ValueError):
                    fault = ""
                raise SandboxError(f"QuickBooks Online Sandbox returned HTTP {error.code}{fault}.") from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise SandboxError("QuickBooks Online Sandbox did not return a readable response.") from None
        if not isinstance(result, dict):
            raise SandboxError("QuickBooks Online Sandbox returned an unexpected result.")
        return result


def run(client, receipt_path, day):
    company = client.request(f"companyinfo/{client.realm}").get("CompanyInfo")
    if not company or not company.get("CompanyName"):
        raise SandboxError("The sandbox company identity could not be verified.")

    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt.get("realm") != client.realm:
            raise SandboxError("The stored synthetic request belongs to a different sandbox.")
    else:
        employees = client.request(
            "query", query={"query": "SELECT * FROM Employee WHERE Active = true MAXRESULTS 1"},
        ).get("QueryResponse", {}).get("Employee", [])
        if len(employees) != 1 or not str(employees[0].get("Id", "")).isdigit():
            raise SandboxError("The sandbox did not return an active sample employee.")
        request_id = uuid4().hex
        payload = {
            "NameOf": "Employee",
            "EmployeeRef": {"value": str(employees[0]["Id"])},
            "TxnDate": day.isoformat(),
            "Hours": 0,
            "Minutes": 8,
            "BillableStatus": "NotBillable",
            "Description": f"OpenPractice synthetic sandbox smoke {request_id}",
        }
        receipt = {"realm": client.realm, "request_id": request_id, "payload": payload}
        with receipt_path.open("x", encoding="utf-8") as target:
            json.dump(receipt, target, indent=2)

    activity_id = receipt.get("activity_id")
    if activity_id is None:
        activity = client.request(
            "timeactivity", query={"requestid": receipt["request_id"]}, body=receipt["payload"],
        ).get("TimeActivity", {})
        activity_id = str(activity.get("Id", ""))
        if not activity_id.isdigit():
            raise SandboxError("The create response did not identify a TimeActivity.")
        receipt["activity_id"] = activity_id
        temp = receipt_path.with_suffix(".tmp")
        temp.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        temp.replace(receipt_path)

    activity_id = str(activity_id)
    if not activity_id.isdigit():
        raise SandboxError("The saved TimeActivity ID is invalid.")
    row = client.request(f"timeactivity/{activity_id}").get("TimeActivity", {})
    expected = receipt["payload"]
    if not (
        str(row.get("Id", "")) == activity_id
        and row.get("NameOf") == expected["NameOf"]
        and str(row.get("EmployeeRef", {}).get("value")) == expected["EmployeeRef"]["value"]
        and row.get("TxnDate") == expected["TxnDate"]
        and row.get("Hours", 0) == expected["Hours"]
        and row.get("Minutes") == expected["Minutes"]
        and row.get("BillableStatus") == expected["BillableStatus"]
        and row.get("Description") == expected["Description"]
    ):
        raise SandboxError("The sandbox TimeActivity readback differed from the synthetic request.")
    return {"id": activity_id, "date": row["TxnDate"], "minutes": row["Minutes"], "billable": False}



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault-name", required=True)
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--azure-cli-python", type=Path, default=Path(sys.executable))
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--time-zone", default="UTC")
    options = parser.parse_args()

    try:
        day = datetime.now(ZoneInfo(options.time_zone)).date()
    except (ValueError, ZoneInfoNotFoundError):
        raise SandboxError("Provide an available IANA time zone.") from None

    root = options.state_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    with connection_lock(root):
        vault = SandboxVault(options.vault_name, options.subscription, options.azure_cli_python)
        realm, token = get_sandbox_access(vault)
        client = SandboxClient(realm, token)
        receipt = root / f"timeactivity-{client.realm}-receipt.json"
        result = run(client, receipt, day)
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except (SandboxError, SandboxOAuthError) as problem:
        raise SystemExit(str(problem)) from None
