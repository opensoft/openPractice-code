import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from .sandbox import Handler, MAX_BYTES, example, simulate


class SandboxTest(unittest.TestCase):
    def test_examples_cover_file_and_claim_acknowledgments(self):
        cases = [
            ("accepted.837", "accepted", "accepted"),
            ("claim-rejected.837", "accepted", "rejected"),
            ("file-rejected.837", "rejected", "not_generated"),
        ]
        for name, file_status, claim_status in cases:
            with self.subTest(name=name):
                result = simulate(example(name))
                self.assertTrue(result["simulated"])
                self.assertEqual(result["file_ack"]["status"], file_status)
                self.assertEqual(result["claim_ack"]["status"], claim_status)
                self.assertEqual(result["accepted"], claim_status == "accepted")

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
        self.assertEqual(result["file_ack"]["issues"][0]["code"], "ISA_CONTROL")
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
                    self.assertIn(b'id="edi-file"', response.read())

                    with_connection.request("GET", "/api/v1/examples/accepted.837")
                    response = with_connection.getresponse()
                    self.assertEqual(response.status, 200)
                    edi = response.read()

                    with_connection.request("POST", "/api/v1/validate", edi, {"Content-Type": "application/edi-x12"})
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
