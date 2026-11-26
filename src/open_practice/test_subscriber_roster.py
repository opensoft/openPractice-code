"""Preloaded synthetic administrative data for the Colorado test slice."""

import re


def get_test_subscriber(subscriber_ref):
    match = re.fullmatch(r"BILLING-TEST-([0-9]{6})", subscriber_ref or "")
    if not match or not 1 <= int(match.group(1)) <= 25:
        raise ValueError("The charting handoff has no matching billing subscriber.")
    number = match.group(1)
    return {
        "patient_first_name": "EXAMPLE",
        "patient_last_name": f"PATIENT{int(number)}",
        "date_of_birth": "1950-01-01",
        "administrative_sex": "F",
        "address": "20 EXAMPLE AVE",
        "city": "DENVER",
        "state": "CO",
        "postal_code": "80201",
        "member_id": f"SYNTH{number}",
    }
