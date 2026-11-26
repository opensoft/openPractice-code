"""Read the billing subset of a charting system's clinical facts response."""

import re
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256


CONTRACT = "openpractice-ehr-clinical-facts"
ICD_10_CM = "http://hl7.org/fhir/sid/icd-10-cm"


@dataclass(frozen=True)
class BillingFacts:
    subscriber_ref: str
    service_date: date
    diagnoses: tuple[str, ...]
    revision: str


def read_billing_facts(encounter_ref, response):
    if (response.get("contract"), response.get("contract_version"), response.get("message_type")) != (
        CONTRACT, "1.0", "facts.result"
    ):
        raise ValueError("The charting adapter did not return an admitted billing facts response.")
    payload = response.get("payload") or {}
    encounter = payload.get("encounter") or {}
    final = payload.get("documentation_final") or {}
    if (
        payload.get("result") != "facts"
        or encounter.get("id") != encounter_ref
        or encounter.get("status") != "finished"
        or final.get("final") is not True
        or not final.get("signer_ref")
        or not final.get("signed_at")
    ):
        raise ValueError("The charting system must supply finalized, signed billing facts.")
    if set(payload) - {"result", "encounter", "conditions", "documentation_final"}:
        raise ValueError("The charting adapter must supply only the diagnosis billing subset.")

    subject = response.get("subject") or {}
    subscriber = subject.get("openpractice_patient") or {}
    if subscriber.get("namespace") != "openpractice" or not subscriber.get("value") or not response.get("link_id"):
        raise ValueError("The charting response needs a linked billing subscriber.")

    start = (encounter.get("period") or {}).get("start") or ""
    try:
        service_start = datetime.fromisoformat(start)
    except ValueError:
        raise ValueError("The charting response needs a valid service date.") from None
    if service_start.tzinfo is None:
        raise ValueError("The charting service date needs a time zone.")

    conditions = payload.get("conditions") or []
    if not 1 <= len(conditions) <= 12:
        raise ValueError("The charting system must provide one to twelve confirmed diagnoses.")
    ranked = {}
    for condition in conditions:
        rank = condition.get("rank")
        coding = condition.get("code") or {}
        code = coding.get("code") or ""
        if (
            condition.get("encounter_ref") != encounter_ref
            or condition.get("category") != "encounter-diagnosis"
            or not isinstance(rank, int)
            or rank in ranked
            or set(coding) != {"system", "code"}
            or coding["system"] != ICD_10_CM
            or not re.fullmatch(r"[A-Z][0-9][A-Z0-9](?:\.?[A-Z0-9]{1,4})?", code)
        ):
            raise ValueError("The charting response needs ranked ICD-10-CM encounter diagnoses.")
        ranked[rank] = (code, condition.get("id"), condition.get("version"))
    if set(ranked) != set(range(1, len(conditions) + 1)):
        raise ValueError("Chart diagnosis ranks must start at one and be consecutive.")

    ordered = [ranked[rank] for rank in sorted(ranked)]
    revision = sha256(repr((
        response.get("issuer"), response["link_id"], subscriber["value"],
        encounter_ref, encounter.get("version"), start, ordered,
    )).encode()).hexdigest()
    return BillingFacts(subscriber["value"], service_start.date(), tuple(row[0] for row in ordered), revision)
