import json
from contextlib import closing
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest import TestCase
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from open_practice.session_desk import SessionDesk, make_server


ADDRESS = {
    "street_address": "10 Example Street",
    "address_line_2": "Suite 2",
    "city": "Denver",
    "region": "CO",
    "postal_code": "80202",
}
SITE = {"legal_name": "Practice Example Company", "npi": "1234567893", **ADDRESS}
INSURANCE = {
    "payer_name": "Example Health",
    "payer_id": "10000",
    "plan_name": "Example Plan",
    "member_id": "SYNTHETIC-001",
    "group_number": "EXAMPLE-GRP",
}


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
        for location, pos_code in (("clinic", "11"), ("alf", "13")):
            for mode in ("in-person", "telemedicine"):
                with self.subTest(location=location, mode=mode):
                    booked = self.book(location, mode)
                    self.assertEqual((booked["location"], booked["mode"], booked["stage"]), (location, mode, "scheduled"))
                    self.assertEqual(booked["pos_code"], pos_code)
                    self.assertEqual(self.book(location, mode)["id"], booked["id"])
        self.assertEqual(len(self.request("/api/appointments")), 4)

    def test_named_alf_can_be_booked_and_its_contract_status_updated(self):
        legacy = self.book("alf")
        name = "Example Assisted Living"
        facility = self.request("/api/facilities", {"name": name, "facility_type": "alf", **SITE})
        self.assertEqual(facility["contract_status"], "unknown")
        self.assertEqual(self.request("/api/facilities", {"name": name, "facility_type": "alf", **SITE}), facility)

        first = self.book(facility["reference"], "in-person")
        second = self.book(facility["reference"], "telemedicine")
        pharmacy = self.request("/api/facilities", {
            **SITE, "name": "Example Pharmacy", "facility_type": "pharmacy", "npi": "0123456789",
        })
        contract = {"contract_status": "contracted", "contract_pharmacy_reference": pharmacy["reference"]}
        updated = self.request("/api/facilities", {
            "reference": facility["reference"], "name": name, **contract,
        })
        self.assertIn(updated, self.request("/api/facilities"))
        self.assertEqual(updated["reference"], facility["reference"])
        self.assertEqual({key: updated[key] for key in SITE}, SITE)

        saved = {row["id"]: row for row in SessionDesk(self.database).appointments()}
        self.assertEqual(saved[legacy["id"]], legacy)
        for booked in (first, second):
            self.assertEqual(saved[booked["id"]], {
                **booked, "facility_name": name, **contract,
                "contract_pharmacy_name": pharmacy["name"], "contract_pharmacy_npi": pharmacy["npi"],
            })
        cleared = self.request("/api/facilities", {**updated, "contract_status": "no_contract"})
        self.assertEqual(cleared["contract_status"], "no_contract")
        self.assertIsNone(cleared["contract_pharmacy_name"])
        self.assertIsNone(cleared["contract_pharmacy_reference"])

    def test_contract_follows_pharmacy_directory_edits_and_preserves_an_archived_link(self):
        pharmacy = self.request("/api/facilities", {
            **SITE, "name": "Example Pharmacy", "facility_type": "pharmacy", "npi": "0123456789",
        })
        values = {
            "name": "Example office", "facility_type": "office", **SITE,
            "contract_status": "contracted",
        }
        for bad_reference in ("clinic", "missing"):
            with self.subTest(bad_reference=bad_reference), self.assertRaises(HTTPError) as rejected:
                self.request("/api/facilities", {**values, "contract_pharmacy_reference": bad_reference})
            with rejected.exception as response:
                self.assertEqual(response.code, 400)
        facility = self.request("/api/facilities", {**values, "contract_pharmacy_reference": pharmacy["reference"]})
        self.assertEqual(facility["npi"], SITE["npi"])
        self.assertEqual(facility["contract_pharmacy_npi"], "0123456789")
        with self.assertRaises(HTTPError) as rejected:
            self.request("/api/facilities", {
                "reference": pharmacy["reference"], "name": pharmacy["name"], "facility_type": "office",
            })
        with rejected.exception as response:
            self.assertEqual(response.code, 400)
        self.request("/api/facilities", {
            "reference": pharmacy["reference"], "name": "Renamed Pharmacy", **{**SITE, "npi": "0123456788"},
        })
        self.request(f"/api/facilities/{pharmacy['reference']}/archive", {})
        kept = self.request("/api/facilities", {"reference": facility["reference"], "name": facility["name"]})
        self.assertEqual(kept["contract_pharmacy_reference"], pharmacy["reference"])
        self.assertEqual(kept["contract_pharmacy_name"], "Renamed Pharmacy")
        self.assertEqual(self.book(facility["reference"])["contract_pharmacy_npi"], "0123456788")
        with self.assertRaises(HTTPError) as rejected:
            self.request("/api/facilities", {
                **values, "name": "Another Office", "contract_pharmacy_reference": pharmacy["reference"],
            })
        with rejected.exception as response:
            self.assertEqual(response.code, 400)

    def test_legacy_contract_details_remain_visible_until_linked_to_the_directory(self):
        office = self.request("/api/facilities", {"name": "Example Office", "facility_type": "office", **SITE})
        with closing(SessionDesk(self.database).connect()) as connection, connection:
            connection.execute("ALTER TABLE facilities DROP COLUMN contract_pharmacy_reference")
            connection.execute("""
                UPDATE facilities SET contract_status = 'contracted', contract_pharmacy_name = 'Old Pharmacy',
                    contract_pharmacy_npi = '0123456789' WHERE reference = ?
            """, (office["reference"],))
        SessionDesk(self.database)
        values = {"reference": office["reference"], "name": office["name"], "contract_pharmacy_reference": ""}
        unchanged = self.request("/api/facilities", values)
        self.assertIsNone(unchanged["contract_pharmacy_reference"])
        self.assertEqual(unchanged["contract_pharmacy_name"], "Old Pharmacy")
        pharmacy = self.request("/api/facilities", {"name": "Directory Pharmacy", "facility_type": "pharmacy", **SITE})
        linked = self.request("/api/facilities", {**values, "contract_pharmacy_reference": pharmacy["reference"]})
        self.assertEqual(linked["contract_pharmacy_name"], pharmacy["name"])
        self.assertEqual(linked["contract_pharmacy_npi"], pharmacy["npi"])

    def test_pharmacies_do_not_have_contract_status_or_identifiers(self):
        contract = {
            "contract_status": "contracted",
            "contract_pharmacy_reference": self.request("/api/facilities", {
                "name": "Directory Pharmacy", "facility_type": "pharmacy", **SITE,
            })["reference"],
        }
        values = {"name": "Example facility", "facility_type": "alf", **SITE, **contract}
        old = self.request("/api/facilities", values)
        pharmacy = self.request("/api/facilities", {
            **values, "facility_type": "pharmacy", "reference": old["reference"],
        })
        self.assertIn(pharmacy, self.request("/api/facilities"))
        for record in (pharmacy, self.book(pharmacy["reference"])):
            for field in (*contract, "contract_pharmacy_name", "contract_pharmacy_npi"):
                self.assertIsNone(record[field])

    def test_facility_types_are_saved_in_the_directory_and_available_when_booking(self):
        pos_codes = {
            "office": "11", "alf": "13", "snf": "31", "hospital": None, "pharmacy": "01",
            "retail_clinic": "17", "urgent_care": "20", "asc": "24", "fqhc": "50", "rhc": "72", "other": "99",
        }
        catalog = self.request("/api/facility-types")
        self.assertEqual({item["value"]: item["pos_code"] for item in catalog["types"]}, pos_codes)
        self.assertEqual(set(catalog["hospital_settings"]), {"19", "21", "22", "23"})
        for facility_type, pos_code in pos_codes.items():
            with self.subTest(facility_type=facility_type):
                kind = "clinic" if facility_type == "office" else "alf"
                details = {"name": f"Example {facility_type}", "facility_type": facility_type, **SITE}
                if facility_type == "hospital":
                    pos_code = details["pos_code"] = "22"
                saved = self.request("/api/facilities", details)
                self.assertEqual(saved["pos_code"], pos_code)
                values = {
                    "patient": "demo-01", "location_kind": kind, "location": saved["reference"],
                    "mode": "telemedicine", "day": "2026-11-26", "time": "09:00",
                }
                booked = self.request("/api/appointments", values)
                self.assertEqual(booked["location_kind"], kind)
                self.assertEqual(booked["facility_type"], facility_type)
                self.assertEqual(booked["pos_code"], pos_code)
                self.assertEqual(booked["location_address"], ADDRESS)
                self.assertEqual(self.request("/api/appointments", values), booked)
                self.assertIn(saved, self.request("/api/facilities"))
                self.assertEqual({key: saved[key] for key in details}, details)
                self.assertEqual(saved["contract_status"], None if facility_type == "pharmacy" else "unknown")

    def test_hospital_setting_is_optional_and_can_be_set_on_legacy_records(self):
        with closing(SessionDesk(self.database).connect()) as connection, connection:
            connection.execute("ALTER TABLE facilities DROP COLUMN hospital_pos_code")
            connection.execute("ALTER TABLE facilities DROP COLUMN contract_pharmacy_name")
            connection.execute("ALTER TABLE facilities DROP COLUMN contract_pharmacy_npi")
            connection.execute("""
                INSERT INTO facilities (reference, name, kind, facility_type)
                VALUES ('legacy-hospital', 'Legacy hospital', 'alf', 'hospital')
            """)
        SessionDesk(self.database)
        legacy = self.request("/api/facilities")[-1]
        self.assertIsNone(legacy["pos_code"])
        booked = self.book(legacy["reference"])
        self.assertIsNone(booked["pos_code"])

        details = {"name": "Example Hospital", "facility_type": "hospital"}
        self.assertIsNone(self.request("/api/facilities", details)["pos_code"])
        self.assertIsNone(self.request("/api/facilities", {**details, "pos_code": ""})["pos_code"])
        for selection in ({"pos_code": "99"}, {"pos_code": 19}):
            with self.subTest(selection=selection), self.assertRaises(HTTPError) as rejected:
                self.request("/api/facilities", {**details, **selection})
            with rejected.exception as response:
                self.assertEqual(response.code, 400)

        for code in ("19", "21", "22", "23"):
            updated = self.request("/api/facilities", {
                "reference": legacy["reference"], "name": legacy["name"], "pos_code": code,
            })
            self.assertEqual(updated["pos_code"], code)
            self.assertEqual(self.request("/api/appointments")[0]["pos_code"], code)

        cleared = self.request("/api/facilities", {"reference": legacy["reference"], "name": legacy["name"], "pos_code": ""})
        self.assertIsNone(cleared["pos_code"])
        changed_type = self.request("/api/facilities", {**updated, "facility_type": "urgent_care", **SITE})
        self.assertEqual(changed_type["pos_code"], "20")

    def test_facility_creation_requires_only_location_name_and_uses_the_directory(self):
        details = {"name": "Example Hospital", **SITE}
        for invalid in ({}, {"name": " "}, {**details, "facility_type": "home"}, {**details, "npi": "bad"}):
            with self.subTest(invalid=invalid), self.assertRaises(HTTPError) as rejected:
                self.request("/api/facilities", invalid)
            with rejected.exception as response:
                self.assertEqual(response.code, 400)
        with self.assertRaises(HTTPError) as rejected:
            self.request("/api/appointments", {
                "patient": "demo-01", "location_kind": "alf", "location": "alf-" + "e" * 32,
                "mode": "in-person", "day": "2026-11-26", "time": "09:00",
                "new_location": {**details, "facility_type": "hospital"},
            })
        with rejected.exception as response:
            self.assertEqual(response.code, 400)
        self.assertEqual(self.request("/api/appointments"), [])
        self.assertEqual([row["reference"] for row in self.request("/api/facilities")], ["clinic"])

        named = self.request("/api/facilities", {"name": "Example Facility"})
        self.assertIsNone(named["facility_type"])
        self.assertEqual((named["npi"], named["legal_name"], named["street_address"]), ("", "", ""))
        self.assertEqual(self.request("/api/facilities", {"name": named["name"], "facility_type": ""}), named)
        booked = self.book(named["reference"])
        self.assertEqual(booked["facility_name"], named["name"])
        self.assertIsNone(booked["pos_code"])

        edit = {"reference": named["reference"], "name": named["name"]}
        self.request("/api/facilities", {**edit, "city": "Denver", "npi": "1234567893"})
        changed = self.request("/api/facilities", {**edit, "npi": ""})
        self.assertEqual(changed["city"], "Denver")
        self.assertEqual(changed["npi"], "")
        self.assertEqual(self.request("/api/facilities", {**edit, "city": ""})["city"], "")

    def test_facility_npi_lookup_uses_organization_name_and_practice_address(self):
        npi = SITE["npi"]
        payload = {"result_count": 1, "results": [{
            "number": npi, "enumeration_type": "NPI-2",
            "basic": {"organization_name": "Example Health LLC", "status": "A", "authorized_official_first_name": "Hidden"},
            "other_names": [
                {"organization_name": "Previous name", "type": "Former Legal Business Name"},
                {"organization_name": "Example Health", "type": "Doing Business As"},
            ],
            "taxonomies": [{"code": "314000000X", "primary": True}],
            "addresses": [
                {"address_purpose": "MAILING", "address_1": "PO Box 12"},
                {
                    "address_purpose": "LOCATION", "address_1": "10 Example Street", "address_2": "Suite 2",
                    "city": "Denver", "state": "CO", "postal_code": "802020001", "country_code": "US",
                },
            ],
        }]}
        for code, facility_type in (
            ("314000000X", "snf"),
            ("333600000X", "pharmacy"),
            ("3336L0003X", "pharmacy"),
            ("3336C0003X", "pharmacy"),
        ):
            payload["results"][0]["taxonomies"][0]["code"] = code
            with self.subTest(code=code), patch(
                "open_practice.npi_registry.urlopen", return_value=BytesIO(json.dumps(payload).encode()),
            ) as registry:
                result = self.request(f"/api/facilities/npi/{npi}")
                request = urlsplit(registry.call_args.args[0].full_url)
                self.assertEqual(request.netloc, "npiregistry.cms.hhs.gov")
                self.assertEqual(parse_qs(request.query), {"version": ["2.1"], "number": [npi]})
                self.assertEqual(result, {"match": "organization", "fields": {
                    **ADDRESS, "postal_code": "80202-0001", "facility_type": facility_type,
                    "name": "Example Health", "legal_name": "Example Health LLC",
                }})
        self.assertEqual([row["reference"] for row in self.request("/api/facilities")], ["clinic"])

    def test_facility_npi_lookup_leaves_unmatched_and_individual_records_manual(self):
        path = f"/api/facilities/npi/{SITE['npi']}"
        for records, match in (
            ([], "not_found"),
            ([{"number": SITE["npi"], "enumeration_type": "NPI-1", "basic": {"first_name": "Example"}}], "individual"),
            ([{"number": SITE["npi"], "enumeration_type": "NPI-2", "basic": {"status": "D"}}], "inactive"),
        ):
            payload = BytesIO(json.dumps({"results": records}).encode())
            with self.subTest(match=match), patch("open_practice.npi_registry.urlopen", return_value=payload):
                self.assertEqual(self.request(path), {"match": match})
        with patch("open_practice.npi_registry.urlopen", side_effect=TimeoutError), self.assertRaises(HTTPError) as failed:
            self.request(path)
        with failed.exception as response:
            self.assertEqual(response.code, 502)
            self.assertIn("manually", json.load(response)["error"])

    def test_facility_npi_search_falls_back_to_location_name_and_ranks_practice_addresses(self):
        primary = {
            "address_purpose": "LOCATION", "address_1": "25 Other Street", "city": "DENVER",
            "state": "CO", "postal_code": "802020001", "country_code": "US",
        }
        secondary = {**primary, "address_1": "10 EXAMPLE ST", "address_2": "STE 2"}
        organization = {
            "number": 1234567893, "enumeration_type": "NPI-2",
            "basic": {"organization_name": "Example Medical LLC", "status": "A"},
            "addresses": [primary],
        }
        records = [
            {**organization, "number": 1234567894},
            {**organization, "practiceLocations": [secondary]},
            {**organization, "number": 1234567895, "enumeration_type": "NPI-1"},
            {**organization, "number": 1234567896, "basic": {"status": "D"}},
        ]
        responses = [BytesIO(json.dumps({"results": items}).encode()) for items in ([], records)]
        path = "/api/facilities/npi/search"
        with patch("open_practice.npi_registry.urlopen", side_effect=responses) as registry:
            result = self.request(path, {
                **ADDRESS, "postal_code": "80202-0001", "name": "Example Medical",
                "legal_name": "Example Medical Associates",
            })
            queries = [parse_qs(urlsplit(call.args[0].full_url).query) for call in registry.call_args_list]
        self.assertEqual([query["organization_name"] for query in queries], [
            ["EXAMPLE MEDICAL ASSOCIATES*"], ["EXAMPLE MEDICAL*"],
        ])
        self.assertEqual(queries[1], {
            "version": ["2.1"], "enumeration_type": ["NPI-2"], "address_purpose": ["LOCATION"],
            "limit": ["50"], "city": ["Denver"], "state": ["CO"], "postal_code": ["80202"],
            "organization_name": ["EXAMPLE MEDICAL*"],
        })
        self.assertEqual([match["npi"] for match in result["matches"]], ["1234567893", "1234567894"])
        self.assertEqual(result["matches"][0]["fields"]["street_address"], "10 EXAMPLE ST")
        self.assertEqual(result["matches"][0]["fields"]["address_line_2"], "STE 2")
        self.assertEqual([row["reference"] for row in self.request("/api/facilities")], ["clinic"])

        responses = [BytesIO(json.dumps({"results": items}).encode()) for items in ([], records)]
        with patch("open_practice.npi_registry.urlopen", side_effect=responses) as registry:
            result = self.request(path, {**ADDRESS, "name": "Unregistered venue name"})
            address_query = parse_qs(urlsplit(registry.call_args.args[0].full_url).query)
        self.assertNotIn("organization_name", address_query)
        self.assertEqual([match["npi"] for match in result["matches"]], ["1234567893"])

    def test_facility_npi_search_handles_missing_criteria_no_matches_and_unavailable_registry(self):
        path = "/api/facilities/npi/search"
        with patch("open_practice.npi_registry.urlopen") as registry, self.assertRaises(HTTPError) as rejected:
            self.request(path, {"street_address": "10 Example Street"})
        registry.assert_not_called()
        with rejected.exception as response:
            self.assertEqual(response.code, 400)
            self.assertIn("ZIP", json.load(response)["error"])
        with patch("open_practice.npi_registry.urlopen", return_value=BytesIO(b'{"results":[]}')):
            self.assertEqual(self.request(path, {"city": "Denver"}), {"matches": [], "limited": False})
        with patch("open_practice.npi_registry.urlopen", side_effect=TimeoutError), self.assertRaises(HTTPError) as failed:
            self.request(path, {"name": "Example Medical"})
        with failed.exception as response:
            self.assertEqual(response.code, 502)

    def test_archived_facility_preserves_appointment_history_and_can_be_restored(self):
        details = {"name": "Example Hospital", "facility_type": "hospital", "pos_code": "22", **SITE}
        facility = self.request("/api/facilities", details)
        reference = facility["reference"]
        first = self.book(reference)
        archive = f"/api/facilities/{reference}/archive"
        self.assertEqual(self.request(archive, {})["archived"], 1)
        self.assertEqual(self.request(archive, {})["archived"], 1)
        self.assertEqual(self.request("/api/appointments"), [first])
        self.assertEqual(self.request("/api/facilities")[-1]["archived"], 1)

        for path, values in (
            ("/api/facilities", details),
            ("/api/appointments", {
                "patient": "demo-02", "location_kind": "alf", "location": reference,
                "mode": "in-person", "day": "2026-11-26", "time": "10:00",
            }),
        ):
            with self.subTest(path=path), self.assertRaises(HTTPError) as rejected:
                self.request(path, values)
            with rejected.exception as response:
                self.assertEqual(response.code, 400)
                self.assertIn("archived", json.load(response)["error"])

        restored = self.request(f"/api/facilities/{reference}/restore", {})
        self.assertEqual(restored, facility)
        second = self.book(reference, "telemedicine")
        self.assertEqual([row["id"] for row in self.request("/api/appointments")], [first["id"], second["id"]])

    def test_booking_saves_home_for_the_patient_and_prevents_cross_patient_use(self):
        patient = "patient-" + "c" * 32
        reference = "home-" + "d" * 32
        values = {
            "patient": patient, "location_kind": "home", "location": reference,
            "mode": "in-person", "day": "2026-11-26", "time": "09:00",
            "new_patient": {"first_name": "Practice", "last_name": "Example"},
            "new_location": {"living_arrangement": "with_caregiver", **ADDRESS},
        }
        booked = self.request("/api/appointments", values)
        self.assertEqual(self.request("/api/appointments", values), booked)
        self.assertEqual((booked["location_kind"], booked["living_arrangement"]), ("home", "with_caregiver"))
        self.assertEqual(booked["pos_code"], "12")
        self.assertEqual(booked["location_address"], ADDRESS)
        self.assertEqual(self.request(f"/api/patients/{patient}/homes"), [{"reference": reference, **ADDRESS}])
        self.assertEqual(self.request("/api/patients/demo-01/homes"), [])
        self.assertNotIn(reference, {row["reference"] for row in self.request("/api/facilities")})

        saved = {key: value for key, value in values.items() if not key.startswith("new_")}
        with self.assertRaises(HTTPError) as rejected:
            self.request("/api/appointments", {**saved, "patient": "demo-01"})
        with rejected.exception as response:
            self.assertEqual(response.code, 400)
        again = self.request("/api/appointments", {**saved, "time": "10:00", "mode": "telemedicine"})
        self.assertEqual(again["location"], reference)
        self.assertEqual(again["pos_code"], "12")
        self.assertEqual(SessionDesk(self.database).appointments(), [booked, again])

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

    def test_group_booking_keeps_one_session_and_editable_insurance_per_patient(self):
        patient = "patient-" + "a" * 32
        group = {
            "reference": "group-" + "b" * 32, "name": "Vaccination Group A",
            "patients": [
                {"patient": "demo-01"},
                {
                    "patient": patient,
                    "new_patient": {"first_name": "Practice", "last_name": "Example", "chart_reference": "GROUP-01"},
                    "insurance": INSURANCE,
                },
            ],
        }
        slot = {"location": "clinic", "location_kind": "clinic", "mode": "in-person", "day": "2026-11-26", "time": "09:00"}
        values = {"group": group, **slot}
        booked = self.request("/api/appointments", values)
        self.assertEqual(booked["group_name"], "Vaccination Group A")
        self.assertEqual(booked["member_count"], 2)
        self.assertIsNone(booked["patient"])
        self.assertIsNone(booked["patient_details"])
        self.assertNotIn(INSURANCE["member_id"], json.dumps(self.request("/api/appointments")))
        self.assertNotIn(INSURANCE["member_id"], json.dumps(self.request("/api/patients")))
        self.assertEqual(self.request("/api/appointments", values), booked)

        path = f"/api/appointments/{booked['id']}/patients"
        roster = self.request(path)
        self.assertEqual([entry["reference"] for entry in roster], ["demo-01", patient])
        self.assertIsNone(roster[0]["insurance"])
        self.assertEqual(roster[1]["chart_reference"], "GROUP-01")
        self.assertEqual(roster[1]["insurance"], INSURANCE)

        first_patient_insurance = {**INSURANCE, "member_id": "SYNTHETIC-002"}
        self.request("/api/patients/demo-01/insurance", first_patient_insurance)
        self.request("/api/appointments", values)
        self.assertEqual(self.request(path)[0]["insurance"], first_patient_insurance)
        self.assertEqual(self.request(path)[1]["insurance"], INSURANCE)
        individual = self.request("/api/appointments", {**slot, "patient": "demo-01"})
        self.assertNotEqual(individual["id"], booked["id"])
        self.assertEqual(self.request(f"/api/appointments/{individual['id']}/patients")[0]["insurance"], first_patient_insurance)
        moved = self.request(f"/api/appointments/{booked['id']}/advance", {"stage": "scheduled"})
        self.assertEqual((moved["stage"], moved["member_count"]), ("available", 2))
        self.assertEqual(SessionDesk(self.database).appointments(), [moved, individual])

    def test_group_booking_rejects_invalid_roster_and_coverage_atomically(self):
        values = {
            "location": "clinic", "mode": "in-person", "day": "2026-11-26", "time": "09:00",
        }
        patient = {
            "patient": "patient-" + "a" * 32,
            "new_patient": {"first_name": "Practice", "last_name": "Example"},
        }
        before = self.request("/api/patients")
        for roster in (
            [], [patient, patient],
            [patient, {**patient, "patient": "patient-" + "c" * 32, "insurance": {"member_id": "INCOMPLETE"}}],
        ):
            with self.subTest(roster=roster), self.assertRaises(HTTPError) as rejected:
                self.request("/api/appointments", {**values, "group": {"reference": "group-" + "c" * 32, "patients": roster}})
            with rejected.exception as response:
                self.assertEqual(response.code, 400)
        self.assertEqual(self.request("/api/patients"), before)
        self.assertEqual(self.request("/api/appointments"), [])

    def test_group_home_requires_a_host_patient_on_its_roster(self):
        values = {
            "group": {
                "reference": "group-" + "d" * 32,
                "patients": [{"patient": "demo-01"}, {"patient": "demo-02"}],
            },
            "location": "home-" + "d" * 32, "location_kind": "home",
            "mode": "in-person", "day": "2026-11-26", "time": "09:00",
            "new_location": {"living_arrangement": "with_caregiver", **ADDRESS},
        }
        with self.assertRaises(HTTPError) as rejected:
            self.request("/api/appointments", {**values, "home_patient": "demo-03"})
        with rejected.exception as response:
            self.assertEqual(response.code, 400)
        booked = self.request("/api/appointments", {**values, "home_patient": "demo-02"})
        self.assertEqual(booked["group_name"], "Group visit")
        self.assertEqual(booked["location_address"], ADDRESS)
        self.assertEqual(self.request("/api/patients/demo-01/homes"), [])
        self.assertEqual(self.request("/api/patients/demo-02/homes"), [{"reference": booked["location"], **ADDRESS}])

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
            {
                **values, "new_patient": {"first_name": "Practice", "last_name": "Example"},
                "location": "clinic-" + "e" * 32, "location_kind": "clinic",
                "new_location": {"name": "Bad NPI Clinic", **SITE, "npi": "invalid"},
            },
            {
                **values, "new_patient": {"first_name": "Practice", "last_name": "Example"},
                "location": "home-" + "f" * 32, "location_kind": "home",
                "new_location": {"living_arrangement": "", **ADDRESS},
            },
        ):
            with self.subTest(wrong=wrong), self.assertRaises(HTTPError) as rejected:
                self.request("/api/appointments", wrong)
            self.assertEqual(rejected.exception.code, 400)
            with rejected.exception:
                rejected.exception.read()
        self.assertEqual(self.request("/api/patients"), before)
        self.assertEqual(self.request("/api/appointments"), [])
        self.assertEqual([row["reference"] for row in self.request("/api/facilities")], ["clinic"])

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
