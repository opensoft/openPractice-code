"""QA-only proxy that drops one successful Company create response."""

import json
import os
import socket
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FaultProxy(BaseHTTPRequestHandler):
    def do_GET(self):
        self.forward()

    def do_POST(self):
        self.forward()

    def forward(self):
        data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        upstream = HTTPConnection("127.0.0.1", int(os.environ.get("PRACTICELAB_UPSTREAM_PORT", "8010")), timeout=30)
        try:
            headers = {
                "Authorization": self.headers["Authorization"],
                "Content-Type": "application/json",
            }
            if self.headers.get("X-Frappe-Site-Name"):
                headers["X-Frappe-Site-Name"] = self.headers["X-Frappe-Site-Name"]
            upstream.request(self.command, self.path, body=data, headers=headers)
            response = upstream.getresponse()
            body = response.read()
            drop = (
                200 <= response.status < 300
                and self.command == "POST"
                and self.path == "/api/resource/Company"
                and json.loads(data)["company_name"] == "Synthetic Lost Response LLC"
            )
            if drop:
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        finally:
            upstream.close()


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8011), FaultProxy).serve_forever()
