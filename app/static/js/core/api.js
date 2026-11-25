export function sameOriginURL(target) {
  const url = new URL(target, window.location.href);
  if (url.origin !== window.location.origin || url.protocol !== location.protocol || url.username || url.password) {
    throw new TypeError("ClubOS requests must stay on the current origin.");
  }
  return url;
}

export function apiFetch(target, options = {}) {
  return fetch(sameOriginURL(target), {
    ...options,
    credentials: "same-origin",
    redirect: "error",
  });
}
