app_name = "open_practice"
app_title = "openPractice"
app_publisher = "Opensoft"
app_description = "Colorado Medicare Part B test workflow"
app_license = "Apache-2.0"

fixtures = [{"dt": "Role", "filters": [["name", "=", "OpenPractice Test Biller"]]}]
before_install = "open_practice.site.require_test_site"
