"""Synthetic implementation of the openChart billing facts read."""

import re
from uuid import NAMESPACE_URL, uuid5

from open_practice.charting import CONTRACT, ICD_10_CM, read_billing_facts


ISSUER = "openchart-test"


def read_mock_response(encounter_ref):
    match = re.fullmatch(r"OC-MOCK-([0-9]{6})", encounter_ref or "")
    if not match or not 1 <= int(match.group(1)) <= 25:
        raise ValueError("Choose a mock openChart encounter from OC-MOCK-000001 through OC-MOCK-000025.")
    number = match.group(1)
    return {
        "contract": CONTRACT,
        "contract_version": "1.0",
        "message_id": str(uuid5(NAMESPACE_URL, f"{ISSUER}:{encounter_ref}:1")),
        "message_type": "facts.result",
        "issued_at": "2026-11-20T12:00:00-07:00",
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
                "period": {"start": "2026-11-20T10:00:00-07:00"},
            },
            "documentation_final": {
                "final": True,
                "signer_ref": "OC-TEST-CLINICIAN",
                "signed_at": "2026-11-20T11:00:00-07:00",
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
                for rank, code in enumerate(("E11.9", "I10"), start=1)
            ],
        },
    }


def read_mock_billing_facts(encounter_ref):
    return read_billing_facts(encounter_ref, read_mock_response(encounter_ref))
