"""Suggest facility details from the public CMS NPPES NPI Registry."""

import json
import re
from http.client import HTTPException
from urllib.parse import urlencode
from urllib.request import Request, urlopen


NPI_PATTERN = r"[0-9]{10}"
REGISTRY_API = "https://npiregistry.cms.hhs.gov/api/"
FACILITY_TAXONOMIES = {
    "261Q00000X": "office",
    "310400000X": "alf",
    "314000000X": "snf",
    "282N00000X": "hospital",
}


class RegistryUnavailable(RuntimeError):
    """The public registry could not answer the lookup."""


def _text(record, field):
    value = record.get(field)
    return value.strip() if isinstance(value, str) else ""


def lookup_facility(npi):
    if not re.fullmatch(NPI_PATTERN, npi):
        raise ValueError("Enter a 10-digit NPI.")
    query = urlencode({"version": "2.1", "number": npi})
    request = Request(f"{REGISTRY_API}?{query}", headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=5) as response:
            result = json.loads(response.read(1_048_576))
        if not isinstance(result, dict) or not isinstance(result.get("results"), list):
            raise ValueError("Invalid registry response.")
        if not result["results"]:
            return {"match": "not_found"}
        record = result["results"][0]
        if len(result["results"]) != 1 or str(record.get("number")) != npi:
            raise ValueError("Registry response did not match the requested NPI.")
        if record["enumeration_type"] != "NPI-2":
            return {"match": "individual"} if record["enumeration_type"] == "NPI-1" else {"match": "not_found"}
        basic = record["basic"]
        if basic.get("status") == "D":
            return {"match": "inactive"}
        legal_name = _text(basic, "organization_name")
        dba = next((
            _text(alias, "organization_name") for alias in record.get("other_names", [])
            if alias.get("type") == "Doing Business As" and _text(alias, "organization_name")
        ), legal_name)
        location = next((
            address for address in record.get("addresses", []) if address.get("address_purpose") == "LOCATION"
        ), {})
        postal_code = _text(location, "postal_code")
        if location.get("country_code") == "US" and re.fullmatch(r"[0-9]{9}", postal_code):
            postal_code = f"{postal_code[:5]}-{postal_code[5:]}"
        primary_taxonomy = next((item.get("code") for item in record.get("taxonomies", []) if item.get("primary")), None)
        data = {
            "name": dba,
            "legal_name": legal_name,
            "facility_type": FACILITY_TAXONOMIES.get(primary_taxonomy, ""),
            "street_address": _text(location, "address_1"),
            "city": _text(location, "city"),
            "region": _text(location, "state"),
            "postal_code": postal_code,
        }
        fields = {key: value for key, value in data.items() if value}
        if "street_address" in fields:
            fields["address_line_2"] = _text(location, "address_2")
        return {"match": "organization", "fields": fields}
    except (OSError, HTTPException, ValueError, TypeError, KeyError, AttributeError) as error:
        raise RegistryUnavailable("The NPI Registry is unavailable. You can enter the facility details manually.") from error
