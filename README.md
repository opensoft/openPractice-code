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
Automated projection from native HRMS scheduling and attendance is separate
from this adapter slice.

From this checkout in PowerShell, run the command below.

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest discover -s src/open_practice/tests -v
```

The live T-26 case is reported separately as
`test_t26_against_openhr_test_site` and is skipped when no URL is configured.
