# openPractice-code

The **code leg** of the `openpractice` project: the implementation and its
tests. The requirements it implements live in
[`opensoft/openPractice-spec`](https://github.com/opensoft/openPractice-spec).

**Clone the assembly root, not this repository.** This leg is mounted as a
submodule at `code/` inside
[`opensoft/openPractice`](https://github.com/opensoft/openPractice), which
is what pins the commit of this repository that the project currently is:

```sh
git clone --recurse-submodules https://github.com/opensoft/openPractice.git
cd openPractice
make bootstrap
```

Working here directly is fine — it is an ordinary repository with an ordinary
branch. What advancing this leg does NOT do is advance the project: that is a
commit in the assembly root moving the gitlink, `contracts/code-pin.yaml` and
any workflow `@<sha>` reference together.

Being the code leg confers no authority over the implementation. The split is
navigation; authority travels in grants, and a project that keeps spec and
code in one repository is reviewed identically.

Topic: `xf-project-openpractice`.

## openHR scheduling adapter

`OpenHRAdapter` uses Frappe REST to submit coverage requests and feed the
existing AP-11 variance engine. It reads three DocTypes from the HR site.

| DocType | Records used by openPractice |
| --- | --- |
| OpenPractice Roster Revision | Each submitted document is a full snapshot of one roster series, sequenced from 1. Sequence 1 is the frozen published roster. Later submissions are the revised roster. |
| OpenPractice Coverage Request | One request keyed by a random correlation ID. The payload contains only the location, role, qualification, exact time window and headcount. Reposting with the same ID and requirements is safe. |
| OpenPractice Attendance Reconciliation | Submitted reconciliations are sequenced from 1 for each roster revision. The latest reconciliation for the current revision supplies approved intervals and outstanding exception references. |

Roster and attendance interval rows carry `staff_id`, `location`, `role`,
`qualification`, `starts_at` and `ends_at`. Timestamps include the time
zone. In a submitted newer roster, set `coverage_request` on each assignment
that fills an open request. Preserve those links in later snapshots while the
assignments remain effective. Coverage stays pending until the linked assignments
in the current revised roster cover the requested role, qualification, headcount
and exact interval. Draft roster and attendance records are ignored.

### Run the provider conformance suite

From the assembly checkout in PowerShell, use both the code and spec legs for
this change. The suite reads the canonical schema from `CONTRACTS_DIR`.

```powershell
$env:CONTRACTS_DIR = Join-Path (Get-Location) 'spec/contracts'
$env:PYTHONPATH = Join-Path (Get-Location) 'code/src'
python -m pip install -r code/requirements-test.txt
python -m unittest discover -s code/src/open_practice/tests -v
```

This runs the canonical fixtures, AP-10 and AP-11 tests, and the loopback
openHR workflow. The adapter conformance assertions also run in both live
openHR cases when the corresponding test environment is configured.

### Run T-26

The loopback rehearsal runs with the regular Python tests. It exercises the
adapter's HTTP requests and the existing demand and variance engines. To run
against a dedicated openHR test site, configure `OPENHR_T26_URL` and API key
and secret pairs for two test users:

- `OPENHR_T26_SERVICE_KEY` and `OPENHR_T26_SERVICE_SECRET` for a user with
  **OpenPractice Scheduling Integration**.
- `OPENHR_T26_MANAGER_KEY` and `OPENHR_T26_MANAGER_SECRET` for a different
  user with **HR Manager**.

If the test site needs the three DocTypes, run `scripts/setup_openhr_t26.py`
once with `OPENHR_T26_SETUP_KEY` and `OPENHR_T26_SETUP_SECRET` for a test-site
administrator. Assign the test users their roles after setup. The fixture
writes uniquely scoped synthetic roster, coverage and attendance records. It
submits the HR projections directly using the test manager's permissions.
The native-record acceptance case below exercises the automatic projection.

The live T-26 case is reported separately as
`test_t26_against_openhr_test_site` and is skipped when no URL is configured.

### Connect native openHR scheduling

Install the `integrations/openhr_ext` companion app in the openHR bench after
provisioning the three scheduling DocTypes. Copy the app directory to
`apps/openhr_ext`, install it in the bench Python environment with
`./env/bin/python -m pip install --no-deps -e apps/openhr_ext`, and add
`openhr_ext` to `sites/apps.txt`. Run `bench --site <site> install-app openhr_ext`.

In openHR, an HR Manager creates an **OpenPractice Native Roster** for one
location and work date. Its worker mapping is keyed by native Employee ID.
Each entry supplies the openPractice `staff_id`, `role` and `qualification`.
Link the native Shift Assignments to this roster and submit them, then submit
the roster to publish its frozen baseline. Submitting, changing or canceling a
linked assignment updates the revised roster. A replacement links to the
coverage request so the adapter can verify that it fills the requested shift.

Submitted native Attendance supplies approved in and out times. The companion
app matches the employee, shift type and date to the applicable assignment.
When more than one assignment matches, select it explicitly on Attendance.
The adapter reads any unresolved assignments alongside the approved intervals.

For native T-26, set the conformance environment above and use the dedicated test-site credentials, including
the setup account for creating synthetic employee and scheduling records. Set
`OPENHR_T26_NATIVE=1` and run the case below with IANA time-zone data available
to Python.

```powershell
python -m unittest -v open_practice.tests.test_openhr_native.OpenHRNativeSiteAcceptanceTest
```

## QuickBooks Time scheduling adapter

`QuickBooksTimeAdapter` uses the documented QuickBooks Time (TSheets) v1
API. It reads published shifts, scoped workers and qualifications, timed
approved leave, and timecard evidence through the shared 1.0 contract.
It posts an unassigned draft for a coverage request and waits for the
provider's published assignment. Intuit is moving the classic Time
experience into QuickBooks Online. Confirm that the tenant can use the
documented Time v1 endpoints before connecting it. [QuickBooks Time API
reference](https://tsheetsteam.github.io/api_docs/) · [Intuit migration
notice](https://quickbooks.intuit.com/learn-support/en-us/help-article/time-tracking/classic-quickbooks-time-moving-quickbooks-online/L5tnv0Sms_US_en_US)

Configure one calendar, workforce job-code mapping and account time zone per
roster series. Supply the account's authorized workforce mapping by QuickBooks
user ID. Each entry maps to one staff reference, role, qualification, skill
level and a credential expiry date or an explicitly nonexpiring credential.
A scheduled assignment counts only when the mapped worker's qualification
matches the job code and is valid through the end of the shift.

Pass an account-scoped bearer-token supplier to `QuickBooksTimeClient`.
The application owns OAuth authorization, token renewal and storage. The
Time v1 authorization is separate from the QuickBooks accounting connection.
The client sends the bearer token only to the documented API origin or to the
loopback fixture when explicitly configured. [Time v1 OAuth
documentation](https://tsheetsteam.github.io/api_docs/#authentication)

Keep one durable local SQLite history file per account, calendar and roster
series. Capture the published baseline with `get_staffing(series)` before
processing day-of changes. This freezes the first successful observation.
Later observations append revisions, and restarts read the same history.
A repeated request with the same correlation ID is idempotent. A retry
after an ambiguous create looks for the corresponding provider event and
keeps the request unresolved when the provider has yet to return it. Only a
published, qualified assignment counts as confirmation. [Schedule events
reference](https://tsheetsteam.github.io/api_docs/#schedule-events)

`get_workers(window)`, `get_leave(window)`, and `get_clock_events(window)`
return canonical messages. Date-only leave lacks an exact window and returns
an unavailable-feed error when it affects the requested dates. Clock events
reflect the boundaries of regular timecards. For approved attendance,
the adapter requires closed, timed entries within the worker's
`approved_to` date. Manual duration entries and unfinished or unapproved
timecards remain unresolved in the reconciliation. [Time off request
entries](https://tsheetsteam.github.io/api_docs/#time-off-request-entries) ·
[Timesheet approval fields](https://tsheetsteam.github.io/api_docs/#the-timesheet-object)

After setting up the shared conformance environment above, run the synthetic
QuickBooks Time acceptance suite from the assembly checkout.

```powershell
python -m unittest -v open_practice.tests.test_quickbooks_time_adapter
```

The tests use a loopback HTTP fixture with synthetic workforce records and
exercise the same conformance checks as the openHR T-26 tests.

### Run live T-12

The live case requires a dedicated Intuit test account with QuickBooks Time
v1 API access and approvals enabled. Its token must belong to a manager or
administrator who can manage schedules and timecards and approve time.
Use three existing synthetic staff with time tracking enabled and no
deliverable notification contacts. [Time v1 authorization](https://tsheetsteam.github.io/api_docs/#authentication) ·
[Time approval API](https://tsheetsteam.github.io/api_docs/#approving-time)

Set `OPENPRACTICE_T12_TIME_TOKEN` from the test account's secure token source.
Set `OPENPRACTICE_T12_CONFIG` to a JSON file with non-secret test settings.

- `account` is the test account's `client_url` from Time v1.
- `calendar_id` and `jobcode_id` select the synthetic schedule calendar and the active regular workforce job code.
- `user_ids` lists the original RN, callout RN, and float RN IDs in that order.
- `date` is a completed local test date with an empty 09:00–09:30 calendar window.
- `time_zone` is the account's IANA time zone.

The runner verifies the tenant and checks that approval will apply only to
timecards from this run. It posts and reads the draft through the adapter,
publishes the float in Time, records two synthetic timecards, and verifies
the approved attendance. The provider records remain available for inspection.

```powershell
python -m unittest -v open_practice.tests.test_quickbooks_time_live
```

## QuickBooks Online sandbox TimeActivity smoke test

This Windows QA runner uses the QuickBooks Online Accounting sandbox API to
create one synthetic, eight-minute, nonbillable employee TimeActivity and
read it back by ID. It uses the existing record on subsequent runs against
the same sandbox and state directory. Live QuickBooks Time v1 T-12 has its
own runner and tenant authorization.

Use a dedicated QA vault, a synthetic QuickBooks Online sandbox company, and
a signed-in Azure CLI identity with access to read secrets and update the
refresh grant. In the vault, store these JSON objects as secret values.

- `openpractice-qbo-sandbox-app` with string fields `client_id` and `client_secret`.
- `openpractice-qbo-sandbox-grant` with string fields `realm_id` and `refresh_token`.

From the code checkout, provide the Python used to run Azure CLI if it lives
in a separate environment. The runner needs IANA time-zone data when using a
non-UTC zone. Use a private state directory outside the checkout and reuse it
for every rerun so the runner can find the original receipt.

```powershell
$stateDir = Join-Path $env:LOCALAPPDATA 'openPractice/quickbooks-online-sandbox'
python scripts/qbo_sandbox/timeactivity_smoke.py `
  --vault-name '<qa-vault-name>' --subscription '<qa-subscription>' `
  --azure-cli-python '<path-to-python-with-azure-cli>' `
  --state-dir $stateDir --time-zone 'America/La_Paz'
```

The runner obtains an access token in memory and saves a replacement refresh
token to the vault when Intuit returns one. Keep its receipt and connection lock
in the state directory. The receipt carries the synthetic request and activity ID
so a retry can resume after an interrupted create or verify an existing record.
