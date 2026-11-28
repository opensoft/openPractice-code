"use strict";

const signup = document.querySelector("#request-signup");
const confirmation = document.querySelector("#confirm-signup");
const notice = document.querySelector("#notice");
const csrf = document.querySelector('meta[name="practicelab-csrf"]').content;
const apiRoot = "/api/method/open_practice.frappe_account_signup.";

async function send(method, values) {
  const response = await fetch(apiRoot + method, {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-Frappe-CSRF-Token": csrf},
    body: JSON.stringify(values),
  });
  if (response.status === 429) {
    throw new Error("Too many requests. Wait a few minutes and try again.");
  }
  if (!response.ok) {
    throw new Error("The request could not be completed. Try again.");
  }
  return (await response.json()).message;
}

function handle(form, action) {
  const button = form.querySelector("button");
  button.disabled = false;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    button.disabled = true;
    notice.textContent = "";
    try {
      await action();
    } catch (error) {
      notice.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });
}

if (signup) {
  handle(signup, async () => {
    await send("request_signup", {email: signup.elements.email.value});
    notice.textContent =
      "If this address can receive an invitation, check your inbox for a verification link.";
    signup.querySelector("button").textContent = "Send another verification email";
  });

  let verification;
  function showVerification() {
    const match = /^#verify=([0-9a-f]{64})\.([0-9a-f]{64})$/.exec(location.hash);
    if (!match) return;

    history.replaceState(null, "", location.pathname);
    verification = {request_id: match[1], token: match[2]};
    notice.textContent = "";
    signup.hidden = true;
    confirmation.hidden = false;
  }

  handle(confirmation, async () => {
    const result = await send("verify_signup", verification);
    verification = null;
    confirmation.hidden = true;
    signup.hidden = result.verified;
    notice.textContent = result.verified
      ? "Work email verified. A QA operator will review your request and send your invitation."
      : "This link is no longer valid. Request another verification email if you still need to verify.";
  });
  window.addEventListener("hashchange", showVerification);
  showVerification();
}

for (const form of document.querySelectorAll(".issue-invite")) {
  handle(form, async () => {
    const result = await send("issue_invite", {request_id: form.elements.request_id.value});
    if (result.issued) {
      form.remove();
      notice.textContent = "Practice invitation queued.";
    } else {
      notice.textContent = "This address already has an account. Use the existing access process.";
    }
  });
}
