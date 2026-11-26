"""Synthetic administrative data for the Colorado test slice."""

import csv
import io
import re
from dataclasses import dataclass
from hashlib import sha256


SUBSCRIBER_REF = re.compile(r"BILLING-TEST-([0-9]{6})")
MBI_LETTERS = "AC-HJ-KM-NP-RT-Y"
MBI = re.compile(
    rf"[1-9][{MBI_LETTERS}][0-9{MBI_LETTERS}][0-9][{MBI_LETTERS}]"
    rf"[0-9{MBI_LETTERS}][0-9][{MBI_LETTERS}]{{2}}[0-9]{{2}}"
)
ROSTER_COLUMNS = {
    "subscriber_ref", "patient_first_name", "patient_last_name", "date_of_birth",
    "administrative_sex", "address", "city", "state", "postal_code", "member_id",
}


@dataclass(frozen=True)
class ApprovedSyntheticRoster:
    approval_reference: str
    sha256: str
    subscribers: dict[str, dict[str, str]]

    def get_subscriber(self, subscriber_ref):
        if subscriber_ref not in self.subscribers:
            raise ValueError("The approved synthetic roster has no matching billing subscriber.")
        return self.subscribers[subscriber_ref]


def load_approved_synthetic_roster(path, approval_reference):
    if not isinstance(approval_reference, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}", approval_reference
    ):
        raise ValueError("Configure the Novitas synthetic test roster approval reference.")

    subscribers = {}
    try:
        raw = path.read_bytes()
        csv_rows = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
        columns = csv_rows.fieldnames or []
        if len(columns) != len(ROSTER_COLUMNS) or set(columns) != ROSTER_COLUMNS:
            raise ValueError("The approved synthetic roster must use the subscriber-roster.csv headers.")
        for row in csv_rows:
            if None in row or any(not value or not value.strip() for value in row.values()):
                raise ValueError(f"The approved synthetic roster has an incomplete row at line {csv_rows.line_num}.")
            subscriber = {key: value.strip() for key, value in row.items()}
            subscriber_ref = subscriber.pop("subscriber_ref")
            match = SUBSCRIBER_REF.fullmatch(subscriber_ref)
            if not match or not 1 <= int(match.group(1)) <= 25 or subscriber_ref in subscribers:
                raise ValueError(f"The approved synthetic roster has an invalid or duplicate reference at line {csv_rows.line_num}.")
            subscriber["member_id"] = subscriber["member_id"].upper()
            if not MBI.fullmatch(subscriber["member_id"]):
                raise ValueError(f"The approved synthetic roster has an invalid MBI format at line {csv_rows.line_num}.")
            subscribers[subscriber_ref] = subscriber
    except (OSError, UnicodeError, csv.Error):
        raise ValueError("The approved synthetic roster must be a readable UTF-8 CSV file.") from None
    if not subscribers:
        raise ValueError("The approved synthetic roster is empty.")
    return ApprovedSyntheticRoster(approval_reference, sha256(raw).hexdigest(), subscribers)


def get_test_subscriber(subscriber_ref):
    match = SUBSCRIBER_REF.fullmatch(subscriber_ref or "")
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
