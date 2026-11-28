import json
import re
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from .sandbox import EXAMPLES, Handler, MAX_BYTES, example, simulate
from .x837p import parse


class SandboxTest(unittest.TestCase):
    def test_examples_cover_file_and_claim_acknowledgments(self):
        cases = [
            ("accepted.837", "not_generated", "accepted", "accepted"),
            ("accepted-25.837", "not_generated", "accepted", "accepted"),
            ("claim-rejected.837", "not_generated", "accepted", "rejected"),
            ("file-rejected.837", "rejected", "not_generated", "not_generated"),
            ("missing-clm02.837", "not_generated", "rejected", "not_generated"),
            ("invalid-total-charge.837", "not_generated", "accepted", "rejected"),
        ]
        for name, interchange_status, file_status, claim_status in cases:
            with self.subTest(name=name):
                result = simulate(example(name))
                self.assertTrue(result["simulated"])
                self.assertEqual(result["interchange_ack"]["status"], interchange_status)
                self.assertEqual(result["file_ack"]["status"], file_status)
                self.assertEqual(result["claim_ack"]["status"], claim_status)
                self.assertEqual(result["accepted"], claim_status == "accepted")

    def test_batch_has_25_distinct_claims_and_subscribers(self):
        edi = example("accepted-25.837")
        segments = parse(edi.decode("ascii"))
        claims = [segment[1] for segment in segments if segment[0] == "CLM"]
        subscribers = [segment[9] for segment in segments if segment[:2] == ["NM1", "IL"]]
        self.assertEqual(len(claims), 25)
        self.assertEqual(len(set(claims)), 25)
        self.assertEqual(len(subscribers), 25)
        self.assertEqual(len(set(subscribers)), 25)
        results = simulate(edi)["claim_ack"]["claims"]
        self.assertEqual(len(results), 25)
        self.assertTrue(all(claim["status"] == "accepted" for claim in results))

    def test_mismatched_interchange_controls_return_ta1_code_001(self):
        result = simulate(example("file-rejected.837"))
        self.assertEqual(result["interchange_ack"]["issues"][0]["ta105"], "001")
        self.assertEqual(result["file_ack"]["issues"], [])
        self.assertEqual(result["claim_ack"]["claims"], [])

    def test_required_and_unbalanced_charges_belong_to_different_stages(self):
        missing = simulate(example("missing-clm02.837"))
        self.assertEqual(missing["file_ack"]["issues"][0]["code"], "CLM02_REQUIRED")
        self.assertEqual(missing["claim_ack"]["claims"], [])
        invalid = simulate(example("invalid-total-charge.837"))
        issues = invalid["claim_ack"]["claims"][0]["issues"]
        self.assertEqual([issue["code"] for issue in issues], ["CLAIM_TOTAL_OUT_OF_BALANCE"])

    def test_charge_balancing_sums_multiple_decimal_service_lines(self):
        edi = example("accepted.837")
        edi = edi.replace(b"CLM*CLAB-DEMO-001*30*", b"CLM*CLAB-DEMO-001*0.3*")
        edi = edi.replace(b"SV1*HC:G0008*30*", b"SV1*HC:G0008*0.1*")
        edi = re.sub(
            rb"SE\*(\d+)\*",
            lambda match: b"LX*2~\nSV1*HC:G0008*0.2*UN*1***1~\nDTP*472*D8*20261120~\n"
            + f"SE*{int(match[1]) + 3}*".encode("ascii"),
            edi,
        )
        self.assertTrue(simulate(edi)["accepted"])

    def test_production_file_stops_at_file_validation(self):
        original = example("accepted.837")
        result = simulate(original[:102] + b"P" + original[103:])
        self.assertEqual(result["file_ack"]["issues"][0]["code"], "TEST_FLAG_REQUIRED")
        self.assertEqual(result["claim_ack"]["status"], "not_generated")

    def test_claim_rejection_does_not_echo_uploaded_subscriber(self):
        payload = example("accepted.837").replace(b"SYNTH00001", b"DO-NOT-ECHO")
        result = simulate(payload)
        self.assertEqual(result["file_ack"]["status"], "accepted")
        self.assertEqual(result["claim_ack"]["claims"][0]["issues"][0]["code"], "TEST_MEMBER_UNKNOWN")
        self.assertNotIn("DO-NOT-ECHO", json.dumps(result))

    def test_rejects_invalid_interchange_control_number(self):
        payload = example("accepted.837").replace(b"000000001", b"A00000001")
        result = simulate(payload)
        self.assertEqual(result["interchange_ack"]["issues"][0]["code"], "ISA_CONTROL")
        self.assertEqual(result["file_ack"]["status"], "not_generated")
        self.assertEqual(result["claim_ack"]["status"], "not_generated")

    def test_browser_assets_and_api_use_the_same_contract(self):
        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                with_connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                try:
                    with_connection.request("GET", "/")
                    response = with_connection.getresponse()
                    self.assertEqual(response.status, 200)
                    homepage = response.read()
                    self.assertIn(b'id="edi-file"', homepage)
                    self.assertIn(b'href="/agents"', homepage)
                    self.assertIn(b'href="/openapi.json"', homepage)

                    with_connection.request("GET", "/agents")
                    response = with_connection.getresponse()
                    self.assertEqual(response.status, 200)
                    guide = response.read()
                    self.assertIn(b"https://claimlab.opensoft.dev/api/v1/validate", guide)
                    self.assertNotIn(b"<script", guide)

                    with_connection.request("GET", "/llms.txt")
                    response = with_connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertTrue(response.getheader("Content-Type").startswith("text/plain"))
                    self.assertIn(b"https://claimlab.opensoft.dev/openapi.json", response.read())

                    with_connection.request("GET", "/openapi.json")
                    response = with_connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.getheader("Content-Type"), "application/json")
                    spec = json.loads(response.read())
                    post = spec["paths"]["/api/v1/validate"]["post"]
                    request_type = post["requestBody"]["content"]["application/edi-x12"]
                    self.assertEqual(request_type["schema"]["maxLength"], MAX_BYTES)
                    example_names = spec["paths"]["/api/v1/examples/{name}"]["get"]["parameters"][0]
                    self.assertEqual(set(example_names["schema"]["enum"]), EXAMPLES)

                    for name in ("accepted-25.837", "file-rejected.837",
                                 "missing-clm02.837", "invalid-total-charge.837"):
                        with self.subTest(name=name):
                            with_connection.request("GET", f"/api/v1/examples/{name}")
                            response = with_connection.getresponse()
                            self.assertEqual(response.status, 200)
                            edi = response.read()
                            self.assertEqual(edi, example(name))

                            with_connection.request("POST", "/api/v1/validate", edi, {
                                "Content-Type": "application/edi-x12",
                            })
                            response = with_connection.getresponse()
                            self.assertEqual(response.status, 200)
                            self.assertEqual(json.loads(response.read()), simulate(edi))

                    with_connection.request("POST", "/api/v1/validate", edi, {"Content-Type": "text/plain"})
                    response = with_connection.getresponse()
                    self.assertEqual(response.status, 415)
                    response.read()

                    with_connection.request("POST", "/api/v1/validate", headers={
                        "Content-Type": "application/edi-x12", "Content-Length": str(MAX_BYTES + 1),
                    })
                    response = with_connection.getresponse()
                    self.assertEqual(response.status, 413)
                    response.read()
                finally:
                    with_connection.close()
            finally:
                server.shutdown()
                thread.join()


if __name__ == "__main__":
    unittest.main()
