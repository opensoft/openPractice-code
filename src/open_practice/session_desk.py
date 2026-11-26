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

from open_practice.npi_registry import NPI_PATTERN, RegistryUnavailable, lookup_facility


STAGES = ("scheduled", "available", "ready", "started", "ended", "wrapped")
PATIENT_FIELDS = ("first_name", "last_name", "date_of_birth", "chart_reference")
PATIENT_REFERENCE = r"(demo-[0-9]{2}|patient-[0-9a-f]{32})"
INSURANCE_FIELDS = (
    ("payer_name", "insurance payer", 120),
    ("payer_id", "payer ID", 40),
    ("plan_name", "plan name", 120),
    ("member_id", "member ID", 80),
    ("group_number", "insurance group number", 80),
)
CONTRACT_STATUSES = ("unknown", "no_contract", "contracted")
# CMS Place of Service Code Set for Professional Claims.
FACILITY_TYPES = {
    "office": ("Office / clinic", "11"),
    "alf": ("Assisted living facility (ALF)", "13"),
    "snf": ("Skilled nursing facility (SNF)", "31"),
    "hospital": ("Hospital", None),
    "pharmacy": ("Pharmacy", "01"),
    "retail_clinic": ("Walk-in Retail Health Clinic", "17"),
    "urgent_care": ("Urgent Care Facility", "20"),
    "asc": ("Ambulatory Surgical Center", "24"),
    "fqhc": ("Federally Qualified Health Center", "50"),
    "rhc": ("Rural Health Clinic", "72"),
    "other": ("Other facility", "99"),
}
HOSPITAL_SETTINGS = {
    "19": "Off-campus outpatient hospital",
    "21": "Inpatient hospital",
    "22": "On-campus outpatient hospital",
    "23": "Hospital emergency room",
}
HOME_POS_CODE = "12"
FACILITY_NAME_LIMIT = 255
LIVING_ARRANGEMENTS = ("alone", "with_caregiver")
ADDRESS_FIELDS = (
    ("street_address", "street address", 120),
    ("address_line_2", "address line 2", 80),
    ("city", "city", 80),
    ("region", "state or province", 60),
    ("postal_code", "postal code", 15),
)
SITE_FIELDS = ("legal_name", "npi", *(key for key, _, _ in ADDRESS_FIELDS))
SITE_SELECT = """
    SELECT f.*, d.legal_name, d.npi, d.street_address, d.address_line_2, d.city, d.region, d.postal_code
    FROM facilities f LEFT JOIN site_details d USING (reference)
"""
APPOINTMENT_SELECT = """
    SELECT a.*, p.first_name, p.last_name, p.date_of_birth, p.chart_reference,
        CASE WHEN a.group_reference IS NULL THEN 1 ELSE (
            SELECT COUNT(*) FROM appointment_members m WHERE m.appointment_id = a.id
        ) END AS member_count,
        COALESCE(f.name, 'Patient home') AS facility_name,
        COALESCE(f.kind, 'home') AS location_kind, f.facility_type, f.hospital_pos_code,
        CASE WHEN f.kind = 'alf' THEN f.contract_status END AS contract_status, h.living_arrangement,
        COALESCE(d.street_address, h.street_address) AS street_address,
        COALESCE(d.address_line_2, h.address_line_2) AS address_line_2,
        COALESCE(d.city, h.city) AS city, COALESCE(d.region, h.region) AS region,
        COALESCE(d.postal_code, h.postal_code) AS postal_code
    FROM appointments a LEFT JOIN patients p ON a.patient = p.reference
    LEFT JOIN facilities f ON a.location = f.reference
    LEFT JOIN site_details d ON f.reference = d.reference
    LEFT JOIN patient_homes h ON a.location = h.reference
"""
APPOINTMENT_TABLE = """
    CREATE TABLE IF NOT EXISTS appointments (
        id INTEGER PRIMARY KEY,
        patient TEXT REFERENCES patients(reference),
        location TEXT NOT NULL,
        mode TEXT NOT NULL,
        day TEXT NOT NULL,
        time TEXT NOT NULL,
        events TEXT NOT NULL,
        group_reference TEXT UNIQUE,
        group_name TEXT,
        CHECK (
            (patient IS NOT NULL AND group_reference IS NULL AND group_name IS NULL)
            OR (patient IS NULL AND group_reference IS NOT NULL AND group_name IS NOT NULL)
        ),
        UNIQUE (patient, location, mode, day, time)
    )
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


def _facility_pos(facility_type, hospital_pos_code):
    if facility_type == "hospital":
        return hospital_pos_code
    return FACILITY_TYPES.get(facility_type, (None, None))[1]


def _facility(row):
    result = dict(row)
    result["pos_code"] = _facility_pos(result["facility_type"], result.pop("hospital_pos_code"))
    return result


def _appointment(row):
    result = dict(row)
    hospital_pos_code = result.pop("hospital_pos_code")
    result["pos_code"] = (
        HOME_POS_CODE if result["location_kind"] == "home"
        else _facility_pos(result["facility_type"], hospital_pos_code)
    )
    patient = {key: result.pop(key) for key in PATIENT_FIELDS}
    result["patient_details"] = None if result["group_reference"] else patient
    result["location_address"] = {key: result.pop(key) for key, _, _ in ADDRESS_FIELDS}
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


def _location_text(details, key, label, limit, *, optional=False):
    raw = details.get(key, "")
    value = raw.strip() if isinstance(raw, str) else ""
    if not isinstance(raw, str) or len(value) > limit or (value and not value.isprintable()):
        raise ValueError(f"Enter a valid {label} of up to {limit} characters.")
    if not value and not optional:
        raise ValueError(f"Enter the {label}.")
    return value


def _address_values(details):
    return {
        key: _location_text(details, key, label, limit, optional=key == "address_line_2")
        for key, label, limit in ADDRESS_FIELDS
    }


def _insurance_values(details):
    if not isinstance(details, dict):
        raise ValueError("Enter the primary insurance details.")
    coverage = {
        key: _location_text(details, key, label, limit, optional=True)
        for key, label, limit in INSURANCE_FIELDS
    }
    if not any(coverage.values()):
        return None
    if not coverage["payer_name"] or not coverage["member_id"]:
        raise ValueError("Enter the insurance payer and member ID.")
    return coverage


def _site_values(details):
    legal_name = _location_text(details, "legal_name", "legal operating name", FACILITY_NAME_LIMIT)
    npi = details.get("npi")
    if not isinstance(npi, str) or not re.fullmatch(NPI_PATTERN, npi.strip()):
        raise ValueError("Enter a 10-digit NPI for the location.")
    return {"legal_name": legal_name, "npi": npi.strip(), **_address_values(details)}


def _location_reference(value, kind):
    return isinstance(value, str) and re.fullmatch(rf"{kind}-[0-9a-f]{{32}}", value)


class SessionDesk:
    def __init__(self, database):
        self.database = database
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
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
                    contract_status TEXT,
                    facility_type TEXT,
                    hospital_pos_code TEXT,
                    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1))
                )
            """)
            facility_columns = {field["name"] for field in connection.execute("PRAGMA table_info(facilities)")}
            if "facility_type" not in facility_columns:
                connection.execute("ALTER TABLE facilities ADD COLUMN facility_type TEXT")
                connection.execute("""
                    UPDATE facilities SET facility_type = CASE WHEN kind = 'clinic' THEN 'office' ELSE 'alf' END
                    WHERE kind = 'clinic' OR reference = 'alf'
                """)
            if "archived" not in facility_columns:
                connection.execute("""
                    ALTER TABLE facilities ADD COLUMN archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1))
                """)
            if "hospital_pos_code" not in facility_columns:
                connection.execute("ALTER TABLE facilities ADD COLUMN hospital_pos_code TEXT")
            connection.execute("""
                INSERT INTO facilities (reference, name, kind, facility_type)
                VALUES ('clinic', 'Clinic', 'clinic', 'office'), ('alf', 'Assisted living facility', 'alf', 'alf')
                ON CONFLICT (reference) DO NOTHING
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS site_details (
                    reference TEXT PRIMARY KEY REFERENCES facilities(reference),
                    legal_name TEXT NOT NULL, npi TEXT NOT NULL,
                    street_address TEXT NOT NULL, address_line_2 TEXT NOT NULL,
                    city TEXT NOT NULL, region TEXT NOT NULL, postal_code TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS patient_homes (
                    reference TEXT PRIMARY KEY, patient TEXT NOT NULL REFERENCES patients(reference),
                    living_arrangement TEXT NOT NULL,
                    street_address TEXT NOT NULL, address_line_2 TEXT NOT NULL,
                    city TEXT NOT NULL, region TEXT NOT NULL, postal_code TEXT NOT NULL
                )
            """)
            connection.execute("CREATE INDEX IF NOT EXISTS patient_homes_patient ON patient_homes(patient)")
            connection.execute(APPOINTMENT_TABLE)
            if "group_reference" not in {field["name"] for field in connection.execute("PRAGMA table_info(appointments)")}:
                connection.execute("ALTER TABLE appointments RENAME TO appointments_previous")
                connection.execute(APPOINTMENT_TABLE)
                connection.execute("""
                    INSERT INTO appointments (id, patient, location, mode, day, time, events)
                    SELECT id, patient, location, mode, day, time, events FROM appointments_previous
                """)
                connection.execute("DROP TABLE appointments_previous")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS appointment_members (
                    appointment_id INTEGER NOT NULL REFERENCES appointments(id),
                    patient TEXT NOT NULL REFERENCES patients(reference),
                    position INTEGER NOT NULL,
                    PRIMARY KEY (appointment_id, position),
                    UNIQUE (appointment_id, patient)
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS patient_insurance (
                    patient TEXT PRIMARY KEY REFERENCES patients(reference),
                    payer_name TEXT NOT NULL, payer_id TEXT NOT NULL, plan_name TEXT NOT NULL,
                    member_id TEXT NOT NULL, group_number TEXT NOT NULL
                )
            """)

    def connect(self):
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
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

    def appointment_patients(self, appointment_id):
        with closing(self.connect()) as connection:
            appointment = connection.execute(
                "SELECT patient FROM appointments WHERE id = ?", (appointment_id,),
            ).fetchone()
            if appointment is None:
                raise KeyError("Appointment not found.")
            rows = connection.execute("""
                WITH roster AS (
                    SELECT patient, position FROM appointment_members WHERE appointment_id = ?
                    UNION ALL SELECT ?, 0 WHERE ? IS NOT NULL
                )
                SELECT p.*, i.payer_name, i.payer_id, i.plan_name, i.member_id, i.group_number
                FROM roster JOIN patients p ON p.reference = roster.patient
                LEFT JOIN patient_insurance i ON i.patient = p.reference
                ORDER BY roster.position
            """, (appointment_id, appointment["patient"], appointment["patient"]))
            result = []
            for row in rows:
                patient = dict(row)
                coverage = {key: patient.pop(key) for key, _, _ in INSURANCE_FIELDS}
                result.append({**patient, "insurance": coverage if coverage["payer_name"] else None})
            return result

    def save_insurance(self, patient, values):
        coverage = _insurance_values(values)
        if coverage is None:
            raise ValueError("Enter the insurance payer and member ID.")
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM patients WHERE reference = ?", (patient,)).fetchone() is None:
                raise KeyError("Patient not found.")
            self._save_insurance(connection, patient, coverage)
        return coverage

    def _save_insurance(self, connection, patient, coverage):
        connection.execute("""
            INSERT INTO patient_insurance (patient, payer_name, payer_id, plan_name, member_id, group_number)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (patient) DO UPDATE SET payer_name = excluded.payer_name, payer_id = excluded.payer_id,
                plan_name = excluded.plan_name, member_id = excluded.member_id, group_number = excluded.group_number
        """, (patient, *coverage.values()))

    def _ensure_patient(self, connection, values):
        patient = values.get("patient")
        if not isinstance(patient, str) or not patient:
            raise ValueError("Choose a saved patient or enter a new one.")
        saved = connection.execute("SELECT * FROM patients WHERE reference = ?", (patient,)).fetchone()
        if "new_patient" not in values:
            if saved is None:
                raise ValueError("Choose a saved patient or enter a new one.")
            return patient
        if not re.fullmatch(r"patient-[0-9a-f]{32}", patient):
            raise ValueError("Open a new appointment to enter patient details.")
        details = _patient_values(values["new_patient"])
        coverage = _insurance_values(values["insurance"]) if "insurance" in values else None
        if saved is not None:
            if tuple(saved[key] for key in PATIENT_FIELDS) != details:
                raise ValueError("Patient details changed. Select the saved patient or open a new appointment.")
            return patient
        try:
            connection.execute("""
                INSERT INTO patients (reference, first_name, last_name, date_of_birth, chart_reference)
                VALUES (?, ?, ?, ?, ?)
            """, (patient, *details))
        except sqlite3.IntegrityError:
            raise ValueError("That chart reference belongs to a saved patient. Select the saved patient.") from None
        if coverage:
            self._save_insurance(connection, patient, coverage)
        return patient

    def facilities(self):
        with closing(self.connect()) as connection:
            return [_facility(row) for row in connection.execute(SITE_SELECT + """
                WHERE f.reference != 'alf'
                ORDER BY f.kind = 'clinic' DESC, f.name COLLATE NOCASE
            """)]

    def patient_homes(self, patient):
        with closing(self.connect()) as connection:
            return [dict(row) for row in connection.execute("""
                SELECT reference, street_address, address_line_2, city, region, postal_code
                FROM patient_homes WHERE patient = ? ORDER BY street_address COLLATE NOCASE, reference
            """, (patient,))]

    def save_facility(self, values):
        reference = values.get("reference", "")
        if not isinstance(reference, str) or reference == "alf":
            raise ValueError("Choose a registered facility.")
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(SITE_SELECT + " WHERE f.reference = ?", (reference,)).fetchone()
            if reference and existing is None:
                raise ValueError("Choose a registered facility.")
            facility_type = values.get("facility_type", existing["facility_type"] if existing else None)
            keep_unclassified = existing is not None and "facility_type" not in values and facility_type is None
            if not keep_unclassified and (not isinstance(facility_type, str) or facility_type not in FACILITY_TYPES):
                raise ValueError("Choose a facility type.")
            hospital_pos_code = None
            if facility_type == "hospital":
                if "pos_code" not in values and existing and existing["facility_type"] == "hospital":
                    hospital_pos_code = existing["hospital_pos_code"]
                else:
                    hospital_pos_code = values.get("pos_code")
                    if not isinstance(hospital_pos_code, str) or hospital_pos_code not in HOSPITAL_SETTINGS:
                        raise ValueError("Choose a hospital setting for the place of service.")
            kind = "clinic" if facility_type == "office" else "alf"
            name = _location_text(values, "name", "location name", FACILITY_NAME_LIMIT)
            old_status = existing["contract_status"] if existing else None
            status = values.get("contract_status", old_status or "unknown") if kind == "alf" else old_status
            if kind == "alf" and status not in CONTRACT_STATUSES:
                raise ValueError("Choose a contract status.")
            details = _site_values(values) if not reference or any(key in values for key in SITE_FIELDS) else {}
            same_name = connection.execute(SITE_SELECT + " WHERE f.name = ?", (name,)).fetchone()
            if same_name is not None and same_name["reference"] != reference:
                if same_name["archived"]:
                    raise ValueError("An archived facility uses that name. Find it in Facilities to restore it.")
                if (not reference and same_name["facility_type"] == facility_type and same_name["contract_status"] == status
                        and same_name["hospital_pos_code"] == hospital_pos_code
                        and all(same_name[key] == value for key, value in details.items())):
                    return _facility(same_name)
                raise ValueError("That location name is already in use. Choose the saved location or use a different name.")
            reference = reference or f"{kind}-{uuid4().hex}"
            connection.execute("""
                INSERT INTO facilities (reference, name, kind, contract_status, facility_type, hospital_pos_code)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (reference) DO UPDATE SET name = excluded.name, kind = excluded.kind,
                    contract_status = excluded.contract_status, facility_type = excluded.facility_type,
                    hospital_pos_code = excluded.hospital_pos_code
            """, (reference, name, kind, status, facility_type, hospital_pos_code))
            if details:
                connection.execute("""
                    INSERT INTO site_details (
                        reference, legal_name, npi, street_address, address_line_2, city, region, postal_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (reference) DO UPDATE SET legal_name = excluded.legal_name, npi = excluded.npi,
                        street_address = excluded.street_address, address_line_2 = excluded.address_line_2,
                        city = excluded.city, region = excluded.region, postal_code = excluded.postal_code
                """, (reference, *details.values()))
            return _facility(connection.execute(SITE_SELECT + " WHERE f.reference = ?", (reference,)).fetchone())

    def set_facility_archived(self, reference, archived):
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                "UPDATE facilities SET archived = ? WHERE reference = ? AND reference != 'alf'",
                (int(archived), reference),
            )
            if updated.rowcount == 0:
                raise KeyError("Facility not found.")
            return _facility(connection.execute(SITE_SELECT + " WHERE f.reference = ?", (reference,)).fetchone())

    def _save_home(self, connection, patient, reference, values):
        arrangement = values.get("living_arrangement")
        if arrangement not in LIVING_ARRANGEMENTS:
            raise ValueError("Choose whether the patient lives alone or with a caregiver.")
        address = _address_values(values)
        existing = connection.execute("SELECT * FROM patient_homes WHERE reference = ?", (reference,)).fetchone()
        if existing is None:
            connection.execute("""
                INSERT INTO patient_homes (reference, patient, living_arrangement,
                    street_address, address_line_2, city, region, postal_code)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (reference, patient, arrangement, *address.values()))
        elif (existing["patient"], existing["living_arrangement"]) != (patient, arrangement) or any(
            existing[key] != value for key, value in address.items()
        ):
            raise ValueError("Choose a saved home for this patient or add a new one.")

    def book(self, values):
        location, mode, day, slot = (values.get(key) for key in ("location", "mode", "day", "time"))
        if not isinstance(location, str) or mode not in ("in-person", "telemedicine"):
            raise ValueError("Choose a location and visit mode.")
        try:
            if date.fromisoformat(day).isoformat() != day or time.fromisoformat(slot).isoformat(timespec="minutes") != slot:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("Enter a valid appointment date and time.") from None

        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if "group" in values:
                return self._book_group(connection, values, (location, mode, day, slot))
            patient = self._ensure_patient(connection, values)
            self._book_location(connection, patient, location, values)
            booking = (patient, location, mode, day, slot)
            connection.execute("""
                INSERT INTO appointments (patient, location, mode, day, time, events)
                VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (patient, location, mode, day, time) DO NOTHING
            """, (*booking, json.dumps([_event("scheduled")])))
            row = connection.execute(
                APPOINTMENT_SELECT + " WHERE a.patient = ? AND a.location = ? AND a.mode = ? AND a.day = ? AND a.time = ?",
                booking,
            ).fetchone()
            return _appointment(row)

    def _book_location(self, connection, patient, location, values):
        kind = values.get("location_kind")
        if "new_location" in values:
            details = values["new_location"]
            if kind != "home":
                raise ValueError("Register the office or facility in Facilities, then select it for the appointment.")
            if not _location_reference(location, "home") or not isinstance(details, dict):
                raise ValueError("Enter the new home details.")
            self._save_home(connection, patient, location, details)
        elif kind == "home":
            home = connection.execute(
                "SELECT 1 FROM patient_homes WHERE reference = ? AND patient = ?", (location, patient),
            ).fetchone()
            if home is None:
                raise ValueError("Choose a saved home for this patient or add a new one.")
        else:
            site = connection.execute("SELECT kind, archived FROM facilities WHERE reference = ?", (location,)).fetchone()
            if site is None or (kind is not None and site["kind"] != kind):
                raise ValueError("Choose a registered location of the selected type.")
            if site["archived"]:
                raise ValueError("This facility is archived. Choose an active location.")

    def _book_group(self, connection, values, slot):
        group = values["group"]
        if not isinstance(group, dict) or not _location_reference(group.get("reference"), "group"):
            raise ValueError("Open a new group appointment to enter the group details.")
        name = _location_text(group, "name", "group name", 120, optional=True) or "Group visit"
        entries = group.get("patients")
        if not isinstance(entries, list) or not entries or any(not isinstance(entry, dict) for entry in entries):
            raise ValueError("Add at least one patient to the group.")
        patients = [self._ensure_patient(connection, entry) for entry in entries]
        if len(set(patients)) != len(patients):
            raise ValueError("Add each patient to the group once.")
        home_patient = values.get("home_patient")
        if values.get("location_kind") == "home" and home_patient not in patients:
            raise ValueError("Choose which group patient the home belongs to.")
        self._book_location(connection, home_patient, slot[0], values)
        reference = group["reference"]
        appointment = connection.execute(
            "SELECT id, group_name, location, mode, day, time FROM appointments WHERE group_reference = ?",
            (reference,),
        ).fetchone()
        if appointment is None:
            appointment_id = connection.execute("""
                INSERT INTO appointments (group_reference, group_name, location, mode, day, time, events)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (reference, name, *slot, json.dumps([_event("scheduled")]))).lastrowid
            connection.executemany("""
                INSERT INTO appointment_members (appointment_id, patient, position) VALUES (?, ?, ?)
            """, ((appointment_id, patient, order) for order, patient in enumerate(patients)))
        else:
            appointment_id = appointment["id"]
            roster = [row["patient"] for row in connection.execute(
                "SELECT patient FROM appointment_members WHERE appointment_id = ? ORDER BY position", (appointment_id,),
            )]
            if tuple(appointment)[1:] != (name, *slot) or roster != patients:
                raise TransitionConflict("This group appointment is already booked. Open a new appointment to change it.")
        return _appointment(connection.execute(APPOINTMENT_SELECT + " WHERE a.id = ?", (appointment_id,)).fetchone())

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
            npi = re.fullmatch(rf"/api/facilities/npi/({NPI_PATTERN})", self.path)
            homes = re.fullmatch(rf"/api/patients/{PATIENT_REFERENCE}/homes", self.path)
            roster = re.fullmatch(r"/api/appointments/([1-9][0-9]{0,17})/patients", self.path)
            if self.path in STATIC_ROUTES:
                filename, content_type = STATIC_ROUTES[self.path]
                self.respond(HTTPStatus.OK, (ASSETS / filename).read_bytes(), content_type)
            elif self.path == "/api/appointments":
                self.respond(HTTPStatus.OK, desk.appointments())
            elif self.path == "/api/patients":
                self.respond(HTTPStatus.OK, desk.patients())
            elif self.path == "/api/facilities":
                self.respond(HTTPStatus.OK, desk.facilities())
            elif self.path == "/api/facility-types":
                self.respond(HTTPStatus.OK, {
                    "types": [
                        {"value": value, "label": label, "pos_code": pos_code}
                        for value, (label, pos_code) in FACILITY_TYPES.items()
                    ],
                    "hospital_settings": HOSPITAL_SETTINGS,
                })
            elif npi:
                try:
                    self.respond(HTTPStatus.OK, lookup_facility(npi[1]))
                except RegistryUnavailable as error:
                    self.respond(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            elif homes:
                self.respond(HTTPStatus.OK, desk.patient_homes(homes[1]))
            elif roster:
                try:
                    self.respond(HTTPStatus.OK, desk.appointment_patients(int(roster[1])))
                except KeyError as error:
                    self.respond(HTTPStatus.NOT_FOUND, {"error": error.args[0]})
            else:
                self.respond(HTTPStatus.NOT_FOUND, {"error": "Page not found."})

        def do_POST(self):
            advance = re.fullmatch(r"/api/appointments/([1-9][0-9]{0,17})/advance", self.path)
            insurance = re.fullmatch(rf"/api/patients/{PATIENT_REFERENCE}/insurance", self.path)
            facility_action = re.fullmatch(r"/api/facilities/(clinic|(?:clinic|alf)-[0-9a-f]{32})/(archive|restore)", self.path)
            if self.path not in ("/api/appointments", "/api/facilities") and not (advance or insurance or facility_action):
                self.respond(HTTPStatus.NOT_FOUND, {"error": "Page not found."})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                if self.headers.get_content_type() != "application/json" or not 0 < length <= 65536:
                    raise ValueError("Send a JSON request of up to 64 KB.")
                values = json.loads(self.rfile.read(length))
                if not isinstance(values, dict):
                    raise ValueError("Send a JSON object.")
                if advance:
                    result = desk.advance(int(advance[1]), values.get("stage"), chart_complete=values.get("chart_complete"))
                elif insurance:
                    result = desk.save_insurance(insurance[1], values)
                elif facility_action:
                    result = desk.set_facility_archived(facility_action[1], facility_action[2] == "archive")
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
