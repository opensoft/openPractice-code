"""Map approved billing cases to a Colorado Medicare Part B 837P."""

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from claimlab import x837p
from open_practice.mock_openchart import read_mock_billing_facts
from open_practice.test_subscriber_roster import get_test_subscriber


PAYER_ID = "04112"


def _field(values, key, pattern=r"[ -~]+", limit=60):
    value = str(values.get(key) or "").strip().upper()
    if len(value) > limit or re.fullmatch(pattern, value) is None or any(c in value for c in "*:~^"):
        raise ValueError(f"Invalid or missing {key.replace('_', ' ')}.")
    return value


def _date(values, key):
    try:
        return date.fromisoformat(str(values.get(key)))
    except ValueError:
        raise ValueError(f"Invalid or missing {key.replace('_', ' ')}.") from None


def _address(values):
    return {
        "address": _field(values, "address"),
        "city": _field(values, "city", limit=30),
        "state": _field(values, "state", r"[A-Z]{2}"),
        "zip": _field(values, "postal_code", r"[0-9]{5}(?:[0-9]{4})?"),
    }


def _charge(line, service_date, diagnosis_count):
    try:
        charge = Decimal(str(line.get("charge")))
    except InvalidOperation:
        raise ValueError("A service line needs a valid charge.") from None
    if not charge.is_finite() or not 0 < charge <= Decimal("99999.90") or charge != round(charge, 2):
        raise ValueError("Service line charges must be positive amounts in cents within Medicare's limit.")
    units = str(line.get("units") or "")
    if not re.fullmatch(r"[1-9][0-9]{0,3}", units):
        raise ValueError("Service line units must be a positive whole number.")
    pointers = str(line.get("diagnosis_pointers") or "")
    if not re.fullmatch(r"[1-9][0-9]?(?: *, *[1-9][0-9]?){0,3}", pointers):
        raise ValueError("Enter one to four comma-separated diagnosis pointers for each line.")
    pointers = [int(pointer) for pointer in pointers.split(",")]
    if max(pointers) > diagnosis_count or len(pointers) != len(set(pointers)):
        raise ValueError("Service line diagnosis pointers must identify distinct diagnoses from the charting handoff.")
    modifiers = [part.strip().upper() for part in (line.get("modifiers") or "").split(",") if part.strip()]
    if len(modifiers) > 4 or any(re.fullmatch(r"[A-Z0-9]{2}", part) is None for part in modifiers):
        raise ValueError("A service line supports up to four two-character modifiers.")
    return {
        "cpt": _field(line, "procedure_code", r"[A-Z0-9]{5}"),
        "charge": charge,
        "units": units,
        "date": service_date.strftime("%Y%m%d"),
        "dx_pointers": pointers,
        "modifiers": modifiers,
    }


def case_to_claim(case, *, today):
    if not case.get("coverage_verified"):
        raise ValueError("Verify primary Medicare Part B coverage for the date of service.")
    facts = read_mock_billing_facts(case.get("chart_encounter_ref"))
    if (case.get("subscriber_ref"), case.get("chart_revision")) != (facts.subscriber_ref, facts.revision):
        raise ValueError("The charting handoff changed. Review the latest facts before release.")
    subscriber = get_test_subscriber(facts.subscriber_ref)
    service_date = facts.service_date
    birth_date = _date(subscriber, "date_of_birth")
    if not birth_date <= service_date <= today:
        raise ValueError("Service date must fall between the patient's date of birth and today.")
    diagnoses = list(facts.diagnoses)
    lines = [_charge(line, service_date, len(diagnoses)) for line in case.get("charges") or []]
    if not lines:
        raise ValueError("At least one service line is required.")
    if sum(line["charge"] for line in lines) > Decimal("99999.90"):
        raise ValueError("Total claim charges exceed Medicare's limit.")
    return {
        "patient": {
            "first": _field(subscriber, "patient_first_name", limit=35),
            "last": _field(subscriber, "patient_last_name"),
            "member_id": _field(subscriber, "member_id", r"[A-Z0-9]{11}"),
            "dob": birth_date.strftime("%Y%m%d"),
            "sex": _field(subscriber, "administrative_sex", r"[FM]"),
            **_address(subscriber),
        },
        "payer": {"name": "MEDICARE PART B", "id": PAYER_ID, "filing": "MB"},
        "claim": {"id": _field(case, "name", r"[A-Z0-9-]{1,17}"), "pos": "11", "diagnoses": diagnoses, "lines": lines},
    }


def build_test_batch(settings, cases, *, control_number, now):
    data = prepare_test_batch(settings, cases, control_number=control_number, now=now)
    payload = "".join(x837p.build_segments(data, ctrl=control_number, test=True, now=now)).encode("ascii")
    validate_test_batch(payload)
    return payload


def prepare_test_batch(settings, cases, *, control_number, now):
    """Export original office-service claims for one enrolled Colorado billing provider."""
    if type(control_number) is not int or not 1 <= control_number <= 999_999_999:
        raise ValueError("Interchange control number must be between 1 and 999999999.")
    if not cases:
        raise ValueError("Select at least one approved billing case.")
    claims = [case_to_claim(case, today=now.date()) for case in cases]
    if len({row["claim"]["id"] for row in claims}) != len(claims):
        raise ValueError("Each billing case may appear once in a claim batch.")

    entity = _field(settings, "provider_type", r"INDIVIDUAL|ORGANIZATION")
    tax_type = _field(settings, "tax_qualifier", r"EI|SY")
    if entity == "ORGANIZATION" and tax_type != "EI":
        raise ValueError("An organization must use an EIN.")
    address = _address(settings)
    if address["state"] != "CO" or len(address["zip"]) != 9:
        raise ValueError("Enter the Colorado billing provider address with its nine-digit ZIP code.")
    billing = {
        **address,
        "name": _field(settings, "provider_name"),
        "entity_type": "1" if entity == "INDIVIDUAL" else "2",
        "npi": _field(settings, "billing_npi", r"[0-9]{10}"),
        "tax_id": _field(settings, "tax_id", r"[0-9]{9}"),
        "tax_qualifier": tax_type,
    }
    if entity == "INDIVIDUAL":
        billing["first"] = _field(settings, "provider_first_name", limit=35)
    if settings.get("billing_taxonomy"):
        billing["taxonomy"] = _field(settings, "billing_taxonomy", r"[A-Z0-9]{10}")
    if entity == "ORGANIZATION":
        rendering = {
            "first": _field(settings, "rendering_first_name", limit=35),
            "last": _field(settings, "rendering_last_name"),
            "npi": _field(settings, "rendering_npi", r"[0-9]{10}"),
        }
        if settings.get("rendering_taxonomy"):
            rendering["taxonomy"] = _field(settings, "rendering_taxonomy", r"[A-Z0-9]{10}")
        for row in claims:
            row["claim"]["rendering_provider"] = rendering

    return {
        "submitter": {
            "id": _field(settings, "submitter_id", r"[A-Z0-9]{1,15}"),
            "name": _field(settings, "submitter_name"),
            "contact": _field(settings, "contact_name"),
            "phone": _field(settings, "contact_phone", r"[0-9]{10}"),
        },
        "receiver": {"name": "NOVITAS SOLUTIONS", "id": PAYER_ID, "qualifier": "27"},
        "billing_provider": billing,
        "batch_ref": f"OPB-{control_number:09d}",
        "request_interchange_ack": True,
        "claims": claims,
    }


def validate_test_batch(payload):
    errors = x837p.validate(payload.decode("ascii"), novitas=True)
    if errors:
        raise ValueError("837P validation failed. " + " ".join(errors))
