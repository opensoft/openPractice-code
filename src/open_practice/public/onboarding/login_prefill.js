"use strict";

if (location.pathname === "/login") {
  document.addEventListener("DOMContentLoaded", () => {
    try {
      const email = sessionStorage.getItem("openpractice-login-email");
      sessionStorage.removeItem("openpractice-login-email");
      const input = document.querySelector("#login_email");
      if (email && input && !input.value) {
        input.value = email;
        if (document.activeElement === input) {
          document.querySelector("#login_password")?.focus();
        }
      }
    } catch {
      // The sign-in form also works without session storage.
    }
  });
}
