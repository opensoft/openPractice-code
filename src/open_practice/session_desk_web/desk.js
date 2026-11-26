const get = (id) => document.getElementById(id);
const modes = { "in-person": "In person", telemedicine: "Telemedicine" };
const contractLabels = { unknown: "Unknown", no_contract: "No contract", contracted: "Contracted" };
const stages = ["scheduled", "available", "ready", "started", "ended", "wrapped"];
const mountain = { timeZone: "America/Denver" };
const dayFormat = new Intl.DateTimeFormat("en-US", {
  weekday: "long", month: "long", day: "numeric", year: "numeric", timeZone: "UTC",
});
const eventFormat = new Intl.DateTimeFormat("en-US", {
  month: "short", day: "numeric", hour: "numeric", minute: "2-digit", ...mountain,
});
const slotFormat = new Intl.DateTimeFormat("en-US", {
  hour: "numeric", minute: "2-digit", timeZone: "UTC",
});
const birthDateFormat = new Intl.DateTimeFormat("en-US", {
  month: "short", day: "numeric", year: "numeric", timeZone: "UTC",
});
const dialog = get("booking-dialog");
const form = get("booking-form");
const viewDay = get("view-day");
const patientSelect = form.elements.patient;
const newPatientFields = get("new-patient-fields");
const facilityDialog = get("facility-dialog");
const facilityForm = get("facility-form");
const facilitySelect = facilityForm.elements.reference;
let appointments = [];
let patients = [];
let facilities = [];
let selectedId = null;
let newPatientReference;

function stageLabel(stage, mode) {
  return {
    scheduled: "Scheduled",
    available: mode === "telemedicine" ? "Session available" : "Arrival registered",
    ready: "Patient ready",
    started: "Session started",
    ended: "Session ended",
    wrapped: "Ready for billing",
  }[stage];
}

function actionFor(appointment) {
  const telemedicine = appointment.mode === "telemedicine";
  return {
    scheduled: telemedicine
      ? ["Mark session available", "The telemedicine session is available for setup."]
      : ["Register arrival", "Record when the patient arrives at the location."],
    available: ["Mark patient ready", telemedicine ? "Confirm the patient is set up for telemedicine." : "Confirm the patient is in the exam room."],
    ready: ["Start session", "Record when the clinician begins the session."],
    started: ["End session", "Record when the patient can leave the room or the telemedicine call ends."],
    ended: ["Wrap up session", "The patient can leave and admin can take over. Wrap up once the clinician has finished in the charting system."],
    wrapped: [null, "Chart completion was confirmed and the session is ready for billing."],
  }[appointment.stage];
}

function patientLabel(patient) {
  return `${patient.first_name} ${patient.last_name}`;
}

function patientIdentifiers(patient) {
  return [
    patient.date_of_birth && `Born ${birthDateFormat.format(new Date(`${patient.date_of_birth}T00:00:00Z`))}`,
    patient.chart_reference && `Chart ${patient.chart_reference}`,
  ].filter(Boolean).join(" · ");
}

function escapeHTML(value) {
  const entities = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  return value.replace(/[&<>"']/g, (character) => entities[character]);
}

function displaySlot(slot) {
  return slotFormat.format(new Date(`2000-01-01T${slot}:00Z`));
}

function showError(element, message) {
  element.textContent = message;
  element.hidden = !message;
}

async function api(path, values) {
  const options = values === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(values),
  };
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error);
  return result;
}

function render() {
  get("schedule-date").textContent = dayFormat.format(new Date(`${viewDay.value}T00:00:00Z`));
  const visible = appointments.filter((row) => row.day === viewDay.value);
  const selected = visible.find((row) => row.id === selectedId) || visible[0];
  selectedId = selected?.id ?? null;
  get("appointments").innerHTML = visible.length ? visible.map((row) => `
    <button type="button" class="appointment" data-appointment="${row.id}" aria-pressed="${row.id === selectedId}">
      <time datetime="${row.time}">${displaySlot(row.time)}</time>
      <span><strong>${escapeHTML(patientLabel(row.patient_details))}</strong>
        <small>${escapeHTML(row.facility_name)} · ${modes[row.mode]}</small>
        <small class="row-stage">${stageLabel(row.stage, row.mode)}</small>
      </span>
    </button>`).join("") : `<p class="empty">No appointments on this date.</p>`;
  renderSession(selected);
}

function renderSession(appointment) {
  const session = get("session");
  if (!appointment) {
    session.innerHTML = `<h2>Session details</h2><p class="session-description">Book an appointment to follow its progress here.</p>`;
    return;
  }
  const [action, hint] = actionFor(appointment);
  const needsConfirmation = appointment.stage === "ended";
  const current = stageLabel(appointment.stage, appointment.mode);
  const identifiers = patientIdentifiers(appointment.patient_details);
  session.innerHTML = `
    <h2>${escapeHTML(patientLabel(appointment.patient_details))}</h2>
    <p class="session-description">${displaySlot(appointment.time)} · ${escapeHTML(appointment.facility_name)} · ${modes[appointment.mode]}</p>
    ${appointment.contract_status ? `<p class="session-description">ALF pharmacy contract · ${contractLabels[appointment.contract_status]}</p>` : ""}
    ${identifiers ? `<p class="patient-details">${escapeHTML(identifiers)}</p>` : ""}
    <div class="session-action">
      <h3>${current}</h3>
      <p>${hint}</p>
      ${needsConfirmation ? `<label class="confirm"><input id="chart-complete" type="checkbox"> The clinician has finished charting for this session.</label>` : ""}
      ${action ? `<button type="button" class="primary" id="advance-session" ${needsConfirmation ? "disabled" : ""}>${action}</button>` : ""}
    </div>
    <ol class="steps" aria-label="Session progress">
      ${stages.map((stage) => {
        const event = appointment.events.find((entry) => entry.stage === stage);
        const label = stage === "wrapped" ? "Wrapped up · Ready for billing" : stageLabel(stage, appointment.mode);
        return `<li ${event ? 'class="done"' : ""} ${stage === appointment.stage ? 'aria-current="step"' : ""}>
          ${label}${event ? `<time datetime="${event.at}">${eventFormat.format(new Date(event.at))}</time>` : ""}
        </li>`;
      }).join("")}
    </ol>`;

  if (needsConfirmation) {
    get("chart-complete").addEventListener("change", (event) => {
      get("advance-session").disabled = !event.target.checked;
    });
  }
  if (action) get("advance-session").addEventListener("click", () => advance(appointment));
}

async function refresh() {
  [appointments, patients, facilities] = await Promise.all([
    api("/api/appointments"), api("/api/patients"), api("/api/facilities"),
  ]);
  render();
  get("new-appointment").disabled = false;
  get("manage-facilities").disabled = false;
}

async function advance(appointment) {
  const button = get("advance-session");
  button.disabled = true;
  showError(get("desk-error"), "");
  try {
    const updated = await api(`/api/appointments/${appointment.id}/advance`, {
      stage: appointment.stage,
      chart_complete: get("chart-complete")?.checked ?? false,
    });
    get("notice").textContent = stageLabel(updated.stage, updated.mode);
    await refresh();
    get("advance-session")?.focus();
  } catch (error) {
    showError(get("desk-error"), error.message);
    try { await refresh(); } catch { button.disabled = false; }
  }
}

function updatePatientFields() {
  const isNewPatient = patientSelect.value === "new";
  newPatientFields.hidden = !isNewPatient;
  newPatientFields.disabled = !isNewPatient;
}

get("new-appointment").addEventListener("click", () => {
  form.reset();
  patientSelect.replaceChildren(
    new Option("Choose a patient", ""),
    new Option("Add new patient", "new"),
    ...patients.map((patient) => {
      const identifiers = patientIdentifiers(patient);
      return new Option(identifiers ? `${patientLabel(patient)} · ${identifiers}` : patientLabel(patient), patient.reference);
    }),
  );
  form.elements.location.replaceChildren(...facilities.map((facility) => new Option(facility.name, facility.reference)));
  newPatientReference = `patient-${crypto.randomUUID().replaceAll("-", "")}`;
  updatePatientFields();
  form.elements.day.value = viewDay.value;
  showError(get("booking-error"), "");
  dialog.showModal();
});
patientSelect.addEventListener("change", updatePatientFields);
get("cancel-booking").addEventListener("click", () => dialog.close());
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = form.querySelector('[type="submit"]');
  submit.disabled = true;
  showError(get("booking-error"), "");
  try {
    const { first_name, last_name, date_of_birth, chart_reference, ...values } = Object.fromEntries(new FormData(form));
    if (values.patient === "new") {
      values.patient = newPatientReference;
      values.new_patient = { first_name, last_name, date_of_birth, chart_reference };
    }
    const booked = await api("/api/appointments", values);
    selectedId = booked.id;
    viewDay.value = booked.day;
    await refresh();
    dialog.close();
    get("notice").textContent = "Appointment booked.";
    get("advance-session")?.focus();
  } catch (error) {
    showError(get("booking-error"), error.message);
  } finally {
    submit.disabled = false;
  }
});

function updateFacilityFields() {
  const selected = facilities.find((facility) => facility.reference === facilitySelect.value);
  facilityForm.elements.name.value = selected?.name ?? "";
  facilityForm.elements.contract_status.value = selected?.contract_status ?? "unknown";
  showError(get("facility-error"), "");
}

get("manage-facilities").addEventListener("click", () => {
  facilitySelect.replaceChildren(
    new Option("Add an assisted living facility", ""),
    ...facilities.filter((facility) => facility.kind === "alf")
      .map((facility) => new Option(facility.name, facility.reference)),
  );
  updateFacilityFields();
  facilityDialog.showModal();
});
facilitySelect.addEventListener("change", updateFacilityFields);
get("cancel-facility").addEventListener("click", () => facilityDialog.close());
facilityForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = facilityForm.querySelector('[type="submit"]');
  submit.disabled = true;
  showError(get("facility-error"), "");
  try {
    const saved = await api("/api/facilities", Object.fromEntries(new FormData(facilityForm)));
    await refresh();
    facilityDialog.close();
    get("notice").textContent = `${saved.name} saved.`;
  } catch (error) {
    showError(get("facility-error"), error.message);
  } finally {
    submit.disabled = false;
  }
});

get("appointments").addEventListener("click", (event) => {
  const selection = event.target.closest("[data-appointment]");
  if (!selection) return;
  selectedId = Number(selection.dataset.appointment);
  render();
  get("appointments").querySelector('[aria-pressed="true"]').focus();
});
const now = new Intl.DateTimeFormat("en-US", {
  year: "numeric", month: "2-digit", day: "2-digit", ...mountain,
}).formatToParts(new Date());
const today = Object.fromEntries(now.map(({ type, value }) => [type, value]));
viewDay.value = `${today.year}-${today.month}-${today.day}`;
viewDay.addEventListener("change", () => {
  if (!viewDay.value) viewDay.value = `${today.year}-${today.month}-${today.day}`;
  render();
});
refresh().catch((error) => showError(get("desk-error"), error.message));
