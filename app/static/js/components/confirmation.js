import { closeDialog, openDialog } from "../core/layout.js";
import { setFeedback } from "./feedback.js";

// Adapter outcomes are confirmed, rejected (definitely unapplied), or uncertain.
export function confirmation(dialog, { submit, processing, onSuccess, onReturn = () => {} }) {
  const confirm = dialog.querySelector("[data-confirm]");
  const cancel = dialog.querySelector("[data-close-dialog]");
  const status = dialog.querySelector("[data-dialog-status]");
  const defaultLabel = confirm.textContent;
  let label = defaultLabel;
  let operation;
  let busy = false;
  let uncertain = false;

  function lockCancellation(locked) {
    cancel.disabled = locked;
  }

  dialog.addEventListener("cancel", (event) => {
    if (busy || uncertain) event.preventDefault();
  });
  dialog.addEventListener("close", () => {
    if (operation) onReturn();
    operation = undefined;
  });

  confirm.addEventListener("click", async () => {
    if (!operation || busy) return;
    busy = true;
    confirm.setAttribute("aria-disabled", "true");
    confirm.setAttribute("aria-busy", "true");
    lockCancellation(true);
    setFeedback(status, uncertain ? "Checking the previous submission…" : processing);

    let outcome;
    try {
      outcome = await submit(operation, { reconcile: uncertain });
    } catch {
      outcome = { kind: "uncertain" };
    }

    busy = false;
    confirm.removeAttribute("aria-disabled");
    confirm.removeAttribute("aria-busy");
    uncertain = outcome.kind === "uncertain";
    lockCancellation(uncertain);
    if (outcome.kind === "confirmed") {
      operation = undefined;
      const focus = onSuccess(outcome.data);
      closeDialog(dialog, focus);
    } else if (uncertain) {
      confirm.textContent = "Check result";
      setFeedback(status, "ClubOS couldn’t confirm the result. Check the original submission before trying again.", "danger");
    } else {
      confirm.textContent = label;
      setFeedback(status, outcome.message ?? "ClubOS couldn’t complete this action. You can try again.", "danger");
    }
  });

  return {
    review(payload, trigger, actionLabel = defaultLabel) {
      operation = { payload, idempotencyKey: crypto.randomUUID() };
      label = actionLabel;
      confirm.textContent = label;
      setFeedback(status, "");
      openDialog(dialog, trigger);
    },
  };
}
