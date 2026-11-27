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
