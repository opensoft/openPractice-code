"""Suggest facility details from the public CMS NPPES NPI Registry."""

import json
import re
from http.client import HTTPException
from urllib.parse import urlencode
from urllib.request import Request, urlopen


NPI_PATTERN = r"[0-9]{10}"
REGISTRY_API = "https://npiregistry.cms.hhs.gov/api/"
REGISTRY_ERRORS = (OSError, HTTPException, ValueError, TypeError, KeyError, AttributeError)
UNAVAILABLE = "The NPI Registry is unavailable. You can enter the facility details manually."
SEARCH_LIMIT = 50
MATCH_LIMIT = 12
STREET_ABBREVIATIONS = {
    "STREET": "ST", "AVENUE": "AVE", "ROAD": "RD", "BOULEVARD": "BLVD",
    "DRIVE": "DR", "LANE": "LN", "COURT": "CT", "PARKWAY": "PKWY",
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W", "SUITE": "STE",
}
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


def _facility_type(taxonomy):
    if re.fullmatch(r"3336[A-Z0-9]{5}X", taxonomy):
        return "pharmacy"
    return FACILITY_TAXONOMIES.get(taxonomy, "")


def _registry_records(**criteria):
    query = urlencode({"version": "2.1", **criteria})
    request = Request(f"{REGISTRY_API}?{query}", headers={"Accept": "application/json"})
    with urlopen(request, timeout=5) as response:
        result = json.loads(response.read(1_048_576))
    if not isinstance(result, dict) or not isinstance(result.get("results"), list):
        raise ValueError("Invalid registry response.")
    return result["results"]


def _primary_location(record):
    return next((
        address for address in record.get("addresses", []) if address.get("address_purpose") == "LOCATION"
    ), {})


def _facility_fields(record, location):
    legal_name = _text(record["basic"], "organization_name")
    dba = next((
        _text(alias, "organization_name") for alias in record.get("other_names", [])
        if alias.get("type") == "Doing Business As" and _text(alias, "organization_name")
    ), legal_name)
    postal_code = _text(location, "postal_code")
    if location.get("country_code") == "US" and re.fullmatch(r"[0-9]{9}", postal_code):
        postal_code = f"{postal_code[:5]}-{postal_code[5:]}"
    primary_taxonomy = next((item for item in record.get("taxonomies", []) if item.get("primary")), {})
    data = {
        "name": dba,
        "legal_name": legal_name,
        "facility_type": _facility_type(_text(primary_taxonomy, "code")),
        "street_address": _text(location, "address_1"),
        "city": _text(location, "city"),
        "region": _text(location, "state"),
        "postal_code": postal_code,
    }
    fields = {key: value for key, value in data.items() if value}
    if "street_address" in fields:
        fields["address_line_2"] = _text(location, "address_2")
    return fields


def lookup_facility(npi):
    if not re.fullmatch(NPI_PATTERN, npi):
        raise ValueError("Enter a 10-digit NPI.")
    try:
        records = _registry_records(number=npi)
        if not records:
            return {"match": "not_found"}
        record = records[0]
        if len(records) != 1 or str(record.get("number")) != npi:
            raise ValueError("Registry response did not match the requested NPI.")
        if record["enumeration_type"] != "NPI-2":
            return {"match": "individual"} if record["enumeration_type"] == "NPI-1" else {"match": "not_found"}
        if record["basic"].get("status") == "D":
            return {"match": "inactive"}
        return {"match": "organization", "fields": _facility_fields(record, _primary_location(record))}
    except REGISTRY_ERRORS as error:
        raise RegistryUnavailable(UNAVAILABLE) from error


def _address_tokens(value):
    return tuple(STREET_ABBREVIATIONS.get(token, token) for token in re.findall(r"[A-Z0-9]+", value.upper()))


def _address_score(location, criteria):
    street = _address_tokens(criteria["street_address"])
    suite = _address_tokens(criteria["address_line_2"])
    address = _address_tokens(_text(location, "address_1"))
    unit = _address_tokens(_text(location, "address_2")) or address[len(street):]
    return (
        bool(street) and address[:len(street)] == street,
        bool(suite) and unit == suite,
        bool(criteria["postal_code"]) and _text(location, "postal_code").startswith(criteria["postal_code"]),
        bool(criteria["city"]) and _text(location, "city").casefold() == criteria["city"].casefold(),
    )


def search_facilities(criteria):
    names = list(dict.fromkeys(criteria[key].upper() for key in ("legal_name", "name") if criteria[key]))
    postal_code = criteria["postal_code"]
    if re.fullmatch(r"[0-9]{5}-?[0-9]{4}", postal_code):
        postal_code = postal_code[:5]
    criteria = {**criteria, "postal_code": postal_code}
    filters = {
        "city": criteria["city"], "postal_code": postal_code,
        "state": criteria["region"].upper() if re.fullmatch(r"[A-Za-z]{2}", criteria["region"]) else "",
    }
    if not (names or filters["city"] or filters["postal_code"]):
        raise ValueError("Enter a facility name, city, or ZIP code to look up an NPI.")

    params = {"enumeration_type": "NPI-2", "address_purpose": "LOCATION", "limit": SEARCH_LIMIT}
    params.update({key: value for key, value in filters.items() if value})
    address_search = criteria["street_address"] and (filters["city"] or filters["postal_code"])
    search_names = [*names, ""] if not names or address_search else names
    matches = []
    limited = False
    try:
        for name in search_names:
            query = {**params}
            if name:
                query["organization_name"] = name + "*" if len(name) >= 3 and not name.endswith("*") else name
            records = _registry_records(**query)
            limited = limited or len(records) >= SEARCH_LIMIT
            for record in records:
                npi = str(record.get("number"))
                if (record.get("enumeration_type") != "NPI-2" or record["basic"].get("status") == "D"
                        or not re.fullmatch(NPI_PATTERN, npi)):
                    continue
                locations = [_primary_location(record), *record.get("practiceLocations", [])]
                location = max(locations, key=lambda candidate: _address_score(candidate, criteria))
                score = _address_score(location, criteria)
                if not name and criteria["street_address"] and not score[0]:
                    continue
                fields = _facility_fields(record, location)
                matches.append(((*score, fields.get("facility_type") == criteria["facility_type"]), {
                    "npi": npi, "fields": fields,
                }))
            if matches:
                break
    except REGISTRY_ERRORS as error:
        raise RegistryUnavailable(UNAVAILABLE) from error

    matches.sort(key=lambda candidate: candidate[0], reverse=True)
    return {"matches": [match for _, match in matches[:MATCH_LIMIT]], "limited": limited or len(matches) > MATCH_LIMIT}
