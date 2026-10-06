// Progressive enhancement only. Every feature of the app works with this file blocked.
(function () {
  "use strict";

  var live = document.getElementById("live-region");

  function announce(text) {
    if (!live) return;
    // Clearing first makes screen readers re-announce identical text.
    live.textContent = "";
    window.setTimeout(function () { live.textContent = text; }, 50);
  }

  // After a page load, move focus to the outcome (error, notice, or results heading)
  // so a screen reader reads it first instead of the page header.
  var target = document.querySelector("#error[data-focus-on-load]") ||
    document.querySelector("[data-focus-on-load]");
  if (target) target.focus();

  // Forms: announce slow actions and ignore repeat submissions.
  // aria-disabled is used instead of disabled so the pressed button's value is still sent.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.dataset.submitting === "true") {
      event.preventDefault();
      return;
    }
    if (!form.dataset.busyText && !form.hasAttribute("data-submit-once")) return;
    form.dataset.submitting = "true";
    form.setAttribute("aria-busy", "true");
    Array.prototype.forEach.call(form.querySelectorAll("button"), function (button) {
      button.setAttribute("aria-disabled", "true");
    });
    if (form.dataset.busyText) announce(form.dataset.busyText);
  });

  // A page restored from the back/forward cache must not stay locked.
  window.addEventListener("pageshow", function (event) {
    if (!event.persisted) return;
    Array.prototype.forEach.call(document.querySelectorAll("form[data-submitting]"), function (form) {
      delete form.dataset.submitting;
      form.removeAttribute("aria-busy");
      Array.prototype.forEach.call(form.querySelectorAll("button"), function (button) {
        button.removeAttribute("aria-disabled");
      });
    });
  });

  // Inbox: announce newly arrived messages without changing the page under the reader.
  var banner = document.getElementById("new-messages");
  if (banner && window.fetch) {
    var known = parseInt(banner.dataset.unread || "0", 10);
    var text = document.getElementById("new-messages-text");
    window.setInterval(function () {
      if (document.hidden) return;
      fetch("/inbox/status", { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(function (data) {
          if (!data || typeof data.unread !== "number" || data.unread <= known) return;
          var added = data.unread - known;
          known = data.unread;
          text.textContent = added === 1 ? "1 new message has arrived." : added + " new messages have arrived.";
          banner.hidden = false;
        })
        .catch(function () { /* offline or signed out: stay quiet */ });
    }, 60000);
  }
})();
