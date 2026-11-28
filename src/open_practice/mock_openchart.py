"""Synthetic implementation of the openChart billing facts read."""

import re
from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from open_practice.charting import CONTRACT, ICD_10_CM, read_billing_facts


ISSUER = "openchart-test"
FLU_ENCOUNTER_PREFIX = "OC-MOCK-FLU-"
DESK_ENCOUNTER_PREFIX = "OC-MOCK-DESK-"
DESK_ENCOUNTER = re.compile(rf"{DESK_ENCOUNTER_PREFIX}(00000[1-4])-([0-9]{{8}}T[0-9]{{4}})")
DEMO_PATIENT = re.compile(r"demo-0([1-4])")


def is_ready_demo_appointment(appointment):
    return (
        appointment.get("stage") == "wrapped"
        and appointment.get("location") == "clinic"
        and appointment.get("mode") == "in-person"
        and DEMO_PATIENT.fullmatch(appointment.get("patient") or "") is not None
    )


def desk_encounter_reference(appointment):
    if not is_ready_demo_appointment(appointment):
        raise ValueError("Mock billing requires a wrapped in-person appointment for a demo patient at Demo clinic.")
    number = int(DEMO_PATIENT.fullmatch(appointment["patient"])[1])
    start = datetime.fromisoformat(f'{appointment["day"]}T{appointment["slot"]}')
    return f"{DESK_ENCOUNTER_PREFIX}{number:06d}-{start:%Y%m%dT%H%M}"


def read_mock_response(encounter_ref):
    if match := DESK_ENCOUNTER.fullmatch(encounter_ref or ""):
        number, appointment_start = match.groups()
        start = datetime.strptime(appointment_start, "%Y%m%dT%H%M").replace(tzinfo=ZoneInfo("America/Denver"))
        signed_at = (start + timedelta(hours=1)).isoformat()
        start_at = start.isoformat()
    else:
        match = re.fullmatch(r"OC-MOCK-(?:FLU-)?([0-9]{6})", encounter_ref or "")
        if not match or not 1 <= int(match.group(1)) <= 25:
            raise ValueError("Choose a mock openChart encounter with an OC-MOCK or OC-MOCK-FLU reference numbered 000001 through 000025.")
        number = match.group(1)
        start_at = "2026-11-20T10:00:00-07:00"
        signed_at = "2026-11-20T11:00:00-07:00"
    diagnoses = ("Z23",) if encounter_ref.startswith(FLU_ENCOUNTER_PREFIX) else ("E11.9", "I10")
    return {
        "contract": CONTRACT,
        "contract_version": "1.0",
        "message_id": str(uuid5(NAMESPACE_URL, f"{ISSUER}:{encounter_ref}:1")),
        "message_type": "facts.result",
        "issued_at": (datetime.fromisoformat(signed_at) + timedelta(hours=1)).isoformat(),
        "issuer": ISSUER,
        "link_id": str(uuid5(NAMESPACE_URL, f"{ISSUER}:billing-link:{number}")),
        "subject": {
            "openpractice_patient": {"issuer": "openpractice-test", "namespace": "openpractice", "value": f"BILLING-TEST-{number}"},
            "ehr_patient": {"issuer": ISSUER, "namespace": "openchart", "value": f"OC-PATIENT-{number}"},
        },
        "payload": {
            "result": "facts",
            "encounter": {
                "id": encounter_ref,
                "version": 1,
                "status": "finished",
                "period": {"start": start_at},
            },
            "documentation_final": {
                "final": True,
                "signer_ref": "OC-TEST-CLINICIAN",
                "signed_at": signed_at,
            },
            "conditions": [
                {
                    "id": f"{encounter_ref}-DX-{rank}",
                    "version": 1,
                    "category": "encounter-diagnosis",
                    "code": {"system": ICD_10_CM, "code": code},
                    "rank": rank,
                    "encounter_ref": encounter_ref,
                }
                for rank, code in enumerate(diagnoses, start=1)
            ],
        },
    }


def read_mock_billing_facts(encounter_ref):
    return read_billing_facts(encounter_ref, read_mock_response(encounter_ref))
