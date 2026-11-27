"use strict";

const setup = document.querySelector("#setup");
const connection = document.querySelector("#connection");
const notice = document.querySelector("#notice");
const company = document.querySelector("#company");
const actionLabel = document.querySelector("#action-label");
const connect = document.querySelector("#connect");
const linkFields = document.querySelector("#link-fields");
const mode = connection.elements.mode;
const csrf = document.querySelector('meta[name="practicelab-csrf"]').content;
let busy = false;
let connected = false;

async function api(method, values) {
  const options = values ? {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-Frappe-CSRF-Token": csrf},
    body: JSON.stringify(values),
  } : {};
  const response = await fetch("/api/method/open_practice.frappe_practice_onboarding." + method, options);
  const result = await response.json();
  if (!response.ok) {
    const messages = result._server_messages && JSON.parse(result._server_messages);
    const message = messages && messages.length && JSON.parse(messages[0]).message;
    throw new Error(message || "The request could not be completed. Try again.");
  }
  return result.message;
}

function updateLinkFields() {
  linkFields.hidden = connected || mode.value !== "Link existing Company";
  connection.elements.company_id.required = !linkFields.hidden;
  connection.elements.confirmed.required = !linkFields.hidden;
}

function render(record) {
  if (!record || !record.office_name) return;
  for (const field of ["office_name", "office_address", "legal_entity_name", "practice_type"]) {
    setup.elements[field].value = record[field] || "";
  }
  connected = Boolean(record.openhr_company);
  connection.hidden = false;
  mode.value = record.connection_mode || "Create Company";
  connection.elements.company_id.value = record.requested_company || "";
  actionLabel.hidden = connected || (
    record.connection_mode === "Create Company" && record.connection_status === "Connection unresolved"
  );
  company.hidden = !connected;
  company.textContent = connected ? record.openhr_company + " · " + record.openhr_site : "";
  connect.hidden = connected;
  connect.textContent = record.connection_status === "Connection unresolved" ? "Retry connection" : "Connect Company";
  updateLinkFields();
  notice.textContent = record.notice || record.connection_status || "";
}

async function run(action) {
  if (busy) return;
  busy = true;
  const buttons = [setup.querySelector("button"), connect];
  buttons.forEach(button => { button.disabled = true; });
  notice.textContent = "Working…";
  try {
    const record = await action();
    notice.textContent = "";
    render(record);
  } catch (error) {
    notice.textContent = error.message || "The request could not be completed. Try again.";
  } finally {
    busy = false;
    buttons.forEach(button => { button.disabled = false; });
  }
}

mode.addEventListener("change", updateLinkFields);
setup.addEventListener("submit", event => {
  event.preventDefault();
  run(() => api("save_setup", Object.fromEntries(new FormData(setup))));
});
connection.addEventListener("submit", event => {
  event.preventDefault();
  run(() => api("connect_company", {
    mode: mode.value,
    company_id: connection.elements.company_id.value,
    confirmed: connection.elements.confirmed.checked,
  }));
});
run(() => api("get_setup"));
