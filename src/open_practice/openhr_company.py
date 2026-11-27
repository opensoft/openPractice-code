"""OpenHR Company operations for the PracticeLab onboarding connector."""

import json
import re
from http.client import HTTPException
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


REQUEST_FIELD = "custom_openpractice_request_id"
COMPANY_API = "/api/resource/Company"
REQUEST_TIMEOUT = 8
RECONCILE_INTERVAL = 0.2


class CompanyUnavailable(Exception):
    """The connector could not verify the requested Company."""


class CompanyConflict(Exception):
    """The requested Company cannot be used for this practice."""


class NoRedirect(HTTPRedirectHandler):
    """Never forward a connector token to a redirected URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CompanyConnector:
    def __init__(self, url, token, *, allow_test_http=False, site_name=None, country="United States", currency="USD"):
        self.url = url.rstrip("/")
        parts = urlsplit(self.url)
        schemes = ("https", "http") if allow_test_http and parts.hostname == "127.0.0.1" else ("https",)
        if (parts.scheme not in schemes or not parts.hostname or parts.username or parts.password
                or parts.path or parts.query or parts.fragment or not token):
            raise CompanyUnavailable("The openHR connection is not configured.")
        if site_name and not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,138}", site_name):
            raise CompanyUnavailable("The openHR site name is invalid.")
        self.site_name = site_name
        self.site = site_name or self.url
        self.token = token
        self.country = country
        self.currency = currency
        self.opener = build_opener(NoRedirect())

    def _request(self, path, payload=None, *, timeout=REQUEST_TIMEOUT):
        request = Request(
            self.url + COMPANY_API + path,
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={
                "Authorization": "token " + self.token,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        if self.site_name:
            request.add_header("X-Frappe-Site-Name", self.site_name)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return json.load(response)["data"]
        except HTTPError as error:
            if error.code == 404 and payload is None and path.startswith("/"):
                raise CompanyConflict("The selected Company was not found.") from error
            raise CompanyUnavailable("The openHR request could not be verified.") from error
        except (URLError, OSError, HTTPException, ValueError, KeyError, TypeError) as error:
            raise CompanyUnavailable("The openHR request could not be verified.") from error

    def _find_request(self, request_id, *, timeout=REQUEST_TIMEOUT):
        query = urlencode({
            "filters": json.dumps([[REQUEST_FIELD, "=", request_id]]),
            "fields": json.dumps(["name", "is_group", REQUEST_FIELD]),
            "limit_page_length": 2,
        })
        rows = self._request("?" + query, timeout=timeout)
        if not isinstance(rows, list):
            raise CompanyUnavailable("Unexpected Company lookup result.")
        if not rows:
            return None
        if len(rows) != 1 or rows[0].get(REQUEST_FIELD) != request_id or rows[0].get("is_group") != 0:
            raise CompanyConflict("The Company request has conflicting results.")
        return rows[0]["name"]

    def create_or_recover(self, request_id, legal_entity_name, *, submitted, mark_submitted):
        if submitted:
            deadline = monotonic() + REQUEST_TIMEOUT
            while (remaining := deadline - monotonic()) > 0:
                company = self._find_request(request_id, timeout=remaining)
                if company:
                    return company
                sleep(min(RECONCILE_INTERVAL, remaining))
            raise CompanyUnavailable("The previous create is still unresolved.")
        company = self._find_request(request_id)
        if company:
            return company
        mark_submitted()
        self._request("", {
            "company_name": legal_entity_name,
            "abbr": request_id[:8].upper(),
            "country": self.country,
            "default_currency": self.currency,
            "is_group": 0,
            REQUEST_FIELD: request_id,
        })
        company = self._find_request(request_id)
        if not company:
            raise CompanyUnavailable("The newly created Company could not be verified.")
        return company

    def link(self, company_id, request_id):
        row = self._request("/" + quote(company_id, safe=""))
        if row.get("name") != company_id or row.get("is_group") != 0:
            raise CompanyConflict("Select an operating Company on this openHR site.")
        if row.get(REQUEST_FIELD) not in (None, "", request_id):
            raise CompanyConflict("This Company already belongs to a different onboarding request.")
        return company_id
