"""HTTP boundary for the documented QuickBooks Time (TSheets) v1 API."""

import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


API_ORIGIN = "https://rest.tsheets.com"


class QuickBooksTimeError(RuntimeError):
    """The provider could not supply reliable evidence."""


class QuickBooksTimeRejected(QuickBooksTimeError):
    """QuickBooks Time rejected the only event in a create request."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class QuickBooksTimeClient:
    """Read the active account token from the application for each API request."""

    def __init__(self, token_provider, *, base_url=API_ORIGIN, timeout=10):
        origin = base_url.rstrip("/")
        url = urlsplit(origin)
        local = (
            url.scheme == "http" and url.hostname in ("127.0.0.1", "::1", "localhost")
            and url.username is None and url.password is None
            and not (url.path or url.query or url.fragment)
        )
        if origin != API_ORIGIN and not local:
            raise ValueError("Use the QuickBooks Time API origin or a loopback HTTP test server.")
        self._origin = origin
        self._token_provider = token_provider
        self._timeout = timeout
        self._opener = build_opener(_NoRedirect)

    def _request(self, resource, *, query=None, payload=None):
        try:
            token = self._token_provider()
        except Exception:
            raise QuickBooksTimeError("The QuickBooks Time token is unavailable.") from None
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~+/\-=]+", token):
            raise QuickBooksTimeError("The QuickBooks Time token is unavailable.")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        url = f"{self._origin}/api/v1/{resource}"
        if query:
            url += "?" + urlencode(query)
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
        request = Request(url, headers=headers, data=body)
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                data = json.load(response)
        except HTTPError as error:
            with error:
                raise QuickBooksTimeError(f"QuickBooks Time returned HTTP {error.code}.") from None
        except (URLError, TimeoutError, OSError):
            raise QuickBooksTimeError("Could not reach QuickBooks Time.") from None
        except (ValueError, TypeError):
            raise QuickBooksTimeError("QuickBooks Time returned invalid JSON.") from None
        if not isinstance(data, dict):
            raise QuickBooksTimeError("QuickBooks Time returned an invalid response.")
        return data

    @staticmethod
    def _rows(data, resource):
        try:
            rows = data["results"][resource]
            if rows == []:
                return {}
            if not isinstance(rows, dict):
                raise ValueError
            if any(type(row["id"]) is not int or row["id"] < 1 or str(row["id"]) != key
                   for key, row in rows.items()):
                raise ValueError
            return {row["id"]: row for row in rows.values()}
        except (KeyError, TypeError, ValueError):
            raise QuickBooksTimeError(f"QuickBooks Time returned an invalid {resource} result.") from None

    def rows(self, resource, **filters):
        records = {}
        for page in range(1, 1001):
            data = self._request(resource, query={**filters, "page": page, "limit": 100})
            rows = self._rows(data, resource)
            nested_more = data["results"].get("more")
            more = data.get("more", nested_more)
            if nested_more is not None and nested_more is not more:
                raise QuickBooksTimeError(f"QuickBooks Time returned inconsistent {resource} pagination.")
            if type(more) is not bool or records.keys() & rows.keys() or (more and not rows):
                raise QuickBooksTimeError(f"QuickBooks Time returned inconsistent {resource} pages.")
            records.update(rows)
            if not more:
                return records
        raise QuickBooksTimeError(f"QuickBooks Time {resource} exceeded the read limit.")

    def create_schedule_event(self, fields):
        data = self._request("schedule_events", payload={"data": [fields], "team_events": "base"})
        try:
            rows = data["results"]["schedule_events"]
            if not isinstance(rows, dict) or set(rows) != {"1"}:
                raise ValueError
            row = rows["1"]
            status = row["_status_code"]
            if type(status) is int and status in (403, 417):
                raise QuickBooksTimeRejected(f"QuickBooks Time rejected the draft event with status {status}.")
            if status not in (200, 201) or type(row["id"]) is not int or row["id"] < 1:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise QuickBooksTimeError("QuickBooks Time did not acknowledge the draft event.") from None
        return row
