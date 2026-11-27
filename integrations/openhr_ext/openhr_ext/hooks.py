app_name = "openhr_ext"
app_title = "OpenHR Ext"
app_publisher = "openPractice"
app_description = "Project native HR scheduling and attendance into openPractice staffing evidence"
app_email = "openpractice@users.noreply.github.com"
app_license = "Apache-2.0"

required_apps = ["hrms"]
after_install = "openhr_ext.install.install"

_projection = "openhr_ext.native_scheduling."
doc_events = {
    "OpenPractice Native Roster": {
        "validate": _projection + "validate_scope",
        "on_submit": _projection + "publish_roster",
    },
    "Shift Assignment": {
        "before_submit": _projection + "lock_shift",
        "before_cancel": _projection + "lock_shift",
        "before_update_after_submit": _projection + "lock_shift",
        "on_submit": _projection + "project_shift",
        "on_cancel": _projection + "project_shift",
        "on_update_after_submit": _projection + "project_shift",
    },
    "Attendance": {
        "before_submit": _projection + "lock_attendance",
        "before_cancel": _projection + "lock_attendance",
        "before_update_after_submit": _projection + "lock_attendance",
        "on_submit": _projection + "project_attendance",
        "on_cancel": _projection + "project_attendance",
        "on_update_after_submit": _projection + "project_attendance",
    },
    "OpenPractice Roster Revision": {"before_submit": _projection + "require_native_evidence"},
    "OpenPractice Attendance Reconciliation": {"before_submit": _projection + "require_native_evidence"},
}
