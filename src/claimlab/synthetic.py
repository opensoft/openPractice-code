"""Original synthetic identities and claim factories shared by the sandbox and fixtures."""

DEMO_MEMBER_IDS = tuple(f"SYNTH{day:05d}" for day in range(1, 26))
WIDE_MEMBER_COUNT = 250
REPEAT_MEMBER_COUNT = 10
MEMBER_COUNT = WIDE_MEMBER_COUNT + REPEAT_MEMBER_COUNT

TEST_SUBSCRIBERS = {
    member_id: {"first": "TAYLOR", "last": "DEMO", "dob": f"196001{day:02d}"}
    for day, member_id in enumerate(DEMO_MEMBER_IDS, 1)
}

first_names = ("ALEX", "CASEY", "JAMIE", "JORDAN", "MORGAN", "RILEY", "ROWAN", "SKYLER")
last_names = ("ASH", "BROOK", "DALE", "FIELD", "GLEN", "HILL", "LAKE", "VALE")
for ordinal in range(len(DEMO_MEMBER_IDS) + 1, MEMBER_COUNT + 1):
    TEST_SUBSCRIBERS[f"SYNTH{ordinal:05d}"] = {
        "first": first_names[ordinal % len(first_names)],
        "last": "DEMO-" + last_names[(ordinal // len(first_names)) % len(last_names)],
        "dob": f"{1948 + ordinal % 20:04d}{1 + ordinal % 12:02d}{1 + ordinal % 28:02d}",
    }


def synthetic_claim(member_id, claim_id, lines):
    return {
        "patient": {
            **TEST_SUBSCRIBERS[member_id],
            "member_id": member_id,
            "address": "2 Example Way", "city": "Denver", "state": "CO", "zip": "80202", "sex": "U",
        },
        "payer": {"name": "Demo Medicare Part B", "id": "04112"},
        "claim": {"id": claim_id, "pos": "60", "diagnoses": ["Z23"], "lines": lines},
    }


def synthetic_interchange(claims):
    return {
        "submitter": {"id": "CLAIMLABTEST", "name": "ClaimLab Demo", "contact": "Test Contact", "phone": "3035550100"},
        "receiver": {"id": "04112", "name": "Demo Medicare Part B", "qualifier": "27"},
        "billing_provider": {
            "name": "ClaimLab Demo Pharmacy", "npi": "0000000000", "tax_id": "000000000",
            "address": "1 Example Way", "city": "Denver", "state": "CO", "zip": "802020000",
        },
        "request_interchange_ack": True,
        "claims": claims,
    }
