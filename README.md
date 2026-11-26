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
   subscriber against the preloaded synthetic administrative roster. Confirm
   primary Medicare Part B coverage and add the confirmed service charges.
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
The mock and preloaded roster use synthetic data.

To compare field mappings, export the same test cases from PC-ACE and compare the
two files with `python -m src.claimlab.x837p diff pcace.dat openpractice.837`.
Expect different transmission dates, times and control numbers between runs.

Run the Frappe lifecycle test on the disposable site.

```sh
bench --site test.localhost set-config --parse allow_tests 1
bench --site test.localhost run-tests --doctype 'OpenPractice Claim Batch'
```
