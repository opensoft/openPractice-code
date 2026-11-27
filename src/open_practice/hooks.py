app_name = "open_practice"
app_title = "openPractice"
app_publisher = "Opensoft"
app_description = "Practice operations and billing"
app_license = "Apache-2.0"

fixtures = [
    {"dt": "Role", "filters": [["name", "in", [
        "OpenPractice Test Biller", "OpenPractice Billing Evidence", "OpenPractice Scheduler",
    ]]]}
]
before_install = "open_practice.site.require_install_site"
after_install = "open_practice.frappe_session_desk.seed"
after_migrate = "open_practice.frappe_session_desk.seed"
