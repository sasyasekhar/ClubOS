import { apiFetch } from "../core/api.js";
import { renderPagination, setFeedback } from "./feedback.js";

export function mountAccountSearch(stage, onSelect) {
  const input = stage.querySelector("input");
  const status = stage.querySelector("[role=status]");
  const results = stage.querySelector("[aria-busy]");
  const list = results.querySelector("ul");
  const skeleton = results.querySelector("[data-skeleton]");
  const pages = stage.querySelector("#account-pages");
  const pagination = pages.querySelector("nav");
  const retry = stage.querySelector("#account-retry");
  let cursors = [null];
  let page = 1;
  let next = null;
  let failedPage;
  let pending;
  let timer;

  function resetPending() {
    clearTimeout(timer);
    pending?.abort();
    pending = undefined;
  }

  function showAccounts(items) {
    list.replaceChildren();
    for (const account of items) {
      const row = document.createElement("li");
      const option = document.createElement("button");
      const name = document.createElement("span");
      const detail = document.createElement("span");
      option.type = "button";
      option.className = "button button--secondary account-option";
      name.textContent = account.name;
      detail.textContent = `${account.username} · ${account.role}`;
      option.append(name, detail);
      option.addEventListener("click", () => onSelect(account));
      row.append(option);
      list.append(row);
    }
  }

  async function load(targetPage) {
    resetPending();
    const request = new AbortController();
    pending = request;
    const url = new URL(stage.dataset.searchUrl, location.origin);
    url.searchParams.set("q", input.value.trim());
    const cursor = cursors[targetPage - 1];
    if (cursor !== null) url.searchParams.set("after", cursor);

    list.replaceChildren();
    skeleton.hidden = false;
    retry.hidden = true;
    results.setAttribute("aria-busy", "true");
    renderPagination(pagination, page, next !== null, true);
    setFeedback(status, "Searching eligible accounts…");
    const timeout = setTimeout(() => request.abort(), 8000);

    try {
      const response = await apiFetch(url, { signal: request.signal });
      if (!response.ok) throw new Error("Search unavailable");
      const data = await response.json();
      if (pending !== request) return;
      page = targetPage;
      next = data.next_cursor;
      showAccounts(data.items);
      setFeedback(status, data.items.length
        ? `${data.items.length} eligible ${data.items.length === 1 ? "account" : "accounts"} on this page.`
        : "No eligible accounts match that name. Try a different search.");
      pages.hidden = page === 1 && next === null;
      renderPagination(pagination, page, next !== null);
    } catch {
      if (pending !== request) return;
      failedPage = targetPage;
      pages.hidden = true;
      retry.hidden = false;
      setFeedback(status, "Couldn’t load eligible accounts. Try the search again.", "danger");
    } finally {
      clearTimeout(timeout);
      if (pending === request) {
        pending = undefined;
        skeleton.hidden = true;
        results.setAttribute("aria-busy", "false");
      }
    }
  }

  input.addEventListener("input", () => {
    resetPending();
    cursors = [null];
    page = 1;
    next = null;
    pages.hidden = true;
    retry.hidden = true;
    skeleton.hidden = true;
    results.setAttribute("aria-busy", "false");
    list.replaceChildren();
    setFeedback(status, "");
    if (input.value.trim()) timer = setTimeout(() => load(1), 250);
  });

  retry.addEventListener("click", () => {
    input.focus();
    load(failedPage);
  });
  pagination.querySelector("[data-prev]").addEventListener("click", () => {
    input.focus();
    load(page - 1);
  });
  pagination.querySelector("[data-next]").addEventListener("click", () => {
    cursors[page] = next;
    input.focus();
    load(page + 1);
  });
}
