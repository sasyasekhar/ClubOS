const returnFocus = new WeakMap();

export function openDialog(dialog, trigger = document.activeElement) {
  returnFocus.set(dialog, trigger);
  dialog.showModal();
}

function restoreFocus(dialog, target = returnFocus.get(dialog)) {
  returnFocus.delete(dialog);
  if (target?.isConnected && target.getClientRects().length) target.focus();
}

export function closeDialog(dialog, focusTarget = returnFocus.get(dialog)) {
  dialog.close();
  restoreFocus(dialog, focusTarget);
}

document.querySelectorAll("dialog").forEach((dialog) => {
  dialog.addEventListener("close", () => {
    if (!dialog.open) restoreFocus(dialog);
  });
});

document.addEventListener("click", (event) => {
  const trigger = event.target.closest("[data-open-dialog]");
  if (trigger) openDialog(document.getElementById(trigger.dataset.openDialog), trigger);
  const closer = event.target.closest("[data-close-dialog]");
  if (closer) closeDialog(closer.closest("dialog"));
  const link = event.target.closest(".nav-drawer a");
  if (link) {
    const dialog = link.closest("dialog");
    const id = link.getAttribute("href");
    const heading = id.startsWith("#") && document.getElementById(id.slice(1));
    if (heading) {
      event.preventDefault();
      heading.tabIndex = -1;
      history.pushState(null, "", id);
      closeDialog(dialog, heading);
      heading.scrollIntoView();
    } else {
      closeDialog(dialog);
    }
  }
  for (const disclosure of document.querySelectorAll(".disclosure[open]")) {
    if (!disclosure.contains(event.target)) disclosure.open = false;
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  const disclosure = event.target.closest(".disclosure[open]");
  if (disclosure) {
    disclosure.open = false;
    disclosure.querySelector("summary").focus();
  }
});

const desktop = matchMedia("(min-width: 64rem)");
desktop.addEventListener("change", ({ matches }) => {
  const drawer = document.getElementById("nav-drawer");
  if (matches && drawer?.open) {
    closeDialog(drawer, document.querySelector(".side-rail a"));
  }
});
