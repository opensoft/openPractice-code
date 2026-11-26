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
from uuid import uuid4


STAGES = ("scheduled", "available", "ready", "started", "ended", "wrapped")
PATIENT_FIELDS = ("first_name", "last_name", "date_of_birth", "chart_reference")
CONTRACT_STATUSES = ("unknown", "no_contract", "contracted")
APPOINTMENT_SELECT = """
    SELECT a.*, p.first_name, p.last_name, p.date_of_birth, p.chart_reference,
        f.name AS facility_name, f.contract_status
    FROM appointments a JOIN patients p ON a.patient = p.reference
    JOIN facilities f ON a.location = f.reference
"""
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
    result["patient_details"] = {key: result.pop(key) for key in PATIENT_FIELDS}
    result["events"] = json.loads(result["events"])
    result["stage"] = result["events"][-1]["stage"]
    result["ready_for_billing"] = result["stage"] == "wrapped"
    return result


def _patient_values(details):
    if not isinstance(details, dict):
        raise ValueError("Enter the new patient's details.")
    cleaned = []
    for field in PATIENT_FIELDS:
        value = details.get(field, "")
        if not isinstance(value, str):
            raise ValueError("Patient details must be text.")
        value = value.strip()
        if value and (len(value) > 80 or not value.isprintable()):
            raise ValueError("Patient fields must be no more than 80 printable characters.")
        cleaned.append(value or None)
    first_name, last_name, birth_date, chart_reference = cleaned
    if not (first_name and last_name):
        raise ValueError("Enter a first and last name.")
    if birth_date:
        try:
            parsed = date.fromisoformat(birth_date)
            if parsed.isoformat() != birth_date or parsed > date.today():
                raise ValueError
        except ValueError:
            raise ValueError("Enter a valid date of birth on or before today.") from None
    return first_name, last_name, birth_date, chart_reference


class SessionDesk:
    def __init__(self, database):
        self.database = database
        with closing(self.connect()) as connection, connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS patients (
                    reference TEXT PRIMARY KEY,
                    first_name TEXT NOT NULL,
                    last_name TEXT NOT NULL,
                    date_of_birth TEXT,
                    chart_reference TEXT UNIQUE
                )
            """)
            connection.executemany("""
                INSERT INTO patients (reference, first_name, last_name) VALUES (?, 'Demo', ?)
                ON CONFLICT (reference) DO NOTHING
            """, [(f"demo-{number:02}", f"patient {number:02}") for number in range(1, 5)])
            connection.execute("""
                CREATE TABLE IF NOT EXISTS facilities (
                    reference TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    kind TEXT NOT NULL,
                    contract_status TEXT
                )
            """)
            connection.execute("""
                INSERT INTO facilities (reference, name, kind, contract_status)
                VALUES ('clinic', 'Clinic', 'clinic', NULL), ('alf', 'Assisted living facility', 'alf', NULL)
                ON CONFLICT (reference) DO NOTHING
            """)
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
                APPOINTMENT_SELECT + " ORDER BY a.day, a.time, a.id"
            )]

    def patients(self):
        with closing(self.connect()) as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM patients ORDER BY last_name COLLATE NOCASE, first_name COLLATE NOCASE, reference"
            )]

    def facilities(self):
        with closing(self.connect()) as connection:
            return [dict(row) for row in connection.execute("""
                SELECT * FROM facilities WHERE reference != 'alf'
                ORDER BY kind = 'clinic' DESC, name COLLATE NOCASE
            """)]

    def save_facility(self, values):
        reference = values.get("reference", "")
        name = values.get("name", "")
        contract_status = values.get("contract_status", "unknown")
        if not isinstance(reference, str) or (reference and not re.fullmatch(r"alf-[0-9a-f]{32}", reference)):
            raise ValueError("Choose a registered assisted living facility.")
        name = name.strip() if isinstance(name, str) else ""
        if len(name) > 120 or not name.isprintable():
            raise ValueError("Enter a facility name of up to 120 printable characters.")
        if contract_status not in CONTRACT_STATUSES:
            raise ValueError("Choose a contract status.")
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if reference and connection.execute(
                "SELECT 1 FROM facilities WHERE reference = ? AND kind = 'alf'", (reference,)
            ).fetchone() is None:
                raise ValueError("Choose a registered assisted living facility.")
            existing = connection.execute("SELECT * FROM facilities WHERE name = ?", (name,)).fetchone()
            if existing is not None and existing["reference"] != reference:
                if not reference and existing["reference"].startswith("alf-") and existing["contract_status"] == contract_status:
                    return dict(existing)
                raise ValueError("That name is already in use. Select the existing facility to make changes.")
            reference = reference or f"alf-{uuid4().hex}"
            connection.execute("""
                INSERT INTO facilities (reference, name, kind, contract_status) VALUES (?, ?, 'alf', ?)
                ON CONFLICT (reference) DO UPDATE SET name = excluded.name, contract_status = excluded.contract_status
            """, (reference, name, contract_status))
            return dict(connection.execute("SELECT * FROM facilities WHERE reference = ?", (reference,)).fetchone())

    def book(self, values):
        patient, location, mode, day, slot = (
            values.get(key) for key in ("patient", "location", "mode", "day", "time")
        )
        if not isinstance(patient, str) or not patient:
            raise ValueError("Choose a saved patient or enter a new one.")
        if not isinstance(location, str) or mode not in ("in-person", "telemedicine"):
            raise ValueError("Choose a location and visit mode.")
        try:
            if date.fromisoformat(day).isoformat() != day or time.fromisoformat(slot).isoformat(timespec="minutes") != slot:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("Enter a valid appointment date and time.") from None

        booking = (patient, location, mode, day, slot)
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM facilities WHERE reference = ?", (location,)).fetchone() is None:
                raise ValueError("Choose a registered location.")
            saved = connection.execute("SELECT * FROM patients WHERE reference = ?", (patient,)).fetchone()
            if "new_patient" in values:
                if not re.fullmatch(r"patient-[0-9a-f]{32}", patient):
                    raise ValueError("Open a new appointment to enter patient details.")
                details = _patient_values(values["new_patient"])
                if saved is None:
                    try:
                        connection.execute("""
                            INSERT INTO patients (reference, first_name, last_name, date_of_birth, chart_reference)
                            VALUES (?, ?, ?, ?, ?)
                        """, (patient, *details))
                    except sqlite3.IntegrityError:
                        raise ValueError("That chart reference belongs to a saved patient. Select the saved patient.") from None
                elif tuple(saved[key] for key in PATIENT_FIELDS) != details:
                    raise ValueError("Patient details changed. Select the saved patient or open a new appointment.")
            elif saved is None:
                raise ValueError("Choose a saved patient or enter a new one.")
            connection.execute("""
                INSERT INTO appointments (patient, location, mode, day, time, events)
                VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (patient, location, mode, day, time) DO NOTHING
            """, (*booking, json.dumps([_event("scheduled")])))
            row = connection.execute(
                APPOINTMENT_SELECT + " WHERE a.patient = ? AND a.location = ? AND a.mode = ? AND a.day = ? AND a.time = ?",
                booking,
            ).fetchone()
            return _appointment(row)

    def advance(self, appointment_id, expected_stage, *, chart_complete=False):
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(APPOINTMENT_SELECT + " WHERE a.id = ?", (appointment_id,)).fetchone()
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
            elif self.path == "/api/patients":
                self.respond(HTTPStatus.OK, desk.patients())
            elif self.path == "/api/facilities":
                self.respond(HTTPStatus.OK, desk.facilities())
            else:
                self.respond(HTTPStatus.NOT_FOUND, {"error": "Page not found."})

        def do_POST(self):
            advance = re.fullmatch(r"/api/appointments/([1-9][0-9]{0,17})/advance", self.path)
            if self.path not in ("/api/appointments", "/api/facilities") and advance is None:
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
                elif self.path == "/api/facilities":
                    result = desk.save_facility(values)
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
