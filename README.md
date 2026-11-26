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
