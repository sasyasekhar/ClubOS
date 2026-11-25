export function representAccess(role) {
  return role === "Teacher"
    ? "Authorized activity reviews only."
    : "Read-only student access.";
}

export function mountImpersonationBanner(root, announcement, onEnd) {
  const timerText = root.querySelector("[data-remaining]");
  const endButton = root.querySelector("[data-end-impersonation]");
  let interval;
  let state;

  function finish(message, hadEndFocus = document.activeElement === endButton) {
    clearInterval(interval);
    state = undefined;
    root.hidden = true;
    announcement.textContent = message;
    onEnd({ hadEndFocus, message });
  }

  function render() {
    const seconds = Math.ceil((Date.parse(state.expires_at) - Date.now()) / 1000);
    if (!Number.isFinite(seconds) || seconds <= 0) {
      finish("Impersonation has expired.");
      return;
    }
    timerText.textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  }

  endButton.addEventListener("click", () => finish("Impersonation ended.", true));

  return {
    show(nextState, announce = true) {
      clearInterval(interval);
      state = nextState;
      root.querySelector("[data-represented-name]").textContent = state.represented_name;
      root.querySelector("[data-represented-role]").textContent = `· ${state.represented_role}`;
      root.querySelector("[data-represented-access]").textContent = representAccess(state.represented_role);
      root.hidden = false;
      if (announce) announcement.textContent = `Impersonation active for ${state.represented_name}.`;
      render();
      if (state) interval = setInterval(render, 1000);
      return state ? endButton : undefined;
    },
  };
}
