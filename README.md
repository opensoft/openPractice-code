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
