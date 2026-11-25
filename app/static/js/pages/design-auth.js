import { clearErrorOnInput, setFeedback, validateFields } from "../components/feedback.js";

const signIn = document.getElementById("demo-sign-in");
const registration = document.getElementById("demo-registration");
const loginHeading = document.getElementById("auth-heading");
const registrationHeading = document.getElementById("registration-heading");
const loginPanel = signIn.closest("section");
const registrationPanel = registration.closest("section");
const loginStatus = document.getElementById("login-feedback");
const registrationStatus = document.getElementById("registration-feedback");

for (const field of document.querySelectorAll("form input")) clearErrorOnInput(field);

document.querySelectorAll("[data-auth-view]").forEach((link) => {
  link.addEventListener("click", (event) => {
    event.preventDefault();
    const joining = link.dataset.authView === "registration";
    loginPanel.hidden = joining;
    registrationPanel.hidden = !joining;
    const heading = joining ? registrationHeading : loginHeading;
    heading.tabIndex = -1;
    heading.focus();
  });
});

signIn.addEventListener("submit", (event) => {
  event.preventDefault();
  setFeedback(loginStatus, "");
  const [username, password] = signIn.querySelectorAll("input");
  if (validateFields([
    [username, username.value.trim() ? "" : "Enter your username."],
    [password, password.value ? "" : "Enter your password."],
  ])) {
    setFeedback(loginStatus, document.getElementById("invalid-credentials-example").textContent, "danger");
  }
});

registration.addEventListener("submit", (event) => {
  event.preventDefault();
  const [schoolCode, name] = registration.querySelectorAll("input");
  if (validateFields([
    [schoolCode, schoolCode.value.trim() ? "" : "Enter the school code provided by your school."],
    [name, name.value.trim() ? "" : "Enter your full name."],
  ])) {
    setFeedback(registrationStatus, "The sample form is ready for review.", "success");
  }
});
