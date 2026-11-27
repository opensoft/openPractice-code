"""Connector checks that do not need a running Frappe site."""

import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from open_practice.openhr_company import REQUEST_FIELD, CompanyConnector, CompanyUnavailable


REQUEST_ID = "bc7233537b4544e39a90db8bc5ccbc69"
COMPANY = "Synthetic Solo LLC"


class CompanyConnectorTest(unittest.TestCase):
    def setUp(self):
        self.connector = CompanyConnector("https://openhr.test.invalid", "test-key:test-secret")
        self.received = []

    def find(self, path, payload=None):
        self.received.append((path, payload))
        filters = parse_qs(urlsplit(path).query)["filters"][0]
        self.assertEqual(json.loads(filters), [[REQUEST_FIELD, "=", REQUEST_ID]])

    def test_retry_recovers_company_after_lost_response(self):
        remote = []
        submitted = Mock()

        def send(path, payload=None, *, timeout=None):
            if payload is not None:
                self.received.append((path, payload))
                submitted.assert_called_once()
                self.assertEqual(payload[REQUEST_FIELD], REQUEST_ID)
                remote.append({"name": payload["company_name"], "is_group": 0, REQUEST_FIELD: REQUEST_ID})
                raise CompanyUnavailable("Response lost")
            self.find(path)
            return remote

        self.connector._request = send
        with self.assertRaises(CompanyUnavailable):
            self.connector.create_or_recover(REQUEST_ID, COMPANY, submitted=False, mark_submitted=submitted)
        company = self.connector.create_or_recover(REQUEST_ID, COMPANY, submitted=True, mark_submitted=submitted)

        self.assertEqual(company, COMPANY)
        self.assertEqual(len(remote), 1)
        self.assertEqual(sum(payload is not None for _, payload in self.received), 1)

    @patch("open_practice.openhr_company.sleep")
    def test_overlapping_create_waits_for_the_original_company(self, pause):
        remote = {"name": COMPANY, "is_group": 0, REQUEST_FIELD: REQUEST_ID}
        self.connector._request = Mock(side_effect=[[], [], [remote]])
        mark_submitted = Mock()

        company = self.connector.create_or_recover(
            REQUEST_ID, COMPANY, submitted=True, mark_submitted=mark_submitted,
        )

        self.assertEqual(company, COMPANY)
        self.assertEqual(pause.call_count, 2)
        self.assertEqual(self.connector._request.call_count, 3)
        for request in self.connector._request.call_args_list:
            self.assertEqual(len(request.args), 1)
            self.find(request.args[0])
        mark_submitted.assert_not_called()

    @patch("open_practice.openhr_company.sleep")
    @patch("open_practice.openhr_company.monotonic", side_effect=[0, 0, 8])
    def test_an_unresolved_retry_does_not_issue_another_create(self, clock, pause):
        self.connector._request = Mock(return_value=[])
        mark_submitted = Mock()
        with self.assertRaises(CompanyUnavailable):
            self.connector.create_or_recover(REQUEST_ID, COMPANY, submitted=True, mark_submitted=mark_submitted)
        self.connector._request.assert_called_once()
        self.find(self.connector._request.call_args.args[0])
        mark_submitted.assert_not_called()

    def test_link_uses_the_exact_id_and_does_not_write_to_openhr(self):
        self.connector._request = Mock(return_value={"name": COMPANY, "is_group": 0})
        self.assertEqual(self.connector.link(COMPANY, REQUEST_ID), COMPANY)
        self.connector._request.assert_called_once_with("/Synthetic%20Solo%20LLC")
