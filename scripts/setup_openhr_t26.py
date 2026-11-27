"""Install the three scheduling DocTypes on a dedicated openHR test site."""

import os

from open_practice.openhr_adapter import ATTENDANCE, REQUEST, ROSTER, FrappeClient, FrappeHTTPError


SERVICE_ROLE = "OpenPractice Scheduling Integration"
MANAGER_ROLE = "HR Manager"


def field(name, label, kind="Data", **options):
    return {"fieldname": name, "label": label, "fieldtype": kind, "reqd": 1, **options}


def doctype(name, fields, *, submittable, autoname):
    service = {"role": SERVICE_ROLE, "read": 1}
    manager = {"role": MANAGER_ROLE, "read": 1}
    if submittable:
        manager.update(create=1, write=1, submit=1)
    else:
        service["create"] = 1
    return {
        "name": name,
        "module": "HR",
        "custom": 1,
        "autoname": autoname,
        "is_submittable": int(submittable),
        "track_changes": 1,
        "fields": fields,
        "permissions": [service, manager],
    }


def ensure(client, resource, name, values):
    try:
        return client.get(resource, name)
    except FrappeHTTPError as error:
        if error.status != 404:
            raise
        return client.create(resource, values)


def main():
    client = FrappeClient(
        os.environ["OPENHR_T26_URL"], os.environ["OPENHR_T26_SETUP_KEY"], os.environ["OPENHR_T26_SETUP_SECRET"],
    )
    client.get("Module Def", "HR")
    ensure(client, "Role", SERVICE_ROLE, {"role_name": SERVICE_ROLE, "desk_access": 0})
    sequence = field("sequence_number", "Sequence number", "Int")
    definitions = (
        doctype(ROSTER, [
            field("roster_series", "Roster series"),
            sequence,
            field("recorded_at_iso", "Effective revision timestamp (ISO 8601)"),
            field("reason", "Reason"),
            field("assignments_json", "Staff assignments (JSON)", "Long Text"),
        ], submittable=True, autoname="hash"),
        doctype(ATTENDANCE, [
            field("roster_revision", "Roster revision", "Link", options=ROSTER),
            sequence,
            field("worked_intervals_json", "Approved worked intervals (JSON)", "Long Text"),
            field("exceptions_json", "Unresolved exceptions (JSON)", "Long Text"),
        ], submittable=True, autoname="hash"),
        doctype(REQUEST, [
            field("correlation_id", "Correlation ID", unique=1),
            field("location_ref", "Location"),
            field("staff_role", "Staff role"),
            field("qualification", "Qualification"),
            field("starts_at_iso", "Starts at (ISO 8601)"),
            field("ends_at_iso", "Ends at (ISO 8601)"),
            field("headcount", "Headcount", "Int"),
        ], submittable=False, autoname="field:correlation_id"),
    )
    for definition in definitions:
        name = definition["name"]
        document = ensure(client, "DocType", name, definition)
        expected = {item["fieldname"]: item["fieldtype"] for item in definition["fields"]}
        actual = {item["fieldname"]: item["fieldtype"] for item in document["fields"]}
        if (
            not expected.items() <= actual.items()
            or document["is_submittable"] != definition["is_submittable"]
            or document["autoname"] != definition["autoname"]
        ):
            raise RuntimeError(f"Existing DocType {name} has an incompatible schema.")
        print(f"Ready: {name}")


if __name__ == "__main__":
    main()
