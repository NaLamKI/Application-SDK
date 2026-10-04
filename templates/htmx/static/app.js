// What the SDK's extFetch does for fetch(), for htmx requests.
//
// A request that finds the session gone is answered with a redirect to the sign-in,
// which the browser cannot follow inside an XHR (it leads to another origin):
// htmx reports a network error. Reloading the page is a top-level navigation – it
// reaches the sign-in, and the person returns to this page afterwards.
document.addEventListener("htmx:sendError", () => window.location.reload());

// Missing consent for a service: 401 with a login_url that asks for the missing scopes.
document.addEventListener("htmx:responseError", (event) => {
  const xhr = event.detail.xhr;
  if (xhr.status !== 401) return;
  try {
    const target = JSON.parse(xhr.responseText).login_url;
    if (typeof target !== "string" || !target.startsWith("/auth/")) return; // only our own origin
    const url = new URL(target, window.location.origin);
    url.searchParams.set("return_to", window.location.pathname + window.location.search);
    window.location.assign(url.pathname + url.search);
  } catch (_) {
    /* not the SDK's answer: leave it to the page */
  }
});
