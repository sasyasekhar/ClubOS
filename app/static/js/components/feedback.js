export function setFeedback(region, message, tone = "neutral") {
  region.dataset.tone = tone;
  region.textContent = message;
}

export function updateFieldError(field, message = "") {
  const error = document.getElementById(`${field.id}-error`);
  error.textContent = message;
  error.hidden = !message;
  if (message) field.setAttribute("aria-invalid", "true");
  else field.removeAttribute("aria-invalid");
}

export function validateFields(fields) {
  let firstInvalid;
  for (const [field, message] of fields) {
    updateFieldError(field, message);
    if (message) firstInvalid ??= field;
  }
  firstInvalid?.focus();
  return !firstInvalid;
}

export function clearErrorOnInput(field) {
  field.addEventListener("input", () => updateFieldError(field));
}

export function setStep(list, index) {
  [...list.children].forEach((item, position) => {
    if (position === index) item.setAttribute("aria-current", "step");
    else item.removeAttribute("aria-current");
  });
}

export function renderPagination(element, page, hasNext, busy = false) {
  element.querySelector("[data-prev]").setAttribute("aria-disabled", String(busy || page === 1));
  element.querySelector("[data-next]").setAttribute("aria-disabled", String(busy || !hasNext));
  element.querySelector("[data-prev]").disabled = busy || page === 1;
  element.querySelector("[data-next]").disabled = busy || !hasNext;
  element.querySelector("[data-page-label]").textContent = `Page ${page}`;
}
