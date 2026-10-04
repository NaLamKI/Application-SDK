/*
 * appext client helper, served by every extension at /_sdk/client.js.
 *
 *     import { extFetch } from "/_sdk/client.js";
 *     const res = await extFetch("/api/projects");
 *
 * extFetch is fetch() with two additions:
 *
 *  1. It sends the CSRF header (X-Appext-CSRF) the SDK requires on writing
 *     requests, and the session cookie (same origin only).
 *  2. When the API answers 401 with a login_url, it navigates the TOP-LEVEL
 *     window to the sign-in, with return_to set to the page the person is on.
 *     Never a fetch: the app can only intercept the authorize request when it
 *     is a real navigation. The returned promise then never settles - the page
 *     is about to go away, and no caller should render an error for it.
 *
 * A guard stops a sign-in loop: if three navigations happen within 30 seconds
 * (the session is not accepted even right after signing in), extFetch gives
 * up and returns the 401 response instead of redirecting forever.
 */
const CSRF_HEADER = "X-Appext-CSRF";
const GUARD_KEY = "appext.signin";
const GUARD_WINDOW_MS = 30000;
const GUARD_MAX = 3;

function guardAllows() {
  try {
    const now = Date.now();
    const recent = JSON.parse(sessionStorage.getItem(GUARD_KEY) || "[]").filter((t) => now - t < GUARD_WINDOW_MS);
    if (recent.length >= GUARD_MAX) return false;
    recent.push(now);
    sessionStorage.setItem(GUARD_KEY, JSON.stringify(recent));
  } catch (_) {
    /* storage unavailable: no guard, but still correct */
  }
  return true;
}

async function loginTarget(response) {
  let body;
  try {
    body = await response.clone().json();
  } catch (_) {
    return null;
  }
  // Only a sign-in on our own origin is ever followed.
  if (!body || typeof body.login_url !== "string" || !body.login_url.startsWith("/auth/")) return null;
  const url = new URL(body.login_url, window.location.origin);
  url.searchParams.set("return_to", window.location.pathname + window.location.search);
  return url.pathname + url.search;
}

export async function extFetch(input, init = {}) {
  const base = input instanceof Request ? input.headers : undefined;
  const headers = new Headers(init.headers || base);
  headers.set(CSRF_HEADER, "1");
  if (!headers.has("Accept")) headers.set("Accept", "application/json");

  const response = await fetch(input, { ...init, headers, credentials: "same-origin" });
  if (response.status === 401) {
    const target = await loginTarget(response);
    if (target && guardAllows()) {
      window.location.assign(target);
      return new Promise(() => {});
    }
  }
  return response;
}

export default extFetch;
