(() => {
  "use strict";
  const api = window.invariantApi;
  const forms = { login: document.getElementById("form-login"), signup: document.getElementById("form-signup") };
  const tabs = Array.from(document.querySelectorAll(".auth-tab"));

  // Wake the backend right away (it sleeps when idle on free hosting), and go straight to the
  // dashboard if already signed in. While it wakes, show a banner instead of failing.
  const banner = document.getElementById("wakeBanner"), bannerText = document.getElementById("wakeText");
  const ready = window.invariantWake(() => { banner.hidden = false; });
  ready
    .then((s) => {
      banner.hidden = true;
      if (s && s.signed_in) location.replace("dashboard.html");
    })
    .catch((err) => {
      banner.hidden = false;
      banner.classList.add("error");
      bannerText.textContent = err.message;
    });

  function setMode(mode, focus = true) {
    tabs.forEach((t) => {
      const on = t.dataset.mode === mode;
      t.classList.toggle("active", on);
      t.setAttribute("aria-selected", String(on));
    });
    Object.entries(forms).forEach(([m, f]) => { f.hidden = m !== mode; });
    history.replaceState(null, "", mode === "signup" ? "#signup" : "#login");
    if (focus) forms[mode].querySelector("input").focus();
  }
  document.querySelectorAll("[data-mode]").forEach((el) =>
    el.addEventListener("click", (e) => { e.preventDefault(); setMode(el.dataset.mode); }));
  setMode(location.hash === "#signup" ? "signup" : "login", false);

  // Show / hide password
  document.querySelectorAll(".pw-toggle").forEach((btn) => btn.addEventListener("click", () => {
    const input = btn.previousElementSibling;
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    btn.textContent = show ? "Hide" : "Show";
    btn.setAttribute("aria-label", show ? "Hide password" : "Show password");
  }));

  // Password strength meter (sign-up)
  const pw = forms.signup.elements.password, meter = forms.signup.querySelector(".pw-meter");
  pw.addEventListener("input", () => {
    const v = pw.value;
    let score = 0;
    if (v.length >= 10) score++;
    if (v.length >= 14) score++;
    if (/[a-z]/i.test(v) && /\d/.test(v)) score++;
    if (/[^a-z0-9]/i.test(v)) score++;
    meter.dataset.score = v ? String(Math.min(score, 4)) : "";
  });

  function validate(form, mode) {
    const f = form.elements;
    if (mode === "signup" && !f.name.value.trim()) return [f.name, "Enter your name."];
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(f.email.value.trim())) return [f.email, "Enter a valid email address."];
    if (!f.password.value) return [f.password, "Enter your password."];
    if (mode === "signup") {
      if (f.password.value.length < 10) return [f.password, "Password must be at least 10 characters."];
      if (/^[a-z]+$/i.test(f.password.value) || /^\d+$/.test(f.password.value)) return [f.password, "Mix letters with numbers or symbols."];
    }
    return null;
  }

  Object.entries(forms).forEach(([mode, form]) => {
    const errorEl = form.querySelector(".auth-error");
    const submit = form.querySelector("button[type=submit]");
    const label = submit.textContent;
    form.addEventListener("input", (e) => { e.target.removeAttribute("aria-invalid"); errorEl.textContent = ""; });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      form.querySelectorAll("[aria-invalid]").forEach((el) => el.removeAttribute("aria-invalid"));
      const problem = validate(form, mode);
      if (problem) {
        problem[0].setAttribute("aria-invalid", "true");
        problem[0].focus();
        errorEl.textContent = problem[1];
        return;
      }
      const f = form.elements;
      const body = mode === "signup"
        ? { name: f.name.value.trim(), company: f.company.value.trim() || null, email: f.email.value.trim(), password: f.password.value }
        : { email: f.email.value.trim(), password: f.password.value };

      submit.disabled = true;
      errorEl.textContent = "";
      try {
        // Make sure the server is awake before sending the form, so it isn't lost to a 502.
        submit.textContent = "Waking up the server…";
        await window.invariantWake(() => { banner.hidden = false; });
        banner.hidden = true;
        submit.textContent = mode === "signup" ? "Creating account…" : "Signing in…";
        await api(`/api/auth/${mode}`, { method: "POST", body: JSON.stringify(body) });
        location.replace("dashboard.html");
      } catch (err) {
        errorEl.textContent = err.message;
        if (mode === "login" && err.status === 401) { f.password.value = ""; f.password.focus(); }
        submit.disabled = false;
        submit.textContent = label;
      }
    });
  });
})();
