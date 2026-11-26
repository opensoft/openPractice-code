const get = (id) => document.getElementById(id);
const locations = { clinic: "Clinic", alf: "Assisted living facility" };
const modes = { "in-person": "In person", telemedicine: "Telemedicine" };
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
const dialog = get("booking-dialog");
const form = get("booking-form");
const viewDay = get("view-day");
let appointments = [];
let selectedId = null;

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
  return `Demo patient ${patient.slice(-2)}`;
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
      <span><strong>${patientLabel(row.patient)}</strong>
        <small>${locations[row.location]} · ${modes[row.mode]}</small>
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
  session.innerHTML = `
    <h2>${patientLabel(appointment.patient)}</h2>
    <p class="session-description">${displaySlot(appointment.time)} · ${locations[appointment.location]} · ${modes[appointment.mode]}</p>
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
  appointments = await api("/api/appointments");
  render();
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

get("new-appointment").addEventListener("click", () => {
  form.reset();
  form.elements.day.value = viewDay.value;
  showError(get("booking-error"), "");
  dialog.showModal();
});
get("cancel-booking").addEventListener("click", () => dialog.close());
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = form.querySelector('[type="submit"]');
  submit.disabled = true;
  showError(get("booking-error"), "");
  try {
    const booked = await api("/api/appointments", Object.fromEntries(new FormData(form)));
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
