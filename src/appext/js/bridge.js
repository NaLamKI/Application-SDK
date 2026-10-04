/*
 * appext bridge to the host app, served by every extension at /_sdk/bridge.js
 * (and loaded into every HTML page the SDK serves, so nobody has to remember).
 *
 *     AppExt.setTitle("Reports");
 *     AppExt.onTheme((theme) => document.documentElement.dataset.theme = theme);
 *
 * Three places an extension can run, and one script for all of them:
 *
 *   1. In the host app on a phone: a JavaScript channel called AppExtBridge.
 *   2. In the host's web app: the extension sits in an iframe and talks to its parent with
 *      postMessage. Only the web app's origins (APPEXT_APP_ORIGINS) are listened to and spoken to.
 *   3. In an ordinary browser tab: no app around. Every call is a quiet no-op that returns false -
 *      an extension must work there - and a bar with a way back to the web app is drawn on top.
 *
 * Deliberately NO data commands: nothing here returns a token, a user name or anything else from
 * the app. Data flows through the extension's own backend.
 *
 * Wire format (the app accepts exactly these; unknown commands are ignored):
 *   extension -> app   {command: "close"}
 *                      {command: "setTitle", title: "..."}
 *                      {command: "openExternal", url: "https://..."}
 *                      {command: "ready"}          (iframe only: "I am up, tell me theme and language")
 *                      sent as AppExtBridge.postMessage(JSON.stringify(...)) in the phone app and as
 *                      parent.postMessage(JSON.stringify(...), appOrigin) in the web app
 *   app -> extension   window event "appext:theme"    (detail: "light" | "dark")
 *                      window event "appext:language" (detail: "de", "en", ...)
 *                      window.AppExtState holds the latest values, for a script that loads after the
 *                      event fired. In the web app the same arrives as
 *                      {appext: "event", type: "theme"|"language", value: "..."} through postMessage.
 *                      (AppExt.emit(type, value) is the same thing as a direct call.)
 */
(function () {
  "use strict";
  if (window.AppExt) return;

  var CONFIG = window.__APPEXT_CONFIG__ || {};
  var APP_ORIGINS = Array.isArray(CONFIG.appOrigins) ? CONFIG.appOrigins : [];
  // What the host app adds to the user agent of its WebView: `<Name>-App-WebView/<version>`.
  var MARKER = typeof CONFIG.appMarker === "string" && CONFIG.appMarker ? CONFIG.appMarker : "-App-WebView/";

  var EVENTS = { theme: [], language: [] };
  var last = {};

  function channel() {
    // Looked up at call time: the app may inject the channel after this script ran.
    var c = window.AppExtBridge;
    return c && typeof c.postMessage === "function" ? c : null;
  }

  /** Is this page framed by something else? (Cross-origin parents answer with an exception: yes.) */
  function framed() {
    try {
      return window.parent !== window && !!window.parent;
    } catch (_) {
      return true;
    }
  }

  /** Embedded in the host's web app: framed, and there is a web app configured to be framed by. */
  function inWebApp() {
    return channel() === null && framed() && APP_ORIGINS.length > 0;
  }

  /** Inside the phone app's WebView (the channel, or the marker the app puts in the user agent). */
  function inPhoneApp() {
    var ua = typeof navigator !== "undefined" && navigator.userAgent ? navigator.userAgent : "";
    return channel() !== null || ua.indexOf(MARKER) >= 0;
  }

  function send(message) {
    var c = channel();
    if (c) {
      try {
        c.postMessage(JSON.stringify(message));
        return true;
      } catch (_) {
        return false;
      }
    }
    if (inWebApp()) {
      var text = JSON.stringify(message);
      // The target origin is the app's, never "*": whatever else frames us gets nothing.
      APP_ORIGINS.forEach(function (origin) {
        try {
          window.parent.postMessage(text, origin);
        } catch (_) {}
      });
      return true;
    }
    return false;
  }

  function subscribe(type, callback) {
    if (typeof callback !== "function") return function () {};
    EVENTS[type].push(callback);
    if (Object.prototype.hasOwnProperty.call(last, type)) {
      try {
        callback(last[type]);
      } catch (_) {}
    }
    return function () {
      var i = EVENTS[type].indexOf(callback);
      if (i >= 0) EVENTS[type].splice(i, 1);
    };
  }

  function apply(type, value) {
    if (!Object.prototype.hasOwnProperty.call(EVENTS, type) || typeof value !== "string") return;
    last[type] = value;
    EVENTS[type].slice().forEach(function (callback) {
      try {
        callback(value);
      } catch (_) {}
    });
  }

  var AppExt = {
    /** True inside the host app - the phone app's WebView or the web app's frame. */
    get inApp() {
      return inPhoneApp() || inWebApp();
    },
    /** Close the extension screen. */
    close: function () {
      return send({ command: "close" });
    },
    /** Set the title of the app bar above the extension. */
    setTitle: function (title) {
      return send({ command: "setTitle", title: String(title) });
    },
    /** Ask the host app to open an https URL in the system browser (the host app decides which addresses it accepts). */
    openExternal: function (url) {
      return send({ command: "openExternal", url: String(url) });
    },
    /** Called with "light" or "dark", now (if known) and on every change. Returns an unsubscribe function. */
    onTheme: function (callback) {
      return subscribe("theme", callback);
    },
    /** Called with a language code such as "de" or "en", now (if known) and on every change. */
    onLanguage: function (callback) {
      return subscribe("language", callback);
    },
    /** Used by the app to push events. Not for extension code. */
    emit: apply,
  };

  Object.defineProperty(window, "AppExt", { value: AppExt, enumerable: true });

  // What the app pushes: a CustomEvent per change plus window.AppExtState with
  // the latest values (an event that fired before this script loaded is not lost).
  if (typeof window.addEventListener === "function") {
    Object.keys(EVENTS).forEach(function (type) {
      window.addEventListener("appext:" + type, function (event) {
        apply(type, event && event.detail);
      });
    });

    // The web app's way of the same: a message from the parent frame, and only from it.
    window.addEventListener("message", function (event) {
      if (!inWebApp() || event.source !== window.parent || APP_ORIGINS.indexOf(event.origin) < 0) return;
      var data;
      try {
        data = typeof event.data === "string" ? JSON.parse(event.data) : null;
      } catch (_) {
        return;
      }
      if (!data || data.appext !== "event" || !Object.prototype.hasOwnProperty.call(EVENTS, data.type)) return;
      if (typeof data.value !== "string") return;
      var state = (window.AppExtState = window.AppExtState || {});
      state[data.type] = data.value;
      // Through the same door as the phone app's events: whoever listens for the window event gets it too.
      if (typeof window.CustomEvent === "function" && typeof window.dispatchEvent === "function") {
        window.dispatchEvent(new window.CustomEvent("appext:" + data.type, { detail: data.value }));
      } else {
        apply(data.type, data.value);
      }
    });
  }
  var state = window.AppExtState;
  if (state && typeof state === "object") {
    Object.keys(EVENTS).forEach(function (type) {
      apply(type, state[type]);
    });
  }

  if (typeof document === "undefined") return;

  // --- once the page is there ----------------------------------------------------------------

  function whenReady(callback) {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", callback);
    else callback();
  }

  // In the web app's frame: say that the page is up. The web app answers with theme and language,
  // and learns from this that the frame is not an error page of the browser (those cannot speak).
  if (inWebApp()) whenReady(function () { send({ command: "ready" }); });

  // In a plain browser tab: the bar that leads back to the web app.
  if (!inPhoneApp() && !framed() && APP_ORIGINS.length > 0) whenReady(showBar);

  function languageTag() {
    return ((document.documentElement.lang || (typeof navigator !== "undefined" && navigator.language) || "en") + "").replace("_", "-");
  }

  function language() {
    return languageTag().toLowerCase().split("-")[0];
  }

  function goBack() {
    // A tab the web app opened can simply be closed; otherwise lead to the web app itself.
    if (window.opener) {
      try {
        window.close();
        return;
      } catch (_) {}
    }
    window.location.assign(APP_ORIGINS[0] + "/");
  }

  function showBar() {
    if (!document.body || document.getElementById("appext-shell")) return;
    var meta = document.querySelector('meta[name="appext-shell"]');
    if (meta && meta.getAttribute("content") === "off") return; // the page brings its own navigation

    var lang = language();
    var name = (CONFIG.nameLocalized && CONFIG.nameLocalized[lang]) || CONFIG.name || document.title || "";
    // The platform names its app and may translate the label (`APPEXT_APP_NAME`, `APPEXT_APP_BACK_LABELS`);
    // "{app}" in a label stands for the name.
    var appName = typeof CONFIG.appName === "string" ? CONFIG.appName : "";
    var labels = CONFIG.backLabels && typeof CONFIG.backLabels === "object" ? CONFIG.backLabels : {};
    // The page's full language tag first ("pt-BR"), then its language ("pt"), then English.
    var template = labels[languageTag()] || labels[lang] || labels.en || "Back to {app}";
    var label = String(template).split("{app}").join(appName || "the app");
    var accent = typeof CONFIG.appAccent === "string" && /^#[0-9a-fA-F]{3,8}$/.test(CONFIG.appAccent) ? CONFIG.appAccent : "#2563eb";
    var dark = typeof window.matchMedia === "function" && window.matchMedia("(prefers-color-scheme: dark)").matches;
    var ink = dark ? "#e8efe9" : "#1a2b22";

    // Styles through the CSSOM (`element.style`), not a <style> element or an attribute: the
    // extension's CSP is default-src 'self', which forbids inline styles but not this. A shadow root
    // keeps the page's own CSS out of the bar.
    var host = document.createElement("div");
    host.id = "appext-shell";
    host.setAttribute("role", "banner");
    host.style.cssText = "position:sticky;top:0;left:0;right:0;z-index:2147483647;display:block;";
    var root = host.attachShadow ? host.attachShadow({ mode: "open" }) : host;

    var bar = document.createElement("div");
    bar.style.cssText =
      "box-sizing:border-box;display:flex;align-items:center;gap:6px;height:52px;padding:0 14px 0 6px;" +
      "font:600 17px/1 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;" +
      "background:" + (dark ? "#14201a" : "#ffffff") + ";color:" + ink + ";" +
      "border-bottom:1px solid " + (dark ? "#2a3a31" : "#e3e8e5") + ";";

    var button = document.createElement("button");
    button.type = "button";
    button.setAttribute("aria-label", label);
    button.title = label;
    button.style.cssText =
      "box-sizing:border-box;width:40px;height:40px;display:inline-flex;align-items:center;justify-content:center;" +
      "border:0;border-radius:20px;background:transparent;color:" + accent + ";cursor:pointer;padding:0;";
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("width", "24");
    svg.setAttribute("height", "24");
    svg.setAttribute("aria-hidden", "true");
    var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "M15 5l-7 7 7 7");
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "2.4");
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("stroke-linejoin", "round");
    svg.appendChild(path);
    button.appendChild(svg);
    button.addEventListener("click", goBack);
    button.addEventListener("mouseenter", function () { button.style.background = dark ? "#1f2f26" : "#eef3ef"; });
    button.addEventListener("mouseleave", function () { button.style.background = "transparent"; });

    var title = document.createElement("span");
    title.textContent = name;
    title.style.cssText = "overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";

    bar.appendChild(button);
    bar.appendChild(title);
    if (appName) {
      var badge = document.createElement("span");
      badge.textContent = appName;
      badge.style.cssText =
        "margin-left:auto;font-weight:700;font-size:13px;letter-spacing:.08em;color:" + accent + ";";
      bar.appendChild(badge);
    }
    root.appendChild(bar);
    document.body.insertBefore(host, document.body.firstChild);
  }
})();
