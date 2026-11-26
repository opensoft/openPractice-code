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

## Two-phase MVP

**Phase 1** is a local, single-operator rehearsal. Enter a synthetic patient's
first and last name, with date of birth and a chart reference when useful, or
pick a saved patient. Book an in-person or telemedicine visit at the clinic or
a registered assisted living facility. One screen records arrival or session availability, patient
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

Open `http://127.0.0.1:17824/` on the same machine. Use **Facilities** to register
an assisted living facility and record whether the pharmacy has a contract with
it. The facility then appears as a location when booking an appointment.

**Phase 2** adds the live chart completion and billing handoff, shared access
for staff, and the agreed administrative and clinical boundaries for telemedicine
preparation. The existing Colorado claim and staffing drafts can attach after
the basic appointment-to-wrap-up workflow is validated.
