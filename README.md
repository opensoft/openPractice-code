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

## PracticeLab on Frappe

The authenticated Session Desk runs at `/practice` on a dedicated Frappe
v15 QA site. Enable it before installing the app.

```sh
bench --site practicelab.opensoft.dev set-config -p open_practice_practicelab 1
bench --site practicelab.opensoft.dev install-app open_practice
bench build --app open_practice
```

Installation seeds four synthetic patients and a demo clinic in the Frappe
site database. Add a Frappe Website User with the **OpenPractice Scheduler**
role for each tester. Site signup is disabled when the rehearsal is enabled.
Install and migration add the **openPractice** workspace and its **Session
Desk** shortcut on PracticeLab-enabled sites. Once Frappe site setup is
complete, Desk users with the **OpenPractice Scheduler** role can open the
shortcut from the sidebar. The scheduler records who booked the appointment
and who advanced each stage.
The host must serve Frappe with HTTPS and its normal session and CSRF handling.

Use the browser acceptance script in `deploy/practicelab/smoke.py` with
`PRACTICELAB_URL`, `PRACTICELAB_SMOKE_USER`, and
`PRACTICELAB_SMOKE_PASSWORD` in the process environment. Run it against a
fresh synthetic site. It signs in as the scheduler, books a visit, verifies
each transition through **Ready for billing**, and reads the saved state
after reloading the page.

### New Practice and openHR

Invite the first practice user as a Frappe Website User with the **OpenPractice
Practice Administrator** role. The user signs in at `/new-practice`. The first
release allows one practice setup per invited administrator.

Use a dedicated synthetic openHR site. Its `Company` DocType must have the
unique `custom_openpractice_request_id` Data field before enabling connection.
On that site, an operator can initialize the field once with
`env/bin/python -m open_practice.practicelab_onboarding_smoke install-request-field <openhr-site>`
from a bench where the `open_practice` Python package is available. Verify that
the target field has a database unique index. Give the PracticeLab service
identity read and create access to Company on that site. Set
`open_practice_openhr_url` to the target HTTPS origin and supply
`open_practice_openhr_token` from the deployment's secret store. Country and
currency default to United States and USD and can be set per site with
`open_practice_openhr_country` and `open_practice_openhr_currency`.

The PracticeLab GitHub test runs a separate openHR fork site and injects a lost
response after the target commits a synthetic creation. It reads Company back
over REST to confirm the link and deduplication. The fault proxy is specific to
the disposable acceptance environment.

For the hosted rehearsal, run the same browser script with
`PRACTICELAB_URL` pointing to the QA site and
`PRACTICELAB_OPENHR_TEST_URL` pointing directly to its native openHR target
for readback. Seed the dedicated synthetic users and group Company first.
Use an isolated QA fault proxy for the lost-response case, so it exercises
the same disconnect path without changing either production service.

## ClaimLab sandbox

Upload a synthetic 837P with `ISA15=T` to see simulated TA1, 999, and
277CA acknowledgments.

[Live 837P uploader](https://claimlab.opensoft.dev/) | [Live API examples](https://claimlab.opensoft.dev/#api) | [ClaimLab documentation](src/claimlab/README.md)

## Colorado Medicare Part B test workflow

Install `open_practice` on a disposable Frappe v15 site after setting its
`open_practice_novitas_test` site configuration value to `1`. Use synthetic
records for local tests and Novitas-approved test data for portal testing.

```sh
bench get-app --soft-link --skip-assets open_practice /path/to/this/checkout
bench --site test.localhost set-config --parse open_practice_novitas_test 1
bench --site test.localhost install-app open_practice
```

1. Set up **OpenPractice EDI Settings** as a System Manager. Use the enrolled
   JH submitter ID, provider identifiers and a range of unused
   interchange control numbers. The settings form requires the first number
   in that range. Keep portal sign-in credentials in Novitasphere.
2. Assign **OpenPractice Test Biller** to the biller. Start an **OpenPractice
   Billing Case** using a chart encounter reference from
   `OC-MOCK-000001` through `OC-MOCK-000025`. The mock openChart adapter reads
   the version 1.0 clinical-facts billing subset and resolves the linked
   subscriber against the site's synthetic administrative roster. Confirm
   test primary Medicare Part B coverage and add the confirmed service charges.
   Submit the billing case to approve it for release.
3. Add approved billing cases to an **OpenPractice Claim Batch** and submit it.
   Release freezes the redacted billing data and the restricted PHI elements
   separately, with an opaque reference between them. A biller with
   **OpenPractice Billing Evidence** can choose **Download test 837P**.
   The download reconstructs the file from the frozen pair, verifies its
   recorded SHA-256 digest, and records an audit event. Each file uses
   Colorado Part B receiver and payer ID `04112` and test flag `T`. Follow the
   enrollment instructions for the size and composition of the test batch.
4. Use the enrolled test account to upload the file in **Novitasphere →
   Claim Submission/ERA**. Record the transmission reference and results of the
   matching **999** and **277CA** on the released batch. The transmission
   reference is kept with that submission's release digest. `Accepted`
   requires acceptance at both levels.

[Novitas JH 837P companion guide](https://www.novitas-solutions.com/webcenter/content/conn/UCM_Repository/uuid/dDocName%3A00198701) · [Novitasphere upload instructions](https://www.novitas-solutions.com/webcenter/portal/MedicareJL/pagebyid?contentId=00027493)

The charting system owns the visit, clinical intake and signed diagnosis.
openChart is the preferred integration and the example in this test slice.
Other charting systems can supply billing facts through an adapter. The
billing case stores a chart reference, a billing subscriber reference, a
service date, confirmed diagnosis count, and a revision digest. The release
keeps patient-linked identifiers, dates and codes in the restricted snapshot.
The frozen format preserves element order, separators and encoding so exporter
changes affect future releases while the existing file stays reproducible.

### Meds Rx flu administration rehearsal

Use `OC-MOCK-FLU-000001` through `OC-MOCK-FLU-000025` for synthetic flu-shot
visits. The mock chart supplies a finalized `Z23` diagnosis. Add one `G0008`
administration line with one unit and diagnosis pointer `1`. Use a demo charge
for local testing. This scenario assumes another entity supplied the vaccine.

Select `01` for the pharmacy POS rehearsal or `60` for the mass immunization
POS rehearsal. The selected code is written to `CLM05-1`. Use the POS, billing
route and rendering provider requirements confirmed for Meds Rx before any
Novitas submission. Rehearsal files use demonstration enrollment values and
synthetic subscribers. [CMS preventive vaccine billing guidance](https://www.cms.gov/regulations-and-guidance/guidance/manuals/downloads/clm104c18.pdf)

For a clinic at an assisted living facility, enter the facility name and
physical address in the billing case's **Service facility** section. Include
city, state and ZIP+4, with a second address line as needed. Enter the facility's
NPI if the test route calls for it. The 837P writes those values to Loop 2310C
and keeps the Meds Rx billing address in Loop 2010AA. [Novitas JH 837P companion guide](https://www.novitas-solutions.com/webcenter/content/conn/UCM_Repository/uuid/dDocName%3A00198701)

### Synthetic subscribers for the portal test

Ask the enrolled submitter to contact the [Novitas JH EDI Help Desk](https://www.novitas-solutions.com/webcenter/portal/MedicareJH/pagebyid?contentId=00004399)
for the Colorado Part B (`04112`) Novitasphere 837P test. Request assigned
synthetic subscribers or written approval to use an operator-supplied synthetic
roster. Ask which subscriber demographics, dates of service and service mix to
use, and whether the route supports both the 999 and 277CA for those cases.
Keep the EDI case or approval reference. Use only synthetic data confirmed for
the route. A valid MBI format alone cannot establish approval.

Configure a fresh disposable Frappe site before approving any cases. Install
the approved UTF-8 `subscriber-roster.csv` as
`sites/<site>/private/openpractice-novitas-synthetic-roster.csv`, readable by
the Frappe service user. The CSV has these columns.

```text
subscriber_ref,patient_first_name,patient_last_name,date_of_birth,administrative_sex,address,city,state,postal_code,member_id
```

Map the approved test subscribers to the mock links `BILLING-TEST-000001`
through `BILLING-TEST-000025` for the cases you plan to approve. Use ISO dates,
the authorized test demographics and the assigned or approved synthetic MBIs
without hyphens. Use the associated `OC-MOCK-…` encounter references to obtain
the synthetic dates and diagnoses. If the approved test instructions require
different dates, diagnoses or services, align the fixture and billing lines
with those instructions before releasing the batch.

```sh
bench --site test.localhost set-config open_practice_novitas_roster_approval YOUR-EDI-CASE-REFERENCE
bench --site test.localhost set-config open_practice_novitas_roster_mode approved-synthetic
```

The default `rehearsal` mode uses the built-in `SYNTH…` member IDs for local
tests. In `approved-synthetic` mode, case approval requires a matching row
and batch release requires the roster digest recorded at approval. Keep the
roster fixed through release. The frozen batch records the approval reference
and digest. Reconstruction reads the frozen records even if the roster is
later removed or replaced.

To compare field mappings, export the same test cases from PC-ACE and compare the
two files with `python -m src.claimlab.x837p diff pcace.dat openpractice.837`.
Expect different transmission dates, times and control numbers between runs.

Run the Frappe lifecycle test on the disposable site.

```sh
bench --site test.localhost set-config --parse allow_tests 1
bench --site test.localhost run-tests --doctype 'OpenPractice Claim Batch'
```
## Two-phase MVP

**Phase 1** is a local, single-operator rehearsal. Enter a synthetic patient's
first and last name, with date of birth and a chart reference when useful, or
pick a saved patient. Book an in-person or telemedicine visit at an office or
clinic, a facility, or a patient's home. For a group visit, add patients to one
appointment and expand its roster to view patient details and enter primary
insurance. The schedule uses the group label and tracks session progress for
the whole group. One screen records arrival or session availability, patient
readiness, session start, session end and wrap-up. Session end signals the
room or call can be released and administrative handoff can begin. Wrap-up
requires explicit confirmation that the clinician finished charting and marks
the session ready for billing. The chart stays in the charting system.

The schedule uses Mountain Time. The session desk uses the Python standard
library and stores bookings, progress timestamps and facility records in a local
SQLite file. From this worktree in PowerShell, run the command below.

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m open_practice.session_desk --database (Join-Path $env:LOCALAPPDATA 'openPractice\session-desk.sqlite3')
```

Open `http://127.0.0.1:17824/` on the same machine. When booking, select a location
type, then choose a saved location or add one. Adding an office or facility
opens **Facilities**. Entering the 10-digit NPI looks up an organization in
the CMS NPI Registry and suggests its name, primary practice address, and
facility type when the registry has a recognized classification. Review or
edit the details before saving. Saving returns to the appointment with the
new facility selected. For a patient's home, record the address and living
arrangement in the appointment form. Saved homes appear for their associated
patient.

Open **Facilities** to search by name, NPI or address and filter by type or
status. Select a facility to view its information, edit it, or archive it.
Archived facilities stay in appointment history and can be restored from the
directory.

**Phase 2** adds the live chart completion and billing handoff, shared access
for staff, and the agreed administrative and clinical boundaries for telemedicine
preparation. The existing Colorado claim and staffing drafts can attach after
the basic appointment-to-wrap-up workflow is validated.
