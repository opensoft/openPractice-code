app_name = "open_practice"
app_title = "openPractice"
app_publisher = "Opensoft"
app_description = "Practice operations and billing"
app_license = "Apache-2.0"

fixtures = [
    {"dt": "Role", "filters": [["name", "in", [
        "OpenPractice Test Biller", "OpenPractice Billing Evidence", "OpenPractice Scheduler",
        "OpenPractice Practice Administrator",
    ]]]}
]
before_install = "open_practice.site.require_install_site"
after_install = "open_practice.frappe_session_desk.seed"
after_sync = "open_practice.frappe_session_desk.seed_workspace"
after_migrate = [
    "open_practice.frappe_session_desk.seed",
    "open_practice.frappe_session_desk.seed_workspace",
]

get_website_user_home_page = "open_practice.frappe_account_signup.signup_home_page"

web_include_js = ["/assets/open_practice/onboarding/login_prefill.js"]
