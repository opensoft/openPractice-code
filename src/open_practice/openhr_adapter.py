"""Read the openHR scheduling projections over Frappe REST for AP-11."""

import json
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from open_practice.staffing_variance import ApprovedAttendance, RosterSnapshot, StaffingAssignment, TimeWindow


ROSTER = "OpenPractice Roster Revision"
ATTENDANCE = "OpenPractice Attendance Reconciliation"
REQUEST = "OpenPractice Coverage Request"
PAGE_SIZE = 50


class OpenHRError(RuntimeError):
    """openHR is unavailable or returned invalid scheduling evidence."""


class FrappeHTTPError(OpenHRError):
    def __init__(self, status):
        super().__init__(f"openHR REST returned HTTP {status}.")
        self.status = status


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class FrappeClient:
    """Call a single Frappe origin with the caller's API token."""

    def __init__(self, base_url, api_key, api_secret, *, timeout=10):
        url = urlsplit(base_url)
        secure = url.scheme == "https" or (
            url.scheme == "http" and url.hostname in ("127.0.0.1", "::1", "localhost")
        )
        if not secure or not url.netloc or url.username or url.password or url.query or url.fragment:
            raise ValueError("Use an HTTPS openHR URL or a local test URL.")
        if not api_key or not api_secret:
            raise ValueError("An openHR API key and secret are required.")
        self._url = base_url.rstrip("/")
        self._headers = {"Authorization": f"token {api_key}:{api_secret}", "Accept": "application/json"}
        self._timeout = timeout
        self._opener = build_opener(_NoRedirect)

    def request(self, path, *, method="GET", values=None, response_key="data"):
        if not path.startswith("/api/"):
            raise ValueError("Expected a Frappe API path.")
        headers = dict(self._headers)
        body = None
        if values is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(values).encode("utf-8")
        request = Request(self._url + path, data=body, method=method, headers=headers)
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                return json.load(response)[response_key]
        except HTTPError as error:
            with error:
                raise FrappeHTTPError(error.code) from None
        except (URLError, TimeoutError):
            raise OpenHRError("Could not reach openHR.") from None
        except (ValueError, KeyError, TypeError):
            raise OpenHRError("openHR returned an invalid REST response.") from None

    @staticmethod
    def _resource(doctype, name=None):
        path = "/api/resource/" + quote(doctype, safe="")
        return path if name is None else path + "/" + quote(name, safe="")

    def get(self, doctype, name):
        return self.request(self._resource(doctype, name))

    def create(self, doctype, values):
        return self.request(self._resource(doctype), method="POST", values=values)

    def names(self, doctype, filters):
        offset = 0
        while True:
            query = urlencode({
                "filters": json.dumps(filters),
                "fields": '["name"]',
                "order_by": "sequence_number asc, name asc",
                "limit_start": offset,
                "limit_page_length": PAGE_SIZE,
            })
            rows = self.request(self._resource(doctype) + "?" + query)
            if not isinstance(rows, list) or any(
                not isinstance(row, dict) or not isinstance(row.get("name"), str) for row in rows
            ):
                raise OpenHRError("openHR returned an invalid list of documents.")
            yield from (row["name"] for row in rows)
            if len(rows) < PAGE_SIZE:
                return
            offset += len(rows)


@dataclass(frozen=True)
class OpenShiftRequest:
    correlation_id: str
    location: str
    role: str
    qualification: str
    window: TimeWindow
    headcount: int

    def __post_init__(self):
        if not isinstance(self.correlation_id, str) or not re.fullmatch(
            r"[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}", self.correlation_id,
        ):
            raise ValueError("Use a random UUID hex value for the coverage request reference.")
        workforce_fields = (self.location, self.role, self.qualification)
        if any(not isinstance(value, str) or not value.strip() for value in workforce_fields):
            raise ValueError("A coverage request needs a site, role and qualification.")
        if type(self.headcount) is not int or self.headcount < 1:
            raise ValueError("A coverage request needs a positive headcount.")

    def _document(self):
        return {
            "correlation_id": self.correlation_id,
            "location_ref": self.location,
            "staff_role": self.role,
            "qualification": self.qualification,
            "starts_at_iso": self.window.starts_at.isoformat(),
            "ends_at_iso": self.window.ends_at.isoformat(),
            "headcount": self.headcount,
        }


@dataclass(frozen=True)
class OpenHRStaffing:
    published: RosterSnapshot
    revisions: tuple[RosterSnapshot, ...]
    attendance: ApprovedAttendance | None
    coverage_links: tuple[tuple[str, StaffingAssignment], ...]

    def coverage_status(self, request: OpenShiftRequest) -> str:
        confirmed = {
            assignment.staff_id for reference, assignment in self.coverage_links
            if reference == request.correlation_id
            and (assignment.location, assignment.role, assignment.qualification)
            == (request.location, request.role, request.qualification)
            and assignment.window.starts_at <= request.window.starts_at
            and assignment.window.ends_at >= request.window.ends_at
        }
        return "confirmed" if len(confirmed) >= request.headcount else "pending"


def _json_array(document, field):
    items = json.loads(document[field])
    if not isinstance(items, list):
        raise ValueError(f"Expected an array for {field}.")
    return items


def _assignment(row):
    workforce_fields = (row["staff_id"], row["location"], row["role"], row["qualification"])
    if any(not isinstance(value, str) or not value.strip() for value in workforce_fields):
        raise ValueError("An assignment needs a staff reference, site, role and qualification.")
    return StaffingAssignment(
        *workforce_fields,
        TimeWindow(datetime.fromisoformat(row["starts_at"]), datetime.fromisoformat(row["ends_at"])),
    )


class OpenHRAdapter:
    """Translate submitted HR documents into the existing AP-11 records."""

    def __init__(self, client: FrappeClient):
        self._client = client

    def post_open_shift(self, request: OpenShiftRequest) -> None:
        """Send workforce requirements only. A successful post leaves coverage pending."""
        values = request._document()
        try:
            document = self._client.get(REQUEST, request.correlation_id)
        except FrappeHTTPError as error:
            if error.status != 404:
                raise
            try:
                document = self._client.create(REQUEST, values)
            except FrappeHTTPError as problem:
                if problem.status not in (409, 417, 500):
                    raise
                # Frappe can report duplicate names when two callers retry concurrently.
                try:
                    document = self._client.get(REQUEST, request.correlation_id)
                except FrappeHTTPError as readback_error:
                    if readback_error.status == 404:
                        raise problem from None
                    raise
        if not isinstance(document, dict):
            raise OpenHRError("openHR returned an invalid coverage request.")
        if document.get("name") != request.correlation_id or any(document.get(k) != v for k, v in values.items()):
            raise OpenHRError("The coverage request reference is already bound to different requirements.")

    def _submitted(self, doctype, scope_field, scope):
        filters = [[scope_field, "=", scope], ["docstatus", "=", 1]]
        documents = [self._client.get(doctype, name) for name in self._client.names(doctype, filters)]
        names = set()
        for position, doc in enumerate(documents, start=1):
            if (
                doc["docstatus"] != 1 or doc[scope_field] != scope
                or doc["sequence_number"] != position or doc["name"] in names
            ):
                raise ValueError("The submitted evidence must form a continuous sequence for the requested scope.")
            names.add(doc["name"])
        return documents

    def get_staffing(self, roster_series: str) -> OpenHRStaffing:
        """Read one roster series and its latest reconciliation for the current revision."""
        try:
            documents = self._submitted(ROSTER, "roster_series", roster_series)
            if not documents:
                raise OpenHRError("openHR has no published roster for this series.")
            snapshots = []
            for doc in documents:
                rows = _json_array(doc, "assignments_json")
                assignments = tuple(map(_assignment, rows))
                snapshots.append(RosterSnapshot(
                    doc["name"], datetime.fromisoformat(doc["recorded_at_iso"]), doc["modified_by"], doc["reason"], assignments,
                ))
            if snapshots != sorted(snapshots, key=lambda snapshot: snapshot.recorded_at):
                raise ValueError("Roster timestamps must be in revision order.")
            links = ()
            if len(snapshots) > 1:
                links = tuple(
                    (row["coverage_request"], assignment)
                    for row, assignment in zip(rows, assignments, strict=True)
                    if row.get("coverage_request")
                )
            records = self._submitted(ATTENDANCE, "roster_revision", snapshots[-1].reference)
            attendance = None
            if records:
                record = records[-1]
                exceptions = _json_array(record, "exceptions_json")
                if any(not isinstance(reference, str) or not reference for reference in exceptions):
                    raise ValueError("Attendance exceptions must be nonempty references.")
                attendance = ApprovedAttendance(
                    record["roster_revision"],
                    tuple(map(_assignment, _json_array(record, "worked_intervals_json"))),
                    tuple(exceptions),
                )
            return OpenHRStaffing(snapshots[0], tuple(snapshots[1:]), attendance, links)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise OpenHRError("openHR returned invalid roster or approved attendance evidence.") from None
