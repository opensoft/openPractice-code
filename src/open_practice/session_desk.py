"""Run the local openPractice Phase 1 session desk for synthetic bookings."""

import argparse
import json
import re
import sqlite3
from contextlib import closing
from datetime import date, datetime, time, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


STAGES = ("scheduled", "available", "ready", "started", "ended", "wrapped")
DEMO_PATIENTS = ("demo-01", "demo-02", "demo-03", "demo-04")
ASSETS = Path(__file__).with_name("session_desk_web")
STATIC_ROUTES = {
    "/": ("index.html", "text/html"),
    "/desk.css": ("desk.css", "text/css"),
    "/desk.js": ("desk.js", "text/javascript"),
}


class TransitionConflict(ValueError):
    """The session has already moved beyond this action."""


def _event(stage):
    return {"stage": stage, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def _appointment(row):
    result = dict(row)
    result["events"] = json.loads(result["events"])
    result["stage"] = result["events"][-1]["stage"]
    result["ready_for_billing"] = result["stage"] == "wrapped"
    return result


class SessionDesk:
    def __init__(self, database):
        self.database = database
        with closing(self.connect()) as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS appointments (
                    id INTEGER PRIMARY KEY,
                    patient TEXT NOT NULL,
                    location TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    day TEXT NOT NULL,
                    time TEXT NOT NULL,
                    events TEXT NOT NULL,
                    UNIQUE (patient, location, mode, day, time)
                )
            """)

    def connect(self):
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        return connection

    def appointments(self):
        with closing(self.connect()) as connection:
            return [_appointment(row) for row in connection.execute(
                "SELECT * FROM appointments ORDER BY day, time, id"
            )]

    def book(self, values):
        patient, location, mode, day, slot = (
            values.get(key) for key in ("patient", "location", "mode", "day", "time")
        )
        if patient not in DEMO_PATIENTS:
            raise ValueError("Choose a demo patient.")
        if location not in ("clinic", "alf") or mode not in ("in-person", "telemedicine"):
            raise ValueError("Choose a location and visit mode.")
        try:
            if date.fromisoformat(day).isoformat() != day or time.fromisoformat(slot).isoformat(timespec="minutes") != slot:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("Enter a valid appointment date and time.") from None

        booking = (patient, location, mode, day, slot)
        with closing(self.connect()) as connection, connection:
            connection.execute("""
                INSERT INTO appointments (patient, location, mode, day, time, events)
                VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (patient, location, mode, day, time) DO NOTHING
            """, (*booking, json.dumps([_event("scheduled")])))
            row = connection.execute("""
                SELECT * FROM appointments WHERE patient = ? AND location = ? AND mode = ? AND day = ? AND time = ?
            """, booking).fetchone()
            return _appointment(row)

    def advance(self, appointment_id, expected_stage, *, chart_complete=False):
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM appointments WHERE id = ?", (appointment_id,)).fetchone()
            if row is None:
                raise KeyError("Appointment not found.")
            appointment = _appointment(row)
            stage = appointment["stage"]
            if expected_stage != stage or stage == "wrapped":
                raise TransitionConflict("This session has moved on. Use the current action.")
            if stage == "ended" and chart_complete is not True:
                raise ValueError("Confirm the clinician finished charting before wrap-up.")

            next_stage = STAGES[STAGES.index(stage) + 1]
            events = [*appointment["events"], _event(next_stage)]
            connection.execute("UPDATE appointments SET events = ? WHERE id = ?", (json.dumps(events), appointment_id))
            return _appointment({**dict(row), "events": json.dumps(events)})


def make_server(desk, port=17824):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, data, content_type="application/json"):
            if content_type == "application/json":
                data = json.dumps(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in STATIC_ROUTES:
                filename, content_type = STATIC_ROUTES[self.path]
                self.respond(HTTPStatus.OK, (ASSETS / filename).read_bytes(), content_type)
            elif self.path == "/api/appointments":
                self.respond(HTTPStatus.OK, desk.appointments())
            else:
                self.respond(HTTPStatus.NOT_FOUND, {"error": "Page not found."})

        def do_POST(self):
            advance = re.fullmatch(r"/api/appointments/([1-9][0-9]{0,17})/advance", self.path)
            if self.path != "/api/appointments" and advance is None:
                self.respond(HTTPStatus.NOT_FOUND, {"error": "Page not found."})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                if self.headers.get_content_type() != "application/json" or not 0 < length <= 4096:
                    raise ValueError("Send a JSON request under 4 KB.")
                values = json.loads(self.rfile.read(length))
                if not isinstance(values, dict):
                    raise ValueError("Send a JSON object.")
                if advance:
                    result = desk.advance(int(advance[1]), values.get("stage"), chart_complete=values.get("chart_complete"))
                else:
                    result = desk.book(values)
            except TransitionConflict as error:
                self.respond(HTTPStatus.CONFLICT, {"error": str(error)})
            except KeyError as error:
                self.respond(HTTPStatus.NOT_FOUND, {"error": error.args[0]})
            except ValueError as error:
                self.respond(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            else:
                self.respond(HTTPStatus.OK, result)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--port", type=int, default=17824)
    args = parser.parse_args()
    args.database.parent.mkdir(parents=True, exist_ok=True)
    with make_server(SessionDesk(args.database), args.port) as server:
        print(f"Session desk running at http://127.0.0.1:{server.server_port}/", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
