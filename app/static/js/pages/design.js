import { openDialog } from "../core/layout.js";
import { mountAccountSearch } from "../components/account-search.js";
import { confirmation } from "../components/confirmation.js";
import { clearErrorOnInput, renderPagination, setFeedback, setStep, validateFields } from "../components/feedback.js";
import { mountImpersonationBanner, representAccess } from "../components/impersonation-banner.js";

const byId = (id) => document.getElementById(id);
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const confirmed = (data) => ({ kind: "confirmed", data });

function mountActivityReview() {
  const form = byId("review-form");
  const decision = byId("decision");
  const comments = byId("review-comments");
  const steps = byId("review-steps");
  const result = byId("review-result");
  const dialog = byId("review-confirm");
  const lostConnection = byId("review-delay");
  let receipt;
  const decisions = {
    APPROVED: { label: "Approve activity", confirm: "Approve activity", effect: "The reviewed revision will be marked approved." },
    CHANGES_REQUESTED: { label: "Request changes", confirm: "Request changes", effect: "The student can use your explanation to revise the activity." },
    REJECTED: { label: "Reject activity", confirm: "Reject activity", effect: "The revision will be marked rejected and the student can read your explanation." },
  };
  const review = confirmation(dialog, {
    processing: "Submitting your review…",
    async submit(operation, { reconcile }) {
      await wait(650);
      if (reconcile) {
        return receipt?.key === operation.idempotencyKey
          ? confirmed(receipt.decision)
          : { kind: "uncertain" };
      }

      receipt = { key: operation.idempotencyKey, decision: operation.payload.decision };
      return operation.payload.simulateLoss ? { kind: "uncertain" } : confirmed(receipt.decision);
    },
    onSuccess(value) {
      setStep(steps, 2);
      setFeedback(result, `Sample review saved. Decision: ${decisions[value].label}.`, "success");
    },
    onReturn() { setStep(steps, 0); },
  });

  for (const input of [decision, comments]) clearErrorOnInput(input);
  decision.addEventListener("input", () => {
    comments.toggleAttribute("required", ["CHANGES_REQUESTED", "REJECTED"].includes(decision.value));
  });

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const value = decision.value;
    const explanation = comments.value.trim();
    const requiredExplanation = {
      CHANGES_REQUESTED: "Explain what the student needs to change before submitting this review.",
      REJECTED: "Explain why the activity is being rejected before submitting this review.",
    };
    if (!validateFields([
      [decision, decisions[value] ? "" : "Choose a review decision."],
      [comments, explanation ? "" : requiredExplanation[value] ?? ""],
    ])) return;

    const specimen = decisions[value];
    setFeedback(result, "");
    byId("review-decision").textContent = specimen.label;
    byId("review-explanation").textContent = explanation || "No explanation provided.";
    byId("review-consequence").textContent = specimen.effect;
    const button = dialog.querySelector("[data-confirm]");
    button.classList.toggle("button--danger", value === "REJECTED");
    button.classList.toggle("button--primary", value !== "REJECTED");
    setStep(steps, 1);
    review.review({ decision: value, explanation, simulateLoss: lostConnection.checked }, event.submitter, specimen.confirm);
  });
}

function mountImpersonation() {
  const key = "clubos_design_impersonation";
  const search = byId("account-search-stage");
  const searchField = byId("account-search");
  const form = byId("reason-form");
  const reason = byId("impersonation-reason");
  const steps = byId("impersonation-steps");
  const result = byId("impersonation-result");
  const dialog = byId("impersonation-confirm");
  let account;

  const banner = mountImpersonationBanner(
    byId("impersonation-banner"), byId("impersonation-announcement"),
    ({ hadEndFocus, message }) => {
      sessionStorage.removeItem(key);
      search.hidden = false;
      setStep(steps, 0);
      setFeedback(result, message);
      if (hadEndFocus) searchField.focus();
    },
  );

  const start = confirmation(dialog, {
    processing: "Starting impersonation…",
    async submit({ payload }) {
      await wait(550);
      // This synthetic response mirrors the timestamps returned by the live start endpoint.
      const startedAt = Date.now();
      return confirmed({
        represented_name: payload.account.name,
        represented_role: payload.account.role,
        started_at: new Date(startedAt).toISOString(),
        expires_at: new Date(startedAt + 15 * 60 * 1000).toISOString(),
      });
    },
    onSuccess(state) {
      sessionStorage.setItem(key, JSON.stringify(state));
      search.hidden = true;
      form.hidden = true;
      reason.value = "";
      setStep(steps, 3);
      setFeedback(result, "The sample impersonation is active.");
      return banner.show(state);
    },
    onReturn() { setStep(steps, 1); },
  });

  mountAccountSearch(search, (selected) => {
    account = selected;
    byId("selected-account").textContent = `${account.name} · ${account.username} · ${account.role}`;
    search.hidden = true;
    form.hidden = false;
    setStep(steps, 1);
    reason.focus();
  });

  byId("change-account").addEventListener("click", () => {
    form.hidden = true;
    search.hidden = false;
    setStep(steps, 0);
    searchField.focus();
  });

  clearErrorOnInput(reason);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const explanation = reason.value.trim();
    if (!validateFields([[reason, explanation ? "" : "Explain why you need to impersonate this account."]])) return;
    byId("confirm-account").textContent = `${account.name} · ${account.username}`;
    byId("confirm-role").textContent = account.role;
    byId("confirm-reason").textContent = explanation;
    byId("confirm-access").textContent = `${representAccess(account.role)} No changes to personal information or credentials and no administrative actions.`;
    setStep(steps, 2);
    start.review({ account, reason: explanation }, event.submitter);
  });

  try {
    const saved = JSON.parse(sessionStorage.getItem(key));
    if (saved && ["Teacher", "Student"].includes(saved.represented_role) && typeof saved.represented_name === "string") {
      search.hidden = true;
      setStep(steps, 3);
      setFeedback(result, "The sample impersonation is active.");
      banner.show(saved, false);
    }
  } catch {
    // Storage may be blocked in private browser contexts.
  }
}

function showToast(message) {
  const toast = byId("showcase-toast");
  clearTimeout(showToast.timer);
  toast.textContent = message;
  showToast.timer = setTimeout(() => { toast.textContent = ""; }, 5000);
}

function mountSpecimens() {
  const pager = byId("table-pagination");
  let page = 1;
  function setPage(next) {
    const focused = document.activeElement;
    page = next;
    document.querySelectorAll("[data-table-page]").forEach((row) => {
      row.hidden = Number(row.dataset.tablePage) !== page;
    });
    renderPagination(pager, page, page < 2);
    if (pager.contains(focused) && focused.disabled) pager.querySelector("button:not(:disabled)").focus();
  }
  pager.querySelector("[data-prev]").addEventListener("click", () => setPage(1));
  pager.querySelector("[data-next]").addEventListener("click", () => setPage(2));
  setPage(1);

  const retryPreview = byId("retry-preview");
  const retry = retryPreview.querySelector("[data-load-retry]");
  retry.addEventListener("click", async () => {
    if (retry.getAttribute("aria-disabled") === "true") return;
    retry.setAttribute("aria-disabled", "true");
    retry.textContent = "Loading clubs…";
    retryPreview.querySelector("[data-load-error]").hidden = true;
    const skeleton = retryPreview.querySelector("[data-skeleton]");
    skeleton.hidden = false;
    await wait(700);
    skeleton.hidden = true;
    retryPreview.querySelector("[data-load-result]").hidden = false;
    retryPreview.querySelector("[data-load-status]").textContent = "Club list loaded.";
    retry.textContent = "Clubs loaded";
  });
  byId("show-toast").addEventListener("click", () => showToast("Sample notification sent."));
  const remove = byId("remove-demo");
  const confirmationPreview = confirmation(remove, {
    processing: "Removing the sample draft…",
    async submit() {
      await wait(350);
      return confirmed();
    },
    onSuccess() {
      document.querySelector("[data-sample-draft]").remove();
      const trigger = byId("remove-demo-button");
      trigger.setAttribute("aria-disabled", "true");
      trigger.textContent = "Sample draft removed";
      showToast("Sample draft removed.");
    },
  });
  byId("remove-demo-button").addEventListener("click", (event) => {
    if (event.currentTarget.getAttribute("aria-disabled") !== "true") {
      confirmationPreview.review({}, event.currentTarget);
    }
  });
  document.querySelector("[data-sign-out]").addEventListener("click", (event) => {
    const disclosure = event.currentTarget.closest("details");
    disclosure.open = false;
    openDialog(byId("demo-sign-out"), disclosure.querySelector("summary"));
  });
}

mountActivityReview();
mountImpersonation();
mountSpecimens();
