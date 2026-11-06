// Where the InvariantDAST API lives, plus small helpers shared by every page.
(function () {
  // - Local development: the FastAPI server on port 8000.
  // - Deployed (Vercel): "" = same origin; vercel.json forwards /api/* to the hosted backend,
  //   so the session cookie stays first-party.
  var local = ["localhost", "127.0.0.1", ""].indexOf(location.hostname) !== -1;
  if (window.INVARIANT_API == null) window.INVARIANT_API = local ? "http://localhost:8000" : "";

  // The hosted backend runs on a free plan that sleeps when idle and takes up to ~1 minute
  // to wake. While it wakes, the proxy answers 502/503/504 (or the request fails outright).
  const WAKING_STATUSES = [502, 503, 504];
  const WAKE_LIMIT_MS = 120000;
  const WAKING_MESSAGE = "The server is waking up (free hosting can take up to a minute). Please try again in a few seconds.";
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // fetch wrapper: sends the session cookie, turns FastAPI error bodies into readable messages,
  // and quietly retries read-only (GET) requests while the server wakes. Requests that change
  // data (POST/DELETE) are never retried automatically.
  window.invariantApi = async function (path, options = {}) {
    const retry = (options.method || "GET").toUpperCase() === "GET";
    const started = Date.now();
    let delay = 2000;
    for (;;) {
      let res = null;
      try {
        // Only label requests that carry a JSON body: a bare GET then needs no CORS preflight.
        const headers = options.body != null ? { "Content-Type": "application/json" } : {};
        res = await fetch(window.INVARIANT_API + path, {
          credentials: "include",
          ...options,
          headers: { ...headers, ...(options.headers || {}) },
        });
      } catch (_) { /* network error or server still waking */ }
      const waking = !res || WAKING_STATUSES.includes(res.status);
      if (waking && retry && Date.now() - started < WAKE_LIMIT_MS) {
        await sleep(delay);
        delay = Math.min(delay * 1.5, 8000);
        continue;
      }
      if (!res) {
        const err = new Error("Couldn't reach the server. Please try again in a moment.");
        err.status = 0;
        throw err;
      }
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        let msg = data.detail;
        if (Array.isArray(msg)) msg = msg[0] && msg[0].msg && msg[0].msg.replace(/^Value error, /, "");
        if (!msg && WAKING_STATUSES.includes(res.status)) msg = WAKING_MESSAGE;
        const err = new Error(msg || `Request failed (${res.status})`);
        err.status = res.status;
        throw err;
      }
      return data;
    }
  };
  window.invariantApi.WAKING_STATUSES = WAKING_STATUSES;

  // Wake the backend. Resolves with the session state once it answers; rejects after
  // WAKE_LIMIT_MS. onWaiting() runs (once) if the server turns out to be asleep.
  let pending = null;
  let asleep = false;
  let awakeAt = 0;
  const listeners = [];
  window.invariantWake = function (onWaiting) {
    // The server may have gone back to sleep if the page sat idle: re-check after a minute.
    if (pending && awakeAt && Date.now() - awakeAt > 60000) { pending = null; awakeAt = 0; }
    if (onWaiting) {
      if (asleep) onWaiting(); else listeners.push(onWaiting);
    }
    if (!pending) {
      pending = (async () => {
        const started = Date.now();
        let delay = 2000;
        for (;;) {
          try {
            const res = await fetch(window.INVARIANT_API + "/api/auth/session", { credentials: "include" });
            if (!WAKING_STATUSES.includes(res.status)) return res.json().catch(() => ({}));
          } catch (_) { /* still waking */ }
          if (!asleep) { asleep = true; listeners.splice(0).forEach((f) => f()); }
          if (Date.now() - started > WAKE_LIMIT_MS) throw new Error("The server is not responding. Please try again later.");
          await sleep(delay);
          delay = Math.min(delay * 1.5, 8000);
        }
      })();
      pending.then(() => { asleep = false; awakeAt = Date.now(); }, () => { pending = null; asleep = false; });
    }
    return pending;
  };
})();
