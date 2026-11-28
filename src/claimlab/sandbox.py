"""Local browser and HTTP sandbox for synthetic 837P files."""

import argparse
import json
import re
from datetime import datetime
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files

from . import x837p
from .synthetic import DEMO_MEMBER_IDS, TEST_SUBSCRIBERS, synthetic_claim, synthetic_interchange


MAX_BYTES = 1024 * 1024
SANDBOX_SUBSCRIBERS = {
    **TEST_SUBSCRIBERS,
    # CMS's example MBI with the openPractice mock patient's name and DOB.
    "1EG4TE5MK73": {"first": "EXAMPLE", "last": "PATIENT1", "dob": "19500101"},
}
EXAMPLES = {
    "accepted.837", "accepted-25.837", "claim-rejected.837", "file-rejected.837",
    "missing-clm02.837", "invalid-total-charge.837",
}
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/agents": ("agents.html", "text/html; charset=utf-8"),
    "/agents.html": ("agents.html", "text/html; charset=utf-8"),
    "/llms.txt": ("llms.txt", "text/plain; charset=utf-8"),
    "/openapi.json": ("openapi.json", "application/json"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


def issue(code, message):
    return {"code": code, "message": message}


def simulate(payload):
    """Return simulated TA1, 999, and 277CA outcomes without including subscriber data."""
    interchange_issues = []
    file_issues = []
    claims = []
    try:
        edi = payload.decode("ascii")
    except UnicodeDecodeError:
        file_issues.append(issue("ASCII_REQUIRED", "Upload the original ASCII 837P file."))
    else:
        segments = x837p.parse(edi)
        interchange_issues = validate_interchange(edi, segments)
        if not interchange_issues:
            file_issues = validate_file(edi, segments)
            if not file_issues:
                claims = simulate_claims(segments)

    file_status = "rejected" if file_issues else "accepted"
    if interchange_issues:
        file_status = "not_generated"
    claim_status = "not_generated"
    if claims:
        statuses = {claim["status"] for claim in claims}
        claim_status = statuses.pop() if len(statuses) == 1 else "mixed"
    return {
        "simulated": True,
        "accepted": claim_status == "accepted",
        "interchange_ack": {
            "type": "TA1",
            "status": "rejected" if interchange_issues else "not_generated",
            "issues": interchange_issues,
        },
        "file_ack": {"type": "999", "status": file_status, "issues": file_issues},
        "claim_ack": {"type": "277CA", "status": claim_status, "claims": claims},
    }


def validate_interchange(edi, segments):
    if not edi.startswith("ISA*"):
        return [issue("ISA_REQUIRED", "Start the file with an ISA segment.")]
    if edi[82:83] != "^" or edi[104:106] != ":~" or len(segments[0]) != 17:
        return [issue("ISA_LAYOUT", "Check the fixed-width ISA and use *, :, ~, and ^ as delimiters.")]
    interchange_control = edi[90:99]
    if not re.fullmatch(r"[0-9]{9}", interchange_control) or interchange_control == "000000000":
        return [issue("ISA_CONTROL", "Use a positive nine-digit interchange control number in ISA13.")]
    trailer = segments[-1]
    if trailer[0] != "IEA" or len(trailer) != 3:
        return [issue("IEA_REQUIRED", "End the interchange with an IEA group count and control number.")]
    if trailer[2] != interchange_control:
        return [{**issue("INTERCHANGE_CONTROL_MISMATCH", "ISA13 must match IEA02."), "ta105": "001"}]
    group_count = sum(segment[0] == "GS" for segment in segments)
    if not group_count or not re.fullmatch(rf"0*{group_count}", trailer[1]):
        return [{**issue("INTERCHANGE_GROUP_COUNT", "IEA01 must match the number of GS groups."), "ta105": "021"}]
    return []


def validate_file(edi, segments):
    if edi[102:103] != "T":
        return [issue("TEST_FLAG_REQUIRED", "Set ISA15 to T before submitting a synthetic 837P.")]
    if re.search(r"[\x00-\x09\x0b-\x0c\x0e-\x1f\x7f]", edi):
        return [issue("UNSUPPORTED_CHARACTER", "Use printable ASCII and optional line breaks.")]

    for index, segment in enumerate(segments, 1):
        kind = segment[0]
        if not re.fullmatch(r"[A-Z][A-Z0-9]{1,2}", kind):
            return [issue("SEGMENT_ID", f"Check the segment identifier at position {index}.")]
        if kind == "SE" and len(segment) > 1 and not re.fullmatch(r"[0-9]{1,10}", segment[1]):
            return [issue("SE_COUNT", f"Enter a numeric segment count in SE01 at position {index}.")]
        if kind in {"CLM", "SV1"}:
            element = f"{kind}02"
            amount = segment[2] if len(segment) > 2 else ""
            if not amount.strip():
                return [issue(f"{element}_REQUIRED", f"Enter the required charge in {element} at segment {index}.")]
            if len(amount) > 18 or not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", amount):
                return [issue(f"{element}_INVALID", f"Enter a numeric charge in {element} at segment {index}.")]

    return [issue("837P_RULE", message) for message in x837p.validate(edi, novitas=True)]


def simulate_claims(segments):
    claims = []
    subscriber_id = first = last = dob = ""
    claim = None
    in_subscriber_loop = False
    for segment in segments:
        kind = segment[0]
        if kind == "HL":
            subscriber_id = first = last = dob = ""
            claim = None
            in_subscriber_loop = segment[3:4] == ["22"]
        elif in_subscriber_loop and kind == "NM1" and segment[1:2] == ["IL"]:
            fields = segment + [""] * 10
            last, first, subscriber_id = fields[3], fields[4], fields[9]
            dob = ""
        elif kind == "DMG" and subscriber_id and segment[1:2] == ["D8"]:
            dob = segment[2] if len(segment) > 2 else ""
        elif kind == "CLM":
            issues = []
            subscriber = SANDBOX_SUBSCRIBERS.get(subscriber_id)
            if not in_subscriber_loop:
                issues.append(issue("SUBSCRIBER_REQUIRED", "Place the claim under a self-subscriber HL."))
            elif subscriber is None:
                issues.append(issue("TEST_MEMBER_UNKNOWN", "Use an ID from the ClaimLab synthetic roster in Loop 2010BA NM109."))
            elif {"first": first, "last": last, "dob": dob} != subscriber:
                issues.append(issue("TEST_DEMOGRAPHICS_MISMATCH", "Use the name and DOB from the ClaimLab synthetic roster for this member."))
            claim = {"number": len(claims) + 1, "issues": issues, "total": Decimal(segment[2]), "line_total": None}
            claims.append(claim)
        elif kind == "SV1" and claim is not None:
            claim["line_total"] = (claim["line_total"] or Decimal(0)) + Decimal(segment[2])

    for claim in claims:
        total = claim.pop("total")
        line_total = claim.pop("line_total")
        if line_total is None:
            claim["issues"].append(issue("SERVICE_REQUIRED", "Include at least one SV1 service line for this claim."))
        elif total < 0 or total != line_total:
            claim["issues"].append(issue("CLAIM_TOTAL_OUT_OF_BALANCE", "CLM02 must be nonnegative and equal the sum of the SV102 service charges."))
        claim["status"] = "rejected" if claim["issues"] else "accepted"
    return claims


def example(name):
    members = DEMO_MEMBER_IDS if name == "accepted-25.837" else DEMO_MEMBER_IDS[:1]
    claims = [
        synthetic_claim(member_id, f"CLAB-DEMO-{ordinal:03d}", [{"cpt": "G0008", "charge": 30, "date": "20261120"}])
        for ordinal, member_id in enumerate(members, 1)
    ]
    if name == "claim-rejected.837":
        claims[0]["patient"]["member_id"] = "SYNTH99999"
    edi = x837p.build(synthetic_interchange(claims), ctrl=1, now=datetime(2026, 11, 26, 12))
    if name == "file-rejected.837":
        edi = edi.replace("IEA*1*000000001", "IEA*1*000000002")
    elif name == "missing-clm02.837":
        edi = edi.replace("CLM*CLAB-DEMO-001*30*", "CLM*CLAB-DEMO-001**")
    elif name == "invalid-total-charge.837":
        edi = edi.replace("CLM*CLAB-DEMO-001*30*", "CLM*CLAB-DEMO-001*31*")
    return edi.encode("ascii")


class Handler(BaseHTTPRequestHandler):
    timeout = 10

    def log_message(self, format, *args):
        # Do not log request URLs, headers, or upload contents.
        pass

    def reply(self, status, content_type, body, *, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                         "base-uri 'none'; frame-ancestors 'none'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def json_reply(self, status, result):
        self.reply(status, "application/json", json.dumps(result).encode("utf-8"))

    def error(self, status, code, message):
        self.json_reply(status, {"error": issue(code, message)})

    def do_GET(self):
        if self.path in ASSETS:
            name, content_type = ASSETS[self.path]
            body = files(__package__).joinpath("sandbox_ui").joinpath(name).read_bytes()
            self.reply(HTTPStatus.OK, content_type, body)
        elif self.path.startswith("/api/v1/examples/"):
            name = self.path.removeprefix("/api/v1/examples/")
            if name in EXAMPLES:
                self.reply(HTTPStatus.OK, "application/edi-x12", example(name), filename=name)
            else:
                self.error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "This example does not exist.")
        else:
            self.error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "This endpoint does not exist.")

    def do_POST(self):
        if self.path != "/api/v1/validate":
            return self.error(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Use POST /api/v1/validate.")
        if self.headers.get_content_type() != "application/edi-x12":
            return self.error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "EDI_REQUIRED",
                              "Send raw file bytes with Content-Type: application/edi-x12.")
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
            return self.error(HTTPStatus.BAD_REQUEST, "CONTENT_LENGTH_REQUIRED", "Send a single Content-Length.")
        length = int(lengths[0])
        if length > MAX_BYTES:
            return self.error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "FILE_TOO_LARGE", "The maximum file size is 1 MiB.")
        if not length:
            return self.error(HTTPStatus.BAD_REQUEST, "EMPTY_UPLOAD", "Supply an 837P file.")
        try:
            body = self.rfile.read(length)
        except TimeoutError:
            return self.error(HTTPStatus.REQUEST_TIMEOUT, "UPLOAD_TIMEOUT", "The upload did not finish in time.")
        if len(body) != length:
            return self.error(HTTPStatus.BAD_REQUEST, "INCOMPLETE_UPLOAD", "The upload did not finish.")
        self.json_reply(HTTPStatus.OK, simulate(body))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        print(f"ClaimLab Sandbox  http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
