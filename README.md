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
2. Assign **OpenPractice Test Biller** to the biller. Create an **OpenPractice
   Visit**, confirm primary Medicare Part B coverage, enter office service
   lines and confirmed ICD-10-CM diagnosis codes, then submit the visit.
3. Add finalized visits to an **OpenPractice Claim Batch**. Submit to release
   the batch, then choose **Download test 837P**. The release fixes the claim
   content and its SHA-256 digest. Each file uses Colorado Part B receiver
   and payer ID `04112` and test flag `T`. Follow the enrollment instructions for the size
   and composition of the test batch.
4. Use the enrolled test account to upload the file in **Novitasphere →
   Claim Submission/ERA**. Record the transmission reference and results of the
   matching **999** and **277CA** on the released batch. `Accepted` requires
   acceptance at both levels.

[Novitas JH 837P companion guide](https://www.novitas-solutions.com/webcenter/content/conn/UCM_Repository/uuid/dDocName%3A00198701) · [Novitasphere upload instructions](https://www.novitas-solutions.com/webcenter/portal/MedicareJL/pagebyid?contentId=00027493)

To compare field mappings, export the same test cases from PC-ACE and compare the
two files with `python -m src.claimlab.x837p diff pcace.dat openpractice.837`.
Expect different transmission dates, times and control numbers between runs.

Run the Frappe lifecycle test on the disposable site.

```sh
bench --site test.localhost set-config --parse allow_tests 1
bench --site test.localhost run-tests --doctype 'OpenPractice Claim Batch'
```
