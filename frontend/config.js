// Where the InvariantDAST API lives.
// - Local development: the FastAPI server on port 8000.
// - Deployed (Vercel): "" = same origin; vercel.json forwards /api/* to the hosted backend,
//   so the session cookie stays first-party.
(function () {
  var local = ["localhost", "127.0.0.1", ""].indexOf(location.hostname) !== -1;
  if (window.INVARIANT_API == null) window.INVARIANT_API = local ? "http://localhost:8000" : "";
})();

// Small fetch helper shared by the pages: sends the session cookie and
// turns FastAPI error bodies into readable messages.
window.invariantApi = async function (path, options = {}) {
  let res;
  try {
    // Only label requests that carry a JSON body: a bare GET then needs no CORS preflight.
    const headers = options.body != null ? { "Content-Type": "application/json" } : {};
    res = await fetch(window.INVARIANT_API + path, {
      credentials: "include",
      ...options,
      headers: { ...headers, ...(options.headers || {}) },
    });
  } catch (_) {
    const err = new Error("Couldn't reach the server. Please try again in a moment.");
    err.status = 0;
    throw err;
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    let msg = data.detail;
    if (Array.isArray(msg)) msg = msg[0] && msg[0].msg && msg[0].msg.replace(/^Value error, /, "");
    const err = new Error(msg || `Request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
};
