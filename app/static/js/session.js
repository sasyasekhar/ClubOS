
const KEY = "clubos_tab_key";

export async function signIn(identifier, password) {
  const response = await fetch("/auth/login", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ identifier, password })
  });

  if (!response.ok) throw new Error("Login failed");

  const data = await response.json();
  sessionStorage.setItem(KEY, data.tab_key);
  return data.account;
}

export async function authFetch(url, options = {}) {
  const key = sessionStorage.getItem(KEY);
  if (!key) throw new Error("Login required");

  const headers = new Headers(options.headers);
  headers.set("X-ClubOS-Tab", key);

  const response = await fetch(url, {
    ...options,
    headers,
    credentials: "same-origin"
  });

  if (response.status === 401) {
    sessionStorage.removeItem(KEY);
  }

  return response;
}

export async function signOut() {
  try {
    if (sessionStorage.getItem(KEY)) {
      await authFetch("/auth/logout", { method: "POST" });
    }
  } finally {
    sessionStorage.removeItem(KEY);
  }
}

// Renew only while the user is actually interacting.
let lastActivity = Date.now();

for (const event of ["keydown", "pointerdown", "input", "scroll"]) {
  window.addEventListener(event, () => {
    lastActivity = Date.now();
  }, { passive: true });
}

setInterval(async () => {
  if (!sessionStorage.getItem(KEY)) return;
  if (document.visibilityState !== "visible") return;
  if (Date.now() - lastActivity > 120_000) return;

  try {
    await authFetch("/auth/heartbeat", { method: "POST" });
  } catch {
    // Network failures do not automatically erase an active session.
  }
}, 60_000);
