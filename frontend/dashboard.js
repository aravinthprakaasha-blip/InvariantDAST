(() => {
  "use strict";
  const api = window.invariantApi;
  const API = window.INVARIANT_API;
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtNum = (n) => Number(n || 0).toLocaleString();
  const fmtDur = (ms) => (ms == null ? "—" : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`);
  const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—");
  const ACTIVE = new Set(["queued", "running"]);
  const SEV_ORDER = ["critical", "high", "medium", "low"];

  let stream = null;          // EventSource for the run in the panel
  let panelRunId = null;
  let lastEventId = 0;
  let allowed = [];           // scope allowlist from the server ([] = any URL, local dev)

  /* ---------------- session ---------------- */
  function renderUser(user) {
    const initials = user.name.split(/\s+/).filter(Boolean).slice(0, 2).map((p) => p[0].toUpperCase()).join("");
    $("userInitials").textContent = initials || "?";
    $("userEmail").textContent = user.email;
    $("userName").textContent = user.name.split(/\s+/)[0];
    $("workspaceName").textContent = (user.company || user.name) + " · workspace";
  }

  function handleAuthError(err) {
    if (err.status === 401) { location.replace("login.html"); return true; }
    return false;
  }

  $("logoutBtn").addEventListener("click", async () => {
    $("logoutBtn").disabled = true;
    if (stream) stream.close();
    try { await api("/api/auth/logout", { method: "POST" }); } catch (_) { /* already signed out */ }
    location.replace("login.html");
  });

  /* ---------------- overview ---------------- */
  async function loadOverview() {
    const o = await api("/api/overview");
    $("ovTargets").textContent = fmtNum(o.targets);
    $("ovRuns").textContent = fmtNum(o.runs);
    $("ovFindings").textContent = fmtNum(o.open_findings);
    $("ovFindings").className = o.open_findings ? "c-crit" : "c-ok";
    $("ovSev").textContent = o.open_findings
      ? SEV_ORDER.filter((s) => o.by_severity[s]).map((s) => `${o.by_severity[s]} ${s}`).join(" · ")
      : o.runs ? "all checks passing" : "no scans yet";
    $("ovRequests").textContent = fmtNum(o.requests);
    $("ovComp").textContent = fmtNum(o.compensations);
  }

  /* ---------------- targets ---------------- */
  function statusPill(status) {
    const cls = { completed: "ok", running: "pending", queued: "pending", failed: "bad", interrupted: "bad" }[status] || "";
    return `<span class="pill ${cls}">${esc(status)}</span>`;
  }

  function runResult(run) {
    if (!run) return "";
    if (ACTIVE.has(run.status)) return `${run.checks_done}/${run.checks_total} checks`;
    const f = run.stats.findings || {};
    const total = SEV_ORDER.reduce((a, s) => a + (f[s] || 0), 0);
    if (run.status !== "completed") return `<span class="c-crit">${esc(run.error || run.status)}</span>`;
    return total ? `<span class="c-crit">${total} finding${total > 1 ? "s" : ""}</span>` : `<span class="c-ok">all ${run.stats.passed} checks passed</span>`;
  }

  async function loadTargets() {
    const { targets } = await api("/api/targets");
    const list = $("targetList");
    if (!targets.length) {
      list.innerHTML = `<div class="dash-card empty">
        <b>No targets yet</b>
        <span>${allowed.length
          ? `Click <b>+ Add target</b> and pick one of the approved demo APIs: ${allowed.map((u) => `<code>${esc(u)}</code>`).join(" ")}`
          : "Add a travel API you're authorised to test. To try it locally, point it at the sample API on <code>http://localhost:8001</code>."}</span>
      </div>`;
      return targets;
    }
    list.innerHTML = targets.map((t) => {
      const run = t.last_run;
      const busy = t.busy || (run && ACTIVE.has(run.status));
      return `<article class="dash-card target" data-id="${t.id}">
        <div class="target-main">
          <h3>${esc(t.name)}</h3>
          <code class="target-url">${esc(t.base_url)}</code>
          <dl class="target-meta">
            <div><dt>Personas</dt><dd>${esc(t.auth_header)}: ${esc(t.persona_a)} / ${esc(t.persona_b)}</dd></div>
            <div><dt>Route</dt><dd>${esc(t.origin)} → ${esc(t.destination)}</dd></div>
            <div><dt>Last scan</dt><dd>${run ? `${statusPill(run.status)} ${runResult(run)}` : "never"}</dd></div>
          </dl>
        </div>
        <div class="target-actions">
          <button class="btn btn-sm" data-action="run" ${busy ? "disabled" : ""}>${busy ? "Scanning…" : "Run scan"}</button>
          ${run ? `<button class="btn btn-sm btn-ghost" data-action="view" data-run="${run.id}">View last scan</button>` : ""}
          <button class="icon-btn" data-action="delete" aria-label="Delete target ${esc(t.name)}" ${busy ? "disabled" : ""}>🗑</button>
        </div>
      </article>`;
    }).join("");
    return targets;
  }

  $("targetList").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-action]");
    if (!btn) return;
    const card = btn.closest(".target");
    const id = card.dataset.id;
    const action = btn.dataset.action;
    if (action === "view") return openRun(Number(btn.dataset.run));
    if (action === "delete") {
      const name = card.querySelector("h3").textContent;
      if (!confirm(`Delete target "${name}" and all of its scan history?`)) return;
      btn.disabled = true;
      try { await api(`/api/targets/${id}`, { method: "DELETE" }); }
      catch (err) { if (!handleAuthError(err)) alert(err.message); }
      if (panelRunId) closePanel();
      return refreshAll();
    }
    if (action === "run") {
      btn.disabled = true;
      btn.textContent = "Starting…";
      try {
        const { run_id } = await api(`/api/targets/${id}/runs`, { method: "POST" });
        updateTargetCard({ target_id: id, status: "queued", checks_done: 0, checks_total: 12, stats: {} });
        await loadTargets();
        openRun(run_id);
      } catch (err) {
        if (!handleAuthError(err)) alert(err.message);
        loadTargets();
      }
    }
  });

  /* add-target form */
  const form = $("targetForm");
  $("addTargetBtn").addEventListener("click", () => {
    form.hidden = !form.hidden;
    if (!form.hidden) form.elements.name.focus();
  });
  function renderAllowedHint() {
    const hint = $("allowedHint");
    hint.hidden = !allowed.length;
    hint.innerHTML = allowed.length
      ? `<span>This server only scans approved targets:</span>` +
        allowed.map((u) => `<button type="button" class="chip" data-fill="${esc(u)}">${esc(u)}</button>`).join("")
      : "";
  }
  $("allowedHint").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-fill]");
    if (!b) return;
    const f = form.elements;
    f.base_url.value = b.dataset.fill;
    if (!f.name.value.trim()) f.name.value = /vuln/i.test(b.dataset.fill) ? "Vulnerable demo API" : "Sample travel API";
    if (!f.persona_a.value.trim()) f.persona_a.value = "AGY-A";
    if (!f.persona_b.value.trim()) f.persona_b.value = "AGY-B";
    if (!f.origin.value.trim()) f.origin.value = "CJB";
    if (!f.destination.value.trim()) f.destination.value = "DEL";
    f.authorized.focus();
  });
  $("targetCancel").addEventListener("click", () => { form.hidden = true; form.reset(); $("targetError").textContent = ""; });
  form.addEventListener("input", (e) => { e.target.removeAttribute("aria-invalid"); $("targetError").textContent = ""; });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = form.elements;
    const required = ["name", "base_url", "auth_header", "persona_a", "persona_b", "origin", "destination"];
    const missing = required.find((k) => !f[k].value.trim());
    const fail = (el, msg) => { if (el) { el.setAttribute("aria-invalid", "true"); el.focus(); } $("targetError").textContent = msg; };
    if (missing) return fail(f[missing], "Fill in every field.");
    if (!/^https?:\/\/\S+$/i.test(f.base_url.value.trim())) return fail(f.base_url, "Base URL must start with http:// or https://");
    if (!/^[A-Za-z0-9-]+$/.test(f.auth_header.value.trim())) return fail(f.auth_header, "Tenant header can only use letters, numbers and hyphens (for example X-Agency-ID).");
    if (f.persona_a.value.trim() === f.persona_b.value.trim()) return fail(f.persona_b, "Agency A and Agency B must be different tenants.");
    if (!f.authorized.checked) return fail(f.authorized, "Confirm you're authorised to test this API.");
    const body = Object.fromEntries(required.map((k) => [k, f[k].value.trim()]));
    body.authorized = true;
    const submit = form.querySelector("button[type=submit]");
    submit.disabled = true;
    try {
      await api("/api/targets", { method: "POST", body: JSON.stringify(body) });
      $("targetError").textContent = "";
      form.reset();
      form.hidden = true;
      await refreshAll();
    } catch (err) {
      if (!handleAuthError(err)) fail(null, err.message);
    } finally {
      submit.disabled = false;
    }
  });

  /* ---------------- findings ---------------- */
  function findingHTML(f, showTarget) {
    const evidence = JSON.stringify(f.evidence, null, 2);
    return `<details class="finding sev-${esc(f.severity)}">
      <summary>
        <span class="sev">${esc(f.severity)}</span>
        <span class="f-title">${esc(f.title)}</span>
        <code class="f-ep">${esc(f.endpoint)}</code>
        ${showTarget ? `<span class="f-target">${esc(f.target_name)}</span>` : ""}
      </summary>
      <div class="f-body">
        <p>${esc(f.description)}</p>
        <div class="f-cols">
          <div>
            <div class="f-label">Reproduce <button class="copy" data-copy>Copy</button></div>
            <pre class="code small">${esc(f.repro)}</pre>
          </div>
          <div>
            <div class="f-label">Evidence</div>
            <pre class="code small">${esc(evidence)}</pre>
          </div>
        </div>
        <small class="muted">Scan #${f.run_id} · check <code>${esc(f.check_id)}</code> · ${fmtTime(f.created_at)}</small>
      </div>
    </details>`;
  }

  document.addEventListener("click", async (e) => {
    const b = e.target.closest("button[data-copy]");
    if (!b) return;
    e.preventDefault();
    // The <pre> is a sibling of the label row, inside the same column.
    const pre = b.closest(".f-cols > div")?.querySelector("pre");
    if (!pre) return;
    const text = pre.textContent;
    let ok = false;
    try { await navigator.clipboard.writeText(text); ok = true; }
    catch (_) {
      // Fallback for browsers/contexts where the async clipboard API is unavailable or blocked.
      const ta = document.createElement("textarea");
      ta.value = text; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
      ta.remove();
    }
    b.textContent = ok ? "Copied" : "Copy failed";
    setTimeout(() => (b.textContent = "Copy"), 1500);
  });

  async function loadFindings() {
    const { findings } = await api("/api/findings");
    $("findingList").innerHTML = findings.length
      ? findings.map((f) => findingHTML(f, true)).join("")
      : `<div class="dash-card empty"><b>No open findings</b><span>Only findings proven by a real request appear here — nothing is guessed.</span></div>`;
  }

  /* ---------------- run history ---------------- */
  async function loadRuns() {
    const { runs } = await api("/api/runs?limit=25");
    $("runList").innerHTML = runs.length
      ? runs.map((r) => `<tr data-run="${r.id}" tabindex="0">
          <td class="mono">#${r.id}</td>
          <td>${esc(r.target_name)}</td>
          <td>${statusPill(r.status)}</td>
          <td>${runResult(r)}</td>
          <td class="mono">${fmtNum(r.stats.requests)}</td>
          <td class="mono">${fmtDur(r.stats.duration_ms)}</td>
          <td class="mono">${fmtTime(r.started_at || r.created_at)}</td>
        </tr>`).join("")
      : `<tr><td colspan="7" class="muted center">No scans yet.</td></tr>`;
  }
  $("runList").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-run]"); if (tr) openRun(Number(tr.dataset.run)); });
  $("runList").addEventListener("keydown", (e) => { const tr = e.target.closest("tr[data-run]"); if (tr && e.key === "Enter") openRun(Number(tr.dataset.run)); });

  /* ---------------- run panel (live) ---------------- */
  function appendLog(ev) {
    const log = $("runLog");
    const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    const li = document.createElement("li");
    li.className = "lv-" + ev.level;
    li.innerHTML = `<time>${esc(new Date(ev.ts).toLocaleTimeString(undefined, { hour12: false }))}</time><span></span>`;
    li.lastChild.textContent = ev.message;
    log.appendChild(li);
    if (nearBottom) log.scrollTop = log.scrollHeight;
  }

  function renderRunState(run) {
    const s = run.stats || {};
    $("runStatus").outerHTML = statusPill(run.status).replace("<span", '<span id="runStatus"');
    const pct = run.checks_total ? Math.round((run.checks_done / run.checks_total) * 100) : 0;
    $("runBar").style.setProperty("--w", (run.status === "completed" ? 100 : pct) + "%");
    $("runBar").parentElement.classList.toggle("live", ACTIVE.has(run.status));
    $("rsChecks").textContent = `${run.checks_done} / ${run.checks_total}`;
    $("rsPassed").textContent = fmtNum(s.passed);
    $("rsFailed").textContent = fmtNum(s.failed);
    $("rsReq").textContent = fmtNum(s.requests);
    $("rsComp").textContent = fmtNum(s.compensations);
    $("rsDur").textContent = fmtDur(s.duration_ms);
    $("runError").hidden = !run.error;
    $("runError").textContent = run.error || "";
  }

  function renderRunDetail(run, findings) {
    $("runTitle").textContent = `Scan #${run.id} · ${run.target_name}`;
    $("runMeta").textContent = `${run.base_url} · started ${fmtTime(run.started_at || run.created_at)}`;
    renderRunState(run);
    const surf = run.surface || [];
    $("runSurfaceWrap").hidden = !surf.length;
    $("runSurfaceCount").textContent = surf.length;
    $("runSurface").innerHTML = surf.map((s) => `<tr>
      <td class="mono">${esc(s.method)}</td><td class="mono">${esc(s.path)}</td>
      <td>${s.documented ? '<span class="c-ok">yes</span>' : '<span class="c-warn">no — undocumented</span>'}</td>
      <td>${s.in_contract ? "yes" : "—"}</td></tr>`).join("");
    const done = !ACTIVE.has(run.status);
    $("runFindingsWrap").hidden = !done;
    if (done) {
      $("runFindings").innerHTML = findings.length
        ? findings.map((f) => findingHTML(f, false)).join("")
        : `<div class="empty small"><b>No findings</b><span>Every check in this scan passed or was skipped.</span></div>`;
    }
  }

  async function openRun(runId) {
    if (stream) { stream.close(); stream = null; }
    panelRunId = runId;
    lastEventId = 0;
    $("runLog").innerHTML = "";
    $("runPanel").hidden = false;
    $("runPanel").scrollIntoView({ behavior: "smooth", block: "start" });
    try {
      const { run, findings, events } = await api(`/api/runs/${runId}`);
      if (panelRunId !== runId) return;
      events.forEach((ev) => { appendLog(ev); lastEventId = ev.id; });
      renderRunDetail(run, findings);
      if (ACTIVE.has(run.status)) connectStream(runId, run);
    } catch (err) {
      if (!handleAuthError(err)) { $("runError").hidden = false; $("runError").textContent = err.message; }
    }
  }

  function connectStream(runId, baseRun) {
    const es = new EventSource(`${API}/api/runs/${runId}/stream?after=${lastEventId}`, { withCredentials: true });
    stream = es;
    es.addEventListener("log", (m) => {
      const ev = JSON.parse(m.data);
      if (ev.id <= lastEventId) return;
      lastEventId = ev.id;
      appendLog(ev);
    });
    let lastStatus = baseRun.status;
    es.addEventListener("run", (m) => {
      const run = { ...baseRun, ...JSON.parse(m.data) };
      renderRunState(run);
      if (run.status !== lastStatus) { lastStatus = run.status; loadTargets().catch(() => {}); }
      else updateTargetCard(run);
    });
    es.addEventListener("done", async () => {
      es.close();
      if (stream === es) stream = null;
      if (panelRunId !== runId) return;
      const { run, findings } = await api(`/api/runs/${runId}`);
      renderRunDetail(run, findings);
      refreshAll();
    });
    es.onerror = () => {
      // Connection dropped (server reload, network). Reconnect from the last event we saw.
      es.close();
      if (stream !== es || panelRunId !== runId) return;
      stream = null;
      setTimeout(() => { if (panelRunId === runId && !stream) connectStream(runId, baseRun); }, 2000);
    };
  }

  // Keep the target card's "Last scan" line in step with the live stream.
  function updateTargetCard(run) {
    const dd = document.querySelector(`.target[data-id="${run.target_id}"] .target-meta div:last-child dd`);
    if (dd) dd.innerHTML = `${statusPill(run.status)} ${runResult(run)}`;
  }

  function closePanel() {
    if (stream) { stream.close(); stream = null; }
    panelRunId = null;
    $("runPanel").hidden = true;
  }
  $("runClose").addEventListener("click", closePanel);

  /* ---------------- boot ---------------- */
  async function refreshAll() {
    try {
      const [targets] = await Promise.all([loadTargets(), loadOverview(), loadFindings(), loadRuns()]);
      return targets;
    } catch (err) {
      if (!handleAuthError(err)) console.error(err);
      return [];
    }
  }

  // GET requests already retry while the server wakes; just tell the user why it's slow.
  window.invariantWake(() => {
    $("dashLoading").textContent = "Waking up the server — free hosting can take up to a minute…";
  }).catch(() => {});
  api("/api/auth/me")
    .then(async ({ user }) => {
      renderUser(user);
      try { allowed = (await api("/api/targets/allowed")).allowed || []; } catch (_) { allowed = []; }
      renderAllowedHint();
      const targets = await refreshAll();
      $("dashLoading").hidden = true;
      $("dashContent").hidden = false;
      document.body.classList.remove("is-loading");
      // Re-attach to a scan that is still running (e.g. after a page reload).
      const live = targets.find((t) => t.last_run && ACTIVE.has(t.last_run.status));
      if (live) openRun(live.last_run.id);
    })
    .catch((err) => {
      if (!handleAuthError(err)) $("dashLoading").textContent = err.message;
    });
})();
