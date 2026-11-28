"use strict";

const form = document.querySelector("#upload-form");
const input = document.querySelector("#edi-file");
const summary = document.querySelector("#result-summary");
const interchangeStatus = document.querySelector("#interchange-status");
const fileStatus = document.querySelector("#file-status");
const claimStatus = document.querySelector("#claim-status");
const issues = document.querySelector("#issues");
const actions = document.querySelectorAll("#check-button, [data-example]");
const copy = document.querySelector("#copy-button");
const apiExample = document.querySelector("#api-example");
apiExample.textContent = apiExample.textContent.replaceAll("https://claimlab.opensoft.dev", location.origin);

const statuses = {
  accepted: "Accepted",
  rejected: "Rejected",
  mixed: "Mixed results",
  not_generated: "Not generated",
  waiting: "Awaiting file",
};

function setStatus(element, status) {
  element.textContent = statuses[status];
  element.dataset.status = status;
}

function showIssues(messages) {
  issues.replaceChildren();
  for (const message of messages) {
    const item = document.createElement("li");
    item.textContent = message;
    issues.append(item);
  }
  issues.hidden = messages.length === 0;
}

async function runCheck(loadFile) {
  for (const action of actions) action.disabled = true;
  summary.textContent = "Checking file…";
  setStatus(interchangeStatus, "waiting");
  setStatus(fileStatus, "waiting");
  setStatus(claimStatus, "waiting");
  showIssues([]);
  try {
    const file = await loadFile();
    if (file.size > 1024 * 1024) throw new Error("The maximum file size is 1 MiB.");
    const response = await fetch("/api/v1/validate", {
      method: "POST",
      headers: {"Content-Type": "application/edi-x12"},
      body: file,
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error.message);
    setStatus(interchangeStatus, result.interchange_ack.status);
    setStatus(fileStatus, result.file_ack.status);
    setStatus(claimStatus, result.claim_ack.status);
    const count = result.claim_ack.claims.length;
    summary.textContent = result.accepted
      ? count + (count === 1 ? " claim" : " claims") + " passed the simulation."
      : "The simulation found issues.";
    const messages = result.interchange_ack.issues.map(issue => {
      const label = issue.ta105 ? "TA1 " + issue.ta105 : "TA1";
      return label + " · " + issue.message;
    });
    for (const issue of result.file_ack.issues) messages.push(issue.message);
    for (const claim of result.claim_ack.claims) {
      for (const issue of claim.issues) {
        messages.push("Claim " + claim.number + " · " + issue.message);
      }
    }
    showIssues(messages);
  } catch (error) {
    summary.textContent = "Check could not run.";
    showIssues([error.message]);
  } finally {
    for (const action of actions) action.disabled = false;
  }
}

form.addEventListener("submit", event => {
  event.preventDefault();
  const [file] = input.files;
  if (file) runCheck(() => file);
});

for (const button of document.querySelectorAll("[data-example]")) {
  button.addEventListener("click", () => runCheck(async () => {
    const response = await fetch("/api/v1/examples/" + button.dataset.example);
    if (!response.ok) throw new Error("The sample file is unavailable.");
    return response.blob();
  }));
}

copy.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(apiExample.textContent);
    copy.textContent = "Copied";
  } catch {
    copy.textContent = "Copy from the code block";
  }
});
