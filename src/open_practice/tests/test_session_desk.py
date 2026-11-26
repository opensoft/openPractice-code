import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest import TestCase
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from open_practice.session_desk import SessionDesk, make_server


class SessionDeskTest(TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.database = Path(directory) / "sessions.sqlite3"
        self.server = self.enterContext(make_server(SessionDesk(self.database), port=0))
        thread = Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def request(self, path, values=None):
        options = {} if values is None else {
            "data": json.dumps(values).encode("utf-8"), "headers": {"Content-Type": "application/json"},
        }
        with urlopen(Request(self.url + path, **options), timeout=3) as response:
            return json.load(response)

    def book(self, location="clinic", mode="in-person"):
        return self.request("/api/appointments", {
            "patient": "demo-01", "location": location, "mode": mode, "day": "2026-11-26", "time": "09:00",
        })

    def test_each_location_can_host_either_visit_mode(self):
        for location in ("clinic", "alf"):
            for mode in ("in-person", "telemedicine"):
                with self.subTest(location=location, mode=mode):
                    booked = self.book(location, mode)
                    self.assertEqual((booked["location"], booked["mode"], booked["stage"]), (location, mode, "scheduled"))
                    self.assertEqual(self.book(location, mode)["id"], booked["id"])
        self.assertEqual(len(self.request("/api/appointments")), 4)

    def test_named_alf_can_be_booked_and_its_contract_status_updated(self):
        legacy = self.book("alf")
        name = "Example Assisted Living"
        facility = self.request("/api/facilities", {"name": name})
        self.assertEqual(facility["contract_status"], "unknown")
        self.assertEqual(self.request("/api/facilities", {"name": name}), facility)

        first = self.book(facility["reference"], "in-person")
        second = self.book(facility["reference"], "telemedicine")
        updated = self.request("/api/facilities", {**facility, "contract_status": "contracted"})
        self.assertEqual(self.request("/api/facilities")[-1], updated)
        self.assertEqual(updated["reference"], facility["reference"])

        saved = {row["id"]: row for row in SessionDesk(self.database).appointments()}
        self.assertEqual(saved[legacy["id"]], legacy)
        for booked in (first, second):
            self.assertEqual(saved[booked["id"]], {**booked, "facility_name": name, "contract_status": "contracted"})
        self.assertEqual(self.request("/api/facilities", {**updated, "contract_status": "no_contract"})["contract_status"], "no_contract")

    def test_booking_registers_patient_once_and_reuses_the_record(self):
        patient = "patient-" + "a" * 32
        details = {"first_name": "Practice", "last_name": "Example", "date_of_birth": "1980-04-03", "chart_reference": "OC-TEST-01"}
        values = {
            "patient": patient, "new_patient": details, "location": "alf", "mode": "telemedicine",
            "day": "2026-11-26", "time": "09:00",
        }
        booked = self.request("/api/appointments", values)
        self.assertEqual(self.request("/api/appointments", values)["id"], booked["id"])
        self.assertEqual(booked["patient_details"], details)
        self.assertEqual([row for row in self.request("/api/patients") if row["reference"] == patient], [
            {"reference": patient, **details},
        ])

        repeat = self.request("/api/appointments", {
            "patient": patient, "location": "clinic", "mode": "in-person", "day": "2026-11-27", "time": "11:15",
        })
        self.assertNotEqual(repeat["id"], booked["id"])
        self.assertEqual(repeat["patient_details"], details)
        self.assertEqual(len(SessionDesk(self.database).appointments()), 2)

    def test_rejected_bookings_leave_patient_directory_unchanged(self):
        values = {
            "patient": "patient-" + "a" * 32, "location": "clinic", "mode": "in-person",
            "day": "2026-11-26", "time": "09:00",
        }
        before = self.request("/api/patients")
        for wrong in (
            {**values, "new_patient": {"first_name": "Practice"}},
            {**values, "new_patient": {"first_name": "Practice", "last_name": "Example", "date_of_birth": "2026-02-30"}},
            {**values, "new_patient": {"first_name": "Practice", "last_name": "Example"}, "time": "25:00"},
            {**values, "new_patient": {"first_name": "Practice", "last_name": "Example"}, "location": "unregistered"},
        ):
            with self.subTest(wrong=wrong), self.assertRaises(HTTPError) as rejected:
                self.request("/api/appointments", wrong)
            self.assertEqual(rejected.exception.code, 400)
            with rejected.exception:
                rejected.exception.read()
        self.assertEqual(self.request("/api/patients"), before)
        self.assertEqual(self.request("/api/appointments"), [])

    def test_session_end_allows_admin_handoff_and_chart_confirmation_enables_billing(self):
        booked = self.book("alf", "telemedicine")
        path = f"/api/appointments/{booked['id']}/advance"
        with self.assertRaises(HTTPError) as skipped:
            self.request(path, {"stage": "ready"})
        self.assertEqual(skipped.exception.code, 409)
        with skipped.exception:
            skipped.exception.read()

        for stage, expected in (("scheduled", "available"), ("available", "ready"), ("ready", "started"), ("started", "ended")):
            updated = self.request(path, {"stage": stage})
            self.assertEqual(updated["stage"], expected)
            self.assertFalse(updated["ready_for_billing"])
        with self.assertRaises(HTTPError) as premature:
            self.request(path, {"stage": "ended"})
        self.assertEqual(premature.exception.code, 400)
        with premature.exception:
            premature.exception.read()

        wrapped = self.request(path, {"stage": "ended", "chart_complete": True})
        self.assertTrue(wrapped["ready_for_billing"])
        self.assertEqual([event["stage"] for event in wrapped["events"]], [
            "scheduled", "available", "ready", "started", "ended", "wrapped",
        ])
        with self.assertRaises(HTTPError) as replay:
            self.request(path, {"stage": "ended", "chart_complete": True})
        self.assertEqual(replay.exception.code, 409)
        with replay.exception:
            replay.exception.read()
        self.assertEqual(SessionDesk(self.database).appointments(), [wrapped])
