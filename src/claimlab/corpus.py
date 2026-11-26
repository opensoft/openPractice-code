"""Generate seeded ClaimLab 837Ps and verify them against expected acknowledgments."""

import argparse
import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from . import x837p
from .carrier_profile import load_profile
from .sandbox import MAX_BYTES, simulate
from .synthetic import TEST_SUBSCRIBERS, WIDE_MEMBER_COUNT, synthetic_claim, synthetic_interchange


DEFAULT_SEED = 837
BATCH_SIZE = 100
TRANSMISSION_TIME = datetime(2026, 11, 26, 12)
ACK_KEYS = {"TA1": "interchange_ack", "999": "file_ack", "277CA": "claim_ack"}
CLAIM_FAULTS = {
    "unknown-subscriber": "TEST_MEMBER_UNKNOWN",
    "wrong-birth-date": "TEST_DEMOGRAPHICS_MISMATCH",
    "missing-service": "SERVICE_REQUIRED",
    "unbalanced-charge": "CLAIM_TOTAL_OUT_OF_BALANCE",
}
FILE_FAULTS = {
    "interchange-mismatch": ("TA1", {"code": "INTERCHANGE_CONTROL_MISMATCH", "ta105": "001"}),
    "wrong-group-count": ("TA1", {"code": "INTERCHANGE_GROUP_COUNT", "ta105": "021"}),
    "missing-clm02": ("999", {"code": "CLM02_REQUIRED"}),
    "nonnumeric-clm02": ("999", {"code": "CLM02_INVALID"}),
    "missing-sv102": ("999", {"code": "SV102_REQUIRED"}),
    "wrong-se-count": ("999", {"code": "837P_RULE"}),
}


@dataclass(frozen=True)
class ClaimCase:
    member_id: str
    claim_id: str
    lines: list
    fault: str | None = None
    pos: str = "60"
    diagnoses: tuple[str, ...] = ("Z23",)


def bulk_batches(seed):
    rng = Random(seed)
    service_rng = Random(f"{seed}:carrier")
    profile = load_profile()
    members = list(TEST_SUBSCRIBERS)
    cohorts = (
        ("varied", members[:WIDE_MEMBER_COUNT], 100),
        ("repeated", members[WIDE_MEMBER_COUNT:], 1_000),
    )
    claim_number = control = 0
    for cohort, population, claims_per_member in cohorts:
        plans = [(member, visit) for member in population for visit in range(claims_per_member)]
        rejects = {
            (member, visit) for member in population
            for visit in rng.sample(range(claims_per_member), claims_per_member * 3 // 10)
        }
        faults = list(CLAIM_FAULTS) * (len(rejects) // len(CLAIM_FAULTS))
        rng.shuffle(faults)
        if cohort == "varied":
            rng.shuffle(plans)
        for offset in range(0, len(plans), BATCH_SIZE):
            control += 1
            cases = []
            for member, visit in plans[offset:offset + BATCH_SIZE]:
                claim_number += 1
                service_date = date(2026, 1, 1) + timedelta(days=service_rng.randrange(320))
                pos, lines = profile.service(service_rng, service_date.strftime("%Y%m%d"))
                fault = faults.pop() if (member, visit) in rejects else None
                cases.append(ClaimCase(member, f"CLAB-B{claim_number:07d}", lines, fault,
                                       pos=pos, diagnoses=("E119",)))
            filename = f"bulk/{cohort}-{offset // BATCH_SIZE + 1:04d}.837"
            yield filename, control, cases, None


def regression_batches():
    member = "SYNTH00001"
    lines = [
        {"cpt": "G0008", "charge": Decimal("17.15"), "date": "20261120"},
        {"cpt": "G0009", "charge": Decimal("20.35"), "date": "20261120"},
    ]
    faults = [None, *FILE_FAULTS, *CLAIM_FAULTS]
    for index, fault in enumerate(faults, 1):
        filename = f"regression/{fault or 'accepted'}.837"
        case = ClaimCase(member, f"CLAB-R{index:03d}-1", lines)
        cases = [case]
        if fault in CLAIM_FAULTS:
            # Keep the first claim valid to verify that each rejection stays claim-level.
            cases.append(ClaimCase("SYNTH00002", f"CLAB-R{index:03d}-2", lines, fault))
        yield filename, 50_000 + index, cases, fault if fault in FILE_FAULTS else None


def render_batch(control, cases, file_fault):
    claims = []
    unbalanced = set()
    for case in cases:
        lines = [] if case.fault == "missing-service" else case.lines
        claim = synthetic_claim(case.member_id, case.claim_id, lines)
        claim["claim"].update(pos=case.pos, diagnoses=list(case.diagnoses))
        if case.fault == "unknown-subscriber":
            claim["patient"]["member_id"] = "SYNTH99999"
        elif case.fault == "wrong-birth-date":
            claim["patient"]["dob"] = "19000101"
        elif case.fault == "unbalanced-charge":
            unbalanced.add(case.claim_id)
        claims.append(claim)

    data = synthetic_interchange(claims)
    if any(case.claim_id.startswith("CLAB-B") for case in cases):
        data["billing_provider"]["name"] = "ClaimLab Demo Clinic"
    data["batch_ref"] = f"CLAB-{control:09d}"
    segments = x837p.build_segments(data, ctrl=control, now=TRANSMISSION_TIME)
    if unbalanced:
        for index, segment in enumerate(segments):
            if segment.startswith("CLM*"):
                kind, claim_id, total, rest = segment.split("*", 3)
                if claim_id in unbalanced:
                    wrong_total = x837p.amt(Decimal(total) + Decimal("0.01"))
                    segments[index] = "*".join((kind, claim_id, wrong_total, rest))

    if file_fault == "interchange-mismatch":
        segments[-1] = x837p.seg("IEA", 1, f"{control + 1:09d}")
    elif file_fault == "wrong-group-count":
        segments[-1] = x837p.seg("IEA", 2, f"{control:09d}")
    elif file_fault == "wrong-se-count":
        kind, count, rest = segments[-3].split("*", 2)
        segments[-3] = "*".join((kind, str(int(count) + 1), rest))
    elif file_fault in {"missing-clm02", "nonnumeric-clm02", "missing-sv102"}:
        target = "SV1*" if file_fault == "missing-sv102" else "CLM*"
        index = next(index for index, segment in enumerate(segments) if segment.startswith(target))
        kind, first, _, rest = segments[index].split("*", 3)
        amount = "INVALID" if file_fault == "nonnumeric-clm02" else ""
        segments[index] = "*".join((kind, first, amount, rest))
    return ("\n".join(segments) + "\n").encode("ascii")


def expected_batch(filename, control, cases, file_fault, payload):
    acknowledgments = {
        "TA1": {"status": "not_generated", "issues": []},
        "999": {"status": "accepted", "issues": []},
        "277CA": {"status": "accepted", "issues": []},
    }
    claims = [
        {
            "number": number,
            "claim_id": case.claim_id,
            "source_subscriber_id": case.member_id,
            "scenario": file_fault or case.fault or "accepted",
            "status": "blocked" if file_fault else "rejected" if case.fault else "accepted",
            "issues": [{"code": CLAIM_FAULTS[case.fault]}] if case.fault else [],
        }
        for number, case in enumerate(cases, 1)
    ]
    if file_fault:
        stage, diagnostic = FILE_FAULTS[file_fault]
        acknowledgments["277CA"]["status"] = "not_generated"
        if stage == "TA1":
            acknowledgments["999"]["status"] = "not_generated"
        acknowledgments[stage] = {"status": "rejected", "issues": [diagnostic]}
    else:
        statuses = {claim["status"] for claim in claims}
        acknowledgments["277CA"]["status"] = statuses.pop() if len(statuses) == 1 else "mixed"
    return {
        "path": filename,
        "interchange_control": f"{control:09d}",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "acknowledgments": acknowledgments,
        "claims": claims,
    }


def summarize(files):
    summaries = {}
    for suite in sorted({entry["path"].split("/", 1)[0] for entry in files}):
        entries = [entry for entry in files if entry["path"].startswith(suite + "/")]
        claims = [claim for entry in entries for claim in entry["claims"]]
        outcomes = Counter(claim["status"] for claim in claims)
        reasons = Counter(claim["scenario"] for claim in claims if claim["status"] == "rejected")
        summaries[suite] = {
            "files": len(entries), "claims": len(claims), "accepted": outcomes["accepted"],
            "rejected": outcomes["rejected"], "blocked": outcomes["blocked"],
            "rejects_by_scenario": dict(sorted(reasons.items())),
        }
    return summaries


def generate(destination, *, seed=DEFAULT_SEED, suite="all"):
    if suite not in {"all", "bulk", "regression"}:
        raise ValueError(f"Unknown suite: {suite}")
    destination = Path(destination).absolute()
    if destination.exists():
        raise FileExistsError(f"Choose a new output directory: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    files = []
    with TemporaryDirectory(prefix="claimlab-", dir=destination.parent) as temporary:
        output = Path(temporary) / "corpus"
        output.mkdir()
        batches = []
        if suite in {"all", "regression"}:
            batches.append(regression_batches())
        if suite in {"all", "bulk"}:
            batches.append(bulk_batches(seed))
        for batch in batches:
            for filename, control, cases, fault in batch:
                payload = render_batch(control, cases, fault)
                if len(payload) > MAX_BYTES:
                    raise ValueError(f"{filename} exceeds the 1 MiB sandbox upload limit.")
                path = output / filename
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(payload)
                files.append(expected_batch(filename, control, cases, fault, payload))
        manifest = {"schema_version": 1, "seed": seed, "summaries": summarize(files), "files": files}
        if suite in {"all", "bulk"}:
            manifest["carrier_profile"] = load_profile().reference
        (output / "manifest.json").write_bytes((json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
        output.rename(destination)
    return manifest


def diagnostic(issues):
    return [{key: item[key] for key in ("code", "ta105") if key in item} for item in issues]


def verify(directory, *, api=None):
    root = Path(directory).resolve()
    manifest = json.loads((root / "manifest.json").read_bytes())
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported manifest schema version.")
    files = manifest["files"]
    if not files or len({entry["path"] for entry in files}) != len(files):
        raise ValueError("The manifest must list distinct files.")
    summary = summarize(files)
    if summary != manifest["summaries"]:
        raise ValueError("Manifest summary does not match its expected results.")
    if api:
        url = urlsplit(api)
        if url.scheme not in {"http", "https"} or not url.netloc or url.path not in {"", "/"} or url.query or url.fragment:
            raise ValueError("Provide the sandbox base URL, such as http://127.0.0.1:8765.")

    for entry in files:
        filename = entry["path"]
        path = (root / filename).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"File is outside the corpus: {filename}")
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != entry["sha256"]:
            raise ValueError(f"{filename}: file digest differs from the manifest.")
        segments = x837p.parse(payload.decode("ascii"))
        claim_ids = [segment[1] for segment in segments if segment[0] == "CLM"]
        if claim_ids != [claim["claim_id"] for claim in entry["claims"]]:
            raise ValueError(f"{filename}: claim identities differ from the manifest.")
        if segments[0][13] != entry["interchange_control"]:
            raise ValueError(f"{filename}: interchange control differs from the manifest.")

        if api:
            request = Request(api.rstrip("/") + "/api/v1/validate", data=payload,
                              headers={"Content-Type": "application/edi-x12"})
            with urlopen(request, timeout=15) as response:
                actual = json.load(response)
        else:
            actual = simulate(payload)
        expected = entry["acknowledgments"]
        if actual.get("simulated") is not True or actual.get("accepted") != (expected["277CA"]["status"] == "accepted"):
            raise ValueError(f"{filename}: overall outcome differs from the manifest.")
        for stage, key in ACK_KEYS.items():
            acknowledgment = actual[key]
            if {"status": acknowledgment["status"], "issues": diagnostic(acknowledgment.get("issues", []))} != expected[stage]:
                raise ValueError(f"{filename}: {stage} outcome differs from the manifest.")

        expected_claims = [
            {key: claim[key] for key in ("number", "status", "issues")}
            for claim in entry["claims"] if claim["status"] != "blocked"
        ]
        actual_claims = [
            {"number": claim["number"], "status": claim["status"], "issues": diagnostic(claim["issues"])}
            for claim in actual["claim_ack"]["claims"]
        ]
        if actual_claims != expected_claims:
            raise ValueError(f"{filename}: claim outcomes differ from the manifest.")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("generate", help="Write fixtures and their expected outcomes.")
    create.add_argument("directory", type=Path)
    create.add_argument("--seed", type=int, default=DEFAULT_SEED)
    create.add_argument("--suite", choices=("all", "bulk", "regression"), default="all")
    check = commands.add_parser("verify", help="Compare every fixture with the manifest.")
    check.add_argument("directory", type=Path)
    check.add_argument("--api", help="Verify through a running ClaimLab sandbox instead of in-process.")
    args = parser.parse_args()
    try:
        if args.command == "generate":
            summaries = generate(args.directory, seed=args.seed, suite=args.suite)["summaries"]
        else:
            summaries = verify(args.directory, api=args.api)
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(summaries, indent=2))


if __name__ == "__main__":
    main()
