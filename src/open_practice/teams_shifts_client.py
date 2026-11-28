"""Microsoft Graph v1.0 transport for an explicitly selected Teams schedule."""

import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID


GRAPH_ORIGIN = "https://graph.microsoft.com"
_COLLECTIONS = {"shifts", "openShifts", "openShiftChangeRequests", "timeCards"}


class TeamsShiftsError(RuntimeError):
    """Microsoft Graph returned unavailable or unusable scheduling evidence."""


class TeamsShiftsHTTPError(TeamsShiftsError):
    def __init__(self, status):
        super().__init__(f"Microsoft Graph Shifts returned HTTP {status}.")
        self.status = status


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TeamsShiftsClient:
    """Send bearer credentials only to Graph or an explicit loopback fixture."""

    def __init__(self, team_id, token_provider, *, origin=GRAPH_ORIGIN, timeout=10):
        self.team_id = str(UUID(team_id))
        target = urlsplit(origin)
        loopback = (
            target.scheme == "http"
            and target.hostname in {"127.0.0.1", "::1", "localhost"}
            and not (target.username or target.password or target.path or target.query or target.fragment)
        )
        if origin != GRAPH_ORIGIN and not loopback:
            raise ValueError("Use the Microsoft Graph origin or a loopback test origin.")
        self._origin = origin
        self._schedule_path = f"/v1.0/teams/{self.team_id}/schedule"
        self._token_provider = token_provider
        self._timeout = timeout
        self._opener = build_opener(_NoRedirect)

    def _request(self, url, method="GET", values=None):
        token = self._token_provider()
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~+/=-]+", token):
            raise TeamsShiftsError("A Microsoft Graph bearer token is required.")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        data = None
        if values is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(values).encode("utf-8")
        try:
            with self._opener.open(Request(url, data=data, method=method, headers=headers),
                                   timeout=self._timeout) as response:
                result = json.load(response)
        except HTTPError as error:
            with error:
                raise TeamsShiftsHTTPError(error.code) from None
        except (URLError, OSError):
            raise TeamsShiftsError("Could not reach Microsoft Graph Shifts.") from None
        except (UnicodeError, ValueError):
            raise TeamsShiftsError("Microsoft Graph returned an unreadable response.") from None
        if not isinstance(result, dict):
            raise TeamsShiftsError("Microsoft Graph returned an unexpected response.")
        return result

    def rows(self, collection):
        if collection not in _COLLECTIONS:
            raise ValueError("Unknown Teams Shifts collection.")
        path = f"{self._schedule_path}/{collection}"
        url = self._origin + path
        visited = set()
        records = {}
        while url is not None:
            if not isinstance(url, str):
                raise TeamsShiftsError("Microsoft Graph returned an invalid continuation URL.")
            target = urlsplit(url)
            if (
                f"{target.scheme}://{target.netloc}" != self._origin
                or target.path != path
                or target.fragment
                or url in visited
            ):
                raise TeamsShiftsError("Microsoft Graph returned an invalid continuation URL.")
            visited.add(url)
            page = self._request(url)
            rows = page.get("value")
            if not isinstance(rows, list):
                raise TeamsShiftsError(f"Microsoft Graph returned an invalid {collection} collection.")
            for row in rows:
                key = row.get("id") if isinstance(row, dict) else None
                if not isinstance(key, str) or not key or key in records:
                    raise TeamsShiftsError(f"Microsoft Graph returned invalid or duplicate {collection} IDs.")
                records[key] = row
            url = page.get("@odata.nextLink")
        return records

    def create_open_shift(self, values):
        row = self._request(self._origin + self._schedule_path + "/openShifts", "POST", values)
        if not isinstance(row.get("id"), str) or not row["id"]:
            raise TeamsShiftsError("Microsoft Graph did not acknowledge the open shift.")
        return row["id"]
