const get = (id) => document.getElementById(id);
const modes = { "in-person": "In person", telemedicine: "Telemedicine" };
const contractLabels = { unknown: "Unknown", no_contract: "No contract", contracted: "Contracted" };
const livingLabels = { alone: "Lives alone", with_caregiver: "Lives with a caregiver" };
const addressFields = ["street_address", "address_line_2", "city", "region", "postal_code"];
const siteDetails = ["legal_name", "npi", ...addressFields];
const contractPharmacyFields = ["contract_pharmacy_name", "contract_pharmacy_npi"];
const patientFields = ["first_name", "last_name", "date_of_birth", "chart_reference"];
const insuranceFields = {
  payer_name: "Payer", member_id: "Member ID", plan_name: "Plan",
  payer_id: "Payer ID", group_number: "Group number",
};
const addLocationLabels = {
  clinic: "Add new office / clinic", alf: "Add new facility", home: "Add new home",
};
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
const appointmentType = form.elements.appointment_type;
const groupPatientSelect = get("group-patient-select");
const groupRosterDraft = get("group-roster-draft");
const groupHomeSelect = form.elements.home_patient;
const addGroupPatient = get("add-group-patient");
const newPatientFields = get("new-patient-fields");
const locationType = form.elements.location_kind;
const locationSelect = form.elements.location;
const newLocationFields = get("new-location-fields");
const facilityDialog = get("facility-dialog");
const facilityForm = get("facility-form");
const facilityType = facilityForm.elements.facility_type;
const hospitalSetting = facilityForm.elements.pos_code;
const facilityNpi = facilityForm.elements.npi;
const npiStatus = get("facility-npi-status");
const applyRegistryDetails = get("apply-registry-details");
const facilityTypeLabels = {};
let hospitalSettingLabels = {};
const facilitySearch = get("facility-search");
const facilityTypeFilter = get("facility-filter-type");
const facilityStatusFilter = get("facility-filter-status");
const facilityList = get("facility-list");
const insuranceDialog = get("insurance-dialog");
const insuranceForm = get("insurance-form");
let appointments = [];
let patients = [];
let facilities = [];
let selectedId = null;
let newPatientReference;
let groupReference;
let groupMembers = [];
let editInsuranceFor;
let newLocationReference;
let locationRequest = 0;
let lastBookingLocation = "";
let facilityHandoff = null;
let selectedFacilityReference = null;
let facilityBusy = false;
let npiLookupTimer;
let npiLookupController;
let matchedFacilityFields = null;
const npiAutofilled = new Map();

for (const group of ["site", "address", "contract"]) {
  get(`facility-${group}-fields`).append(get(`${group}-fields-template`).content.cloneNode(true));
}
const contractStatus = facilityForm.elements.contract_status;
get("booking-address-fields").append(get("address-fields-template").content.cloneNode(true));
for (const target of ["booking-insurance-fields", "insurance-edit-fields"]) {
  get(target).append(get("insurance-fields-template").content.cloneNode(true));
}
insuranceForm.elements.payer_name.required = true;
insuranceForm.elements.member_id.required = true;

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
  const inHome = appointment.location_kind === "home";
  return {
    scheduled: telemedicine
      ? ["Mark session available", "The telemedicine session is available for setup."]
      : ["Register arrival", inHome ? "Record when staff reach the patient's home." : "Record when the patient arrives at the location."],
    available: ["Mark patient ready", telemedicine ? "Confirm the patient is set up for telemedicine." : "Confirm the patient is ready for the visit."],
    ready: ["Start session", "Record when the clinician begins the session."],
    started: ["End session", "Record when the patient can leave the room or the telemedicine call ends."],
    ended: ["Wrap up session", "The patient can leave and admin can take over. Wrap up once the clinician has finished in the charting system."],
    wrapped: [null, "Chart completion was confirmed and the session is ready for billing."],
  }[appointment.stage];
}

function patientLabel(patient) {
  return `${patient.first_name} ${patient.last_name}`;
}

function appointmentLabel(appointment) {
  const name = appointment.group_name;
  if (!appointment.group_reference) return patientLabel(appointment.patient_details);
  return name === "Group visit" ? name : `Group visit · ${name}`;
}

function patientIdentifiers(patient) {
  return [
    patient.date_of_birth && `Born ${birthDateFormat.format(new Date(`${patient.date_of_birth}T00:00:00Z`))}`,
    patient.chart_reference && `Chart ${patient.chart_reference}`,
  ].filter(Boolean).join(" · ");
}

function locationAddress(location) {
  const city = [location.city, [location.region, location.postal_code].filter(Boolean).join(" ")]
    .filter(Boolean).join(", ");
  return [location.street_address, location.address_line_2, city].filter(Boolean).join(", ");
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

async function api(path, values, signal) {
  const options = values === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(values),
  };
  const response = await fetch(path, { ...options, signal });
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
      <span><strong>${escapeHTML(appointmentLabel(row))}</strong>
        <small>${escapeHTML(row.facility_name)} · ${modes[row.mode]}</small>
        <small class="row-stage">${stageLabel(row.stage, row.mode)}</small>
      </span>
    </button>`).join("") : `<p class="empty">No appointments on this date.</p>`;
  renderSession(selected);
}

function renderSession(appointment) {
  const session = get("session");
  const previousRoster = get("patient-roster");
  if (!appointment) {
    session.innerHTML = `<h2>Session details</h2><p class="session-description">Book an appointment to follow its progress here.</p>`;
    return;
  }
  const [action, hint] = actionFor(appointment);
  const needsConfirmation = appointment.stage === "ended";
  const current = stageLabel(appointment.stage, appointment.mode);
  const identifiers = appointment.patient_details ? patientIdentifiers(appointment.patient_details) : "";
  const address = locationAddress(appointment.location_address);
  const locationClassification = [
    facilityTypeLabels[appointment.facility_type],
    appointment.pos_code && `Location POS ${appointment.pos_code}`,
  ].filter(Boolean).join(" · ");
  const pharmacyContract = [
    contractLabels[appointment.contract_status],
    appointment.contract_pharmacy_name,
    appointment.contract_pharmacy_npi && `NPI ${appointment.contract_pharmacy_npi}`,
  ].filter(Boolean).join(" · ");
  const rosterLabel = appointment.group_reference ? `Patients in this visit (${appointment.member_count})` : "Patient information";
  const keepRosterOpen = previousRoster?.open && Number(previousRoster.dataset.appointment) === appointment.id;
  session.innerHTML = `
    <h2>${escapeHTML(appointmentLabel(appointment))}</h2>
    <p class="session-description">${displaySlot(appointment.time)} · ${escapeHTML(appointment.facility_name)} · ${modes[appointment.mode]}</p>
    ${locationClassification ? `<p class="session-description">${escapeHTML(locationClassification)}</p>` : ""}
    ${address ? `<p class="session-description">${escapeHTML(address)}</p>` : ""}
    ${appointment.contract_status ? `<p class="session-description">Facility pharmacy contract · ${escapeHTML(pharmacyContract)}</p>` : ""}
    ${appointment.living_arrangement ? `<p class="session-description">${livingLabels[appointment.living_arrangement]}</p>` : ""}
    ${identifiers ? `<p class="patient-details">${escapeHTML(identifiers)}</p>` : ""}
    <details id="patient-roster" class="patient-roster" data-appointment="${appointment.id}" ${keepRosterOpen ? "open" : ""}>
      <summary>${rosterLabel}</summary>
      <div id="patient-roster-content" aria-live="polite"></div>
    </details>
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

  const roster = get("patient-roster");
  const rosterContent = get("patient-roster-content");
  roster.addEventListener("toggle", () => {
    if (roster.open && !rosterContent.dataset.state) loadPatients(appointment.id, rosterContent);
  });
  if (keepRosterOpen) loadPatients(appointment.id, rosterContent);
  if (needsConfirmation) {
    get("chart-complete").addEventListener("change", (event) => {
      get("advance-session").disabled = !event.target.checked;
    });
  }
  if (action) get("advance-session").addEventListener("click", () => advance(appointment));
}

async function loadPatients(appointmentId, content) {
  content.dataset.state = "loading";
  content.textContent = "Loading patient information…";
  try {
    const roster = await api(`/api/appointments/${appointmentId}/patients`);
    content.innerHTML = roster.map((patient) => {
      const identifiers = patientIdentifiers(patient);
      const info = patient.insurance
        ? `<dl class="insurance-values">${Object.entries(insuranceFields)
          .filter(([key]) => patient.insurance[key])
          .map(([key, label]) => `<dt>${label}</dt><dd>${escapeHTML(patient.insurance[key])}</dd>`).join("")}</dl>`
        : '<p class="patient-details">Primary insurance has not been entered.</p>';
      return `<details><summary>${escapeHTML(patientLabel(patient))}</summary>
        ${identifiers ? `<p class="patient-details">${escapeHTML(identifiers)}</p>` : ""}
        ${info}<button type="button" data-insurance="${escapeHTML(patient.reference)}">${patient.insurance ? "Edit insurance" : "Add insurance"}</button></details>`;
    }).join("");
    content.querySelectorAll("[data-insurance]").forEach((button, index) => {
      const patient = roster[index];
      button.addEventListener("click", () => {
        editInsuranceFor = { patient, appointmentId, content };
        get("insurance-patient").textContent = patientLabel(patient);
        for (const key of Object.keys(insuranceFields)) {
          insuranceForm.elements[key].value = patient.insurance?.[key] ?? "";
        }
        showError(get("insurance-error"), "");
        insuranceDialog.showModal();
      });
    });
    content.dataset.state = "loaded";
  } catch (error) {
    content.textContent = error.message;
    delete content.dataset.state;
  }
}

get("cancel-insurance").addEventListener("click", () => insuranceDialog.close());
insuranceForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const { patient, appointmentId, content } = editInsuranceFor;
  const submit = insuranceForm.querySelector('[type="submit"]');
  submit.disabled = true;
  showError(get("insurance-error"), "");
  try {
    await api(`/api/patients/${encodeURIComponent(patient.reference)}/insurance`, Object.fromEntries(new FormData(insuranceForm)));
    insuranceDialog.close();
    await loadPatients(appointmentId, content);
    const button = [...content.querySelectorAll("[data-insurance]")].find((item) => item.dataset.insurance === patient.reference);
    if (button) {
      button.closest("details").open = true;
      button.focus();
    }
    get("notice").textContent = "Primary insurance saved.";
  } catch (error) {
    showError(get("insurance-error"), error.message);
  } finally {
    submit.disabled = false;
  }
});

async function loadFacilityTypes() {
  const { types, hospital_settings } = await api("/api/facility-types");
  for (const { value, label, pos_code } of types) {
    facilityTypeLabels[value] = label;
    facilityType.add(new Option(pos_code ? `${label} · POS ${pos_code}` : label, value));
    facilityTypeFilter.add(new Option(label, value));
  }
  hospitalSettingLabels = hospital_settings;
  for (const [code, label] of Object.entries(hospital_settings)) {
    hospitalSetting.add(new Option(`${label} · POS ${code}`, code));
  }
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
  const group = appointmentType.value === "group";
  const selection = group ? groupPatientSelect.value : patientSelect.value;
  toggleFields(get("individual-patient-fields"), !group);
  toggleFields(get("group-patient-fields"), group);
  toggleFields(newPatientFields, selection === "new");
  toggleFields(addGroupPatient, group && Boolean(selection));
}

function patientOptions(exclude = []) {
  return [
    new Option("Choose a patient", ""),
    new Option("Add new patient", "new"),
    ...patients.filter((patient) => !exclude.includes(patient.reference)).map((patient) => {
      const identifiers = patientIdentifiers(patient);
      return new Option(identifiers ? `${patientLabel(patient)} · ${identifiers}` : patientLabel(patient), patient.reference);
    }),
  ];
}

function syncGroupHome() {
  const previous = groupHomeSelect.value;
  toggleFields(get("group-home-fields"), appointmentType.value === "group" && locationType.value === "home");
  groupHomeSelect.replaceChildren(
    new Option("Choose the host patient", ""),
    ...groupMembers.map(({ name, entry }) => new Option(name, entry.patient)),
  );
  if (groupMembers.some(({ entry }) => entry.patient === previous)) groupHomeSelect.value = previous;
  else if (groupMembers.length === 1) groupHomeSelect.value = groupMembers[0].entry.patient;
  return previous !== groupHomeSelect.value;
}

function updateGroupRoster() {
  groupRosterDraft.hidden = !groupMembers.length;
  groupRosterDraft.innerHTML = groupMembers.map(({ name, entry }) => `
    <li><span>${escapeHTML(name)}</span>
      <button type="button" data-remove-patient="${escapeHTML(entry.patient)}" aria-label="Remove ${escapeHTML(name)}">Remove</button></li>`).join("");
  groupPatientSelect.replaceChildren(...patientOptions(groupMembers.map(({ entry }) => entry.patient)));
  updatePatientFields();
  if (syncGroupHome() && locationType.value === "home") {
    clearLocationDraft();
    updateLocations();
  }
}

function newPatientEntry(reference, fields) {
  const patient = Object.fromEntries(patientFields.map((key) => [key, fields[key]]));
  const insurance = Object.fromEntries(Object.keys(insuranceFields).map((key) => [key, fields[key]]));
  if (Object.values(insurance).some((value) => value?.trim())
      && (!insurance.payer_name?.trim() || !insurance.member_id?.trim())) {
    get("booking-insurance").open = true;
    throw new Error("Enter the insurance payer and member ID.");
  }
  return { patient: reference, new_patient: patient, insurance };
}

function clearPatientDraft() {
  for (const key of [...patientFields, ...Object.keys(insuranceFields)]) form.elements[key].value = "";
  get("booking-insurance").open = false;
}

groupPatientSelect.addEventListener("change", () => {
  updatePatientFields();
  showError(get("booking-error"), "");
});
addGroupPatient.addEventListener("click", () => {
  try {
    const selection = groupPatientSelect.value;
    if (selection === "new") {
      if ([...newPatientFields.querySelectorAll("input")].some((input) => !input.reportValidity())) return;
      const entry = newPatientEntry(`patient-${crypto.randomUUID().replaceAll("-", "")}`, Object.fromEntries(new FormData(form)));
      if (!entry.new_patient.first_name.trim() || !entry.new_patient.last_name.trim()) {
        throw new Error("Enter a first and last name.");
      }
      groupMembers.push({ entry, name: patientLabel(entry.new_patient) });
    } else {
      const patient = patients.find((item) => item.reference === selection);
      if (!patient) throw new Error("Choose a patient to add to the group.");
      groupMembers.push({ entry: { patient: selection }, name: patientLabel(patient) });
    }
    clearPatientDraft();
    updateGroupRoster();
    groupPatientSelect.focus();
    showError(get("booking-error"), "");
  } catch (error) {
    showError(get("booking-error"), error.message);
  }
});
groupRosterDraft.addEventListener("click", (event) => {
  const remove = event.target.closest("[data-remove-patient]");
  if (!remove) return;
  groupMembers = groupMembers.filter(({ entry }) => entry.patient !== remove.dataset.removePatient);
  updateGroupRoster();
  groupPatientSelect.focus();
});

function toggleFields(fieldset, enabled) {
  fieldset.hidden = !enabled;
  fieldset.disabled = !enabled;
}

function updateLocationFields() {
  toggleFields(newLocationFields, locationType.value === "home" && locationSelect.value === "new");
}

async function updateLocations(preferred = "") {
  const request = ++locationRequest;
  const kind = locationType.value;
  const group = appointmentType.value === "group";
  const patient = group ? groupHomeSelect.value : patientSelect.value;
  const home = kind === "home";
  const savedPatient = group
    ? patients.some((entry) => entry.reference === patient)
    : patient && patient !== "new";
  const prompt = home && !patient ? (group ? "Choose the host patient first" : "Choose a patient first") : "Choose a location";
  showError(get("booking-error"), "");
  lastBookingLocation = "";
  locationSelect.replaceChildren(new Option(home && savedPatient ? "Loading saved homes…" : prompt, ""));
  updateLocationFields();
  try {
    const locations = home
      ? (savedPatient ? await api(`/api/patients/${encodeURIComponent(patient)}/homes`) : [])
      : facilities.filter((facility) => facility.kind === kind && !facility.archived);
    if (request !== locationRequest) return;
    locationSelect.replaceChildren(
      new Option(prompt, ""),
      ...locations.map((location) => new Option(home ? locationAddress(location) : location.name, location.reference)),
      ...(home && !patient ? [] : [new Option(addLocationLabels[kind], "new")]),
    );
    if (locations.some((location) => location.reference === preferred)) locationSelect.value = preferred;
    lastBookingLocation = locationSelect.value;
    updateLocationFields();
  } catch (error) {
    if (request !== locationRequest) return;
    locationSelect.replaceChildren(new Option("Saved homes unavailable", ""));
    showError(get("booking-error"), error.message);
  }
}

function clearLocationDraft() {
  for (const key of ["living_arrangement", ...addressFields]) {
    form.elements[key].value = "";
  }
  newLocationReference = crypto.randomUUID().replaceAll("-", "");
}

get("new-appointment").addEventListener("click", () => {
  form.reset();
  patientSelect.replaceChildren(...patientOptions());
  newPatientReference = `patient-${crypto.randomUUID().replaceAll("-", "")}`;
  groupReference = `group-${crypto.randomUUID().replaceAll("-", "")}`;
  groupMembers = [];
  clearPatientDraft();
  updateGroupRoster();
  clearLocationDraft();
  updateLocations("clinic");
  form.elements.day.value = viewDay.value;
  showError(get("booking-error"), "");
  dialog.showModal();
});
appointmentType.addEventListener("change", () => {
  clearPatientDraft();
  updatePatientFields();
  syncGroupHome();
  if (locationType.value === "home") {
    clearLocationDraft();
    updateLocations();
  }
});
patientSelect.addEventListener("change", () => {
  updatePatientFields();
  if (locationType.value === "home") {
    clearLocationDraft();
    updateLocations();
  }
});
locationType.addEventListener("change", () => {
  syncGroupHome();
  clearLocationDraft();
  updateLocations();
});
groupHomeSelect.addEventListener("change", () => {
  clearLocationDraft();
  updateLocations();
});
locationSelect.addEventListener("change", () => {
  if (locationSelect.value === "new" && locationType.value !== "home") {
    facilityHandoff = { previous: lastBookingLocation, saved: null };
    dialog.close();
    openFacilities();
    return;
  }
  if (locationSelect.value !== "new") lastBookingLocation = locationSelect.value;
  updateLocationFields();
});
dialog.addEventListener("close", () => { locationRequest++; });
get("cancel-booking").addEventListener("click", () => dialog.close());
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = form.querySelector('[type="submit"]');
  submit.disabled = true;
  showError(get("booking-error"), "");
  try {
    const fields = Object.fromEntries(new FormData(form));
    const { patient, location, location_kind, mode, day, time } = fields;
    const values = { location, location_kind, mode, day, time };
    if (appointmentType.value === "group") {
      if (groupPatientSelect.value) throw new Error("Add the selected patient to the group or clear the selection.");
      if (!groupMembers.length) throw new Error("Add at least one patient to the group.");
      values.group = { reference: groupReference, name: fields.group_name, patients: groupMembers.map(({ entry }) => entry) };
      if (location_kind === "home") values.home_patient = fields.home_patient;
    } else {
      Object.assign(values, patient === "new" ? newPatientEntry(newPatientReference, fields) : { patient });
    }
    if (values.location === "new") {
      values.location = `home-${newLocationReference}`;
      values.new_location = Object.fromEntries(
        ["living_arrangement", ...addressFields].map((key) => [key, fields[key]]),
      );
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

function showFacilityView(view, title) {
  if (view !== "editor") resetNpiLookup();
  get("facility-list-view").hidden = view !== "list";
  get("facility-detail-view").hidden = view !== "detail";
  facilityForm.hidden = view !== "editor";
  get("facility-title").textContent = title;
  facilityDialog.scrollTop = 0;
}

function renderFacilityList() {
  const query = facilitySearch.value.trim().toLowerCase();
  const type = facilityTypeFilter.value;
  const status = facilityStatusFilter.value;
  const visible = facilities.filter((facility) => {
    const matchesType = !type || (facility.facility_type ?? "unclassified") === type;
    const matchesStatus = status === "all" || Boolean(facility.archived) === (status === "archived");
    const searchText = [facility.name, facility.legal_name, facility.npi, locationAddress(facility)]
      .filter(Boolean).join(" ").toLowerCase();
    return matchesType && matchesStatus && searchText.includes(query);
  });
  facilityList.innerHTML = visible.map((facility) => {
    const subtitle = [
      facilityTypeLabels[facility.facility_type] || "Unclassified",
      facility.pos_code && `POS ${facility.pos_code}`, facility.city,
      facility.archived ? "Archived" : "",
    ].filter(Boolean).join(" · ");
    return `<li><button type="button" data-facility="${escapeHTML(facility.reference)}">
      <strong>${escapeHTML(facility.name)}</strong><small>${escapeHTML(subtitle)}</small>
    </button></li>`;
  }).join("");
  get("facility-list-empty").hidden = visible.length !== 0;
}

function showFacilityList() {
  showFacilityView("list", "Facilities");
  renderFacilityList();
  const selectedRow = [...facilityList.querySelectorAll("[data-facility]")]
    .find((item) => item.dataset.facility === selectedFacilityReference);
  (selectedRow || facilitySearch).focus();
}

function showFacilityDetails(reference) {
  const facility = facilities.find((entry) => entry.reference === reference);
  if (!facility) return;
  selectedFacilityReference = reference;
  const details = [
    ["Facility type", facilityTypeLabels[facility.facility_type] || "Unclassified"],
    ["Location POS code", facility.pos_code],
    ["Status", facility.archived ? "Archived" : "Active"],
    ["Legal operating name", facility.legal_name],
    ["NPI", facility.npi],
    ["Address", locationAddress(facility)],
  ];
  if (facility.facility_type === "hospital") details.splice(2, 0, ["Hospital setting", hospitalSettingLabels[facility.pos_code]]);
  if (facility.facility_type !== "pharmacy") {
    details.push(["Pharmacy contract", contractLabels[facility.contract_status ?? "unknown"]]);
    if (facility.contract_status === "contracted") {
      if (facility.contract_pharmacy_name) details.push(["Contracted pharmacy", facility.contract_pharmacy_name]);
      if (facility.contract_pharmacy_npi) details.push(["Contracted pharmacy NPI", facility.contract_pharmacy_npi]);
    }
  }
  get("facility-details").innerHTML = details.map(([label, value]) =>
    `<dt>${label}</dt><dd>${escapeHTML(value || "Not recorded")}</dd>`).join("");
  get("change-facility-status").textContent = facility.archived ? "Restore facility" : "Archive facility";
  showError(get("facility-detail-error"), "");
  showFacilityView("detail", facility.name);
  get("facility-title").focus();
}

function showFacilityEditor(facility = null) {
  resetNpiLookup();
  facilityForm.reset();
  facilityForm.elements.reference.value = facility?.reference ?? "";
  facilityType.value = facility?.facility_type ?? "";
  hospitalSetting.value = facility?.facility_type === "hospital" ? facility.pos_code ?? "" : "";
  facilityForm.elements.name.value = facility?.name ?? "";
  contractStatus.value = facility?.contract_status ?? "unknown";
  for (const key of contractPharmacyFields) {
    facilityForm.elements[key].value = facility?.[key] ?? "";
  }
  for (const key of siteDetails) {
    facilityForm.elements[key].value = facility?.[key] ?? "";
    if (key !== "address_line_2") {
      facilityForm.elements[key].required = !facility || Boolean(facility.legal_name);
    }
  }
  updateFacilityTypeFields();
  showError(get("facility-error"), "");
  showFacilityView("editor", facility ? "Edit facility" : "New facility");
  facilityNpi.focus();
}

function updateFacilityTypeFields() {
  toggleFields(get("hospital-setting-fields"), facilityType.value === "hospital");
  const hasContractField = facilityType.value !== "" && facilityType.value !== "pharmacy";
  toggleFields(get("facility-contract-fields"), hasContractField);
  toggleFields(get("contracted-pharmacy-fields"), hasContractField && contractStatus.value === "contracted");
}

function setNpiStatus(message) {
  npiStatus.textContent = message;
  npiStatus.hidden = !message;
}

function resetNpiLookup() {
  clearTimeout(npiLookupTimer);
  npiLookupController?.abort();
  matchedFacilityFields = null;
  npiAutofilled.clear();
  applyRegistryDetails.hidden = true;
  setNpiStatus("");
}

function updateRegistryButton() {
  applyRegistryDetails.hidden = !matchedFacilityFields || !Object.entries(matchedFacilityFields).some(([key, value]) =>
    key !== "facility_type" && facilityForm.elements[key].value.trim() !== value
  );
}

function fillFromRegistry(replace = false) {
  for (const [key, value] of Object.entries(matchedFacilityFields)) {
    const input = facilityForm.elements[key];
    if (input.value.trim() === value || (input.value.trim() && (!replace || key === "facility_type"))) continue;
    input.value = value;
    npiAutofilled.set(key, value);
  }
  updateFacilityTypeFields();
  updateRegistryButton();
}

async function lookUpNpi(npi, signal) {
  try {
    const result = await api(`/api/facilities/npi/${npi}`, undefined, signal);
    if (signal.aborted || !facilityDialog.open || facilityForm.hidden) return;
    if (result.match !== "organization") {
      setNpiStatus({
        individual: "This NPI is registered to an individual. Enter the facility details.",
        inactive: "The registry lists this NPI as inactive. Check the NPI before saving.",
        not_found: "No organization was found for this NPI. You can enter the facility details manually.",
      }[result.match]);
      return;
    }
    matchedFacilityFields = result.fields;
    fillFromRegistry();
    setNpiStatus(result.fields.street_address
      ? "Organization found. Review the registry details and practice address before saving."
      : "Organization found. Enter the facility’s practice address.");
  } catch (error) {
    if (!signal.aborted) {
      setNpiStatus("The NPI Registry is unavailable. You can enter the facility details manually.");
    }
  }
}

facilityNpi.addEventListener("input", () => {
  for (const [key, value] of npiAutofilled) {
    const input = facilityForm.elements[key];
    if (input.value === value) input.value = "";
  }
  resetNpiLookup();
  updateFacilityTypeFields();
  const npi = facilityNpi.value.trim();
  if (!/^[0-9]{10}$/.test(npi)) return;
  setNpiStatus("Searching the NPI Registry…");
  const controller = new AbortController();
  npiLookupController = controller;
  npiLookupTimer = setTimeout(() => lookUpNpi(npi, controller.signal), 300);
});
facilityForm.addEventListener("input", updateRegistryButton);
applyRegistryDetails.addEventListener("click", () => {
  fillFromRegistry(true);
  setNpiStatus("Registry details applied. Review the practice address before saving.");
  facilityForm.elements.name.focus();
});

function openFacilities() {
  selectedFacilityReference = null;
  facilityDialog.showModal();
  if (facilityHandoff) showFacilityEditor();
  else showFacilityList();
}

get("manage-facilities").addEventListener("click", openFacilities);
get("close-facilities").addEventListener("click", () => facilityDialog.close());
facilitySearch.addEventListener("input", renderFacilityList);
facilityTypeFilter.addEventListener("change", renderFacilityList);
facilityStatusFilter.addEventListener("change", renderFacilityList);
facilityList.addEventListener("click", (event) => {
  const row = event.target.closest("[data-facility]");
  if (row) showFacilityDetails(row.dataset.facility);
});
get("add-facility").addEventListener("click", () => showFacilityEditor());
get("facility-back").addEventListener("click", showFacilityList);
get("edit-facility").addEventListener("click", () => {
  showFacilityEditor(facilities.find((facility) => facility.reference === selectedFacilityReference));
});
facilityType.addEventListener("change", updateFacilityTypeFields);
contractStatus.addEventListener("change", updateFacilityTypeFields);
facilityDialog.addEventListener("close", () => {
  resetNpiLookup();
  if (!facilityHandoff) return;
  const { previous, saved } = facilityHandoff;
  facilityHandoff = null;
  if (saved) locationType.value = saved.kind;
  updateLocations(saved?.reference ?? previous);
  dialog.showModal();
  locationSelect.focus();
});
facilityDialog.addEventListener("cancel", (event) => {
  if (facilityBusy) {
    event.preventDefault();
  } else if (!facilityHandoff && !facilityForm.hidden) {
    event.preventDefault();
    cancelFacilityEditor();
  }
});

function cancelFacilityEditor() {
  if (facilityHandoff) facilityDialog.close();
  else if (facilityForm.elements.reference.value) showFacilityDetails(facilityForm.elements.reference.value);
  else showFacilityList();
}

function setFacilityBusy(busy) {
  facilityBusy = busy;
  for (const id of ["close-facilities", "cancel-facility", "facility-back", "edit-facility", "change-facility-status", "apply-registry-details"]) {
    get(id).disabled = busy;
  }
  facilityForm.querySelector('[type="submit"]').disabled = busy;
}

get("cancel-facility").addEventListener("click", cancelFacilityEditor);
get("change-facility-status").addEventListener("click", async () => {
  const facility = facilities.find((entry) => entry.reference === selectedFacilityReference);
  const action = facility.archived ? "restore" : "archive";
  setFacilityBusy(true);
  showError(get("facility-detail-error"), "");
  try {
    await api(`/api/facilities/${encodeURIComponent(facility.reference)}/${action}`, {});
    await refresh();
    showFacilityDetails(facility.reference);
    get("notice").textContent = facility.archived ? "Facility restored." : "Facility archived.";
  } catch (error) {
    showError(get("facility-detail-error"), error.message);
  } finally {
    setFacilityBusy(false);
  }
});
facilityForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  setFacilityBusy(true);
  showError(get("facility-error"), "");
  try {
    const values = Object.fromEntries(new FormData(facilityForm));
    if (values.reference && siteDetails.every((key) => !values[key].trim())) {
      for (const key of siteDetails) delete values[key];
    }
    const saved = await api("/api/facilities", values);
    await refresh();
    if (facilityHandoff) {
      facilityHandoff.saved = saved;
      facilityDialog.close();
    } else {
      showFacilityDetails(saved.reference);
    }
    get("notice").textContent = `${saved.name} saved.`;
  } catch (error) {
    showError(get("facility-error"), error.message);
  } finally {
    setFacilityBusy(false);
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
loadFacilityTypes().then(refresh).catch((error) => showError(get("desk-error"), error.message));
