(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  /* ---------------- nav ---------------- */
  const toggle = $("#navToggle"), links = $("#navLinks");
  toggle.addEventListener("click", () => {
    const open = links.classList.toggle("open");
    toggle.setAttribute("aria-expanded", String(open));
  });
  $$("a", links).forEach((a) => a.addEventListener("click", () => links.classList.remove("open")));

  /* ---------------- reveal on scroll ---------------- */
  $$(".section-head, .compare-card, .graph-panel, .blast, .matrix, .oracle, .saga, .pr, .pipeline, .split > div:first-child").forEach((el) => el.classList.add("reveal"));
  const io = new IntersectionObserver((entries) => {
    entries.forEach((e) => { if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); } });
  }, { threshold: 0.12 });
  $$(".reveal").forEach((el) => io.observe(el));

  /* ---------------- hero terminal ---------------- */
  const termLines = [
    ["w", "$ invariant run --trigger=deploy#4182 --env=staging"],
    ["", "› webhook: commit 9c1e2a4 \"pricing: allow per-quote markup\""],
    ["info", "› AST blast radius: apply_markup() → 3 routes, 2 state paths"],
    ["", "› personas: AGY-A (owner) · AGY-B (adversary) authenticated"],
    ["", "› petri net loaded: 6 places · 7 transitions · 4 invariants"],
    ["", ""],
    ["w", "[path] SEARCH → QUOTE → HOLD → ISSUE"],
    ["ok", "  ✓ POST /flights/search            200   38ms"],
    ["ok", "  ✓ POST /quotes                    200   61ms  ₹11,000"],
    ["warn", "  ~ tamper markup=-0.99 → POST /bookings/hold"],
    ["crit", "  ✗ INVARIANT fare_integrity  ₹110 ≠ ₹11,000"],
    ["info", "  ↺ compensate: POST /bookings/bk_91d/cancel 200"],
    ["", ""],
    ["w", "[oracle] cross-tenant read GET /bookings/{id}"],
    ["crit", "  ✗ BOLA confirmed  diff(A,B) = ∅  11/11 keys"],
    ["", ""],
    ["ok", "✓ 2 verified · 0 unverified · 14/14 compensated"],
    ["ok", "✓ PR #318: fix(pricing): ignore client markup"],
    ["", "  elapsed 4m 37s"],
  ];
  const term = $("#termBody");
  async function runTerminal() {
    for (;;) {
      term.innerHTML = "";
      for (const [cls, text] of termLines) {
        const line = document.createElement("div");
        if (cls) line.className = cls;
        term.appendChild(line);
        if (reduced) { line.textContent = text || " "; continue; }
        if (text.startsWith("$")) {
          for (let i = 1; i <= text.length; i++) { line.textContent = text.slice(0, i); await sleep(22); }
        } else {
          line.textContent = text || " ";
        }
        await sleep(text ? 260 : 120);
      }
      const c = document.createElement("span"); c.className = "cursor"; term.appendChild(c);
      if (reduced) return;
      await sleep(5200);
    }
  }
  runTerminal();

  /* ---------------- petri net ---------------- */
  const svg = $("#petri");
  const NS = "http://www.w3.org/2000/svg";
  const el = (tag, attrs = {}, parent = svg) => {
    const n = document.createElementNS(NS, tag);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    parent && parent.appendChild(n);
    return n;
  };
  const places = {
    search: { x: 70, y: 150, label: "SEARCHED", sub: "/flights/search" },
    quote: { x: 240, y: 150, label: "QUOTED", sub: "/quotes" },
    hold: { x: 410, y: 150, label: "HELD", sub: "/bookings/hold" },
    issue: { x: 580, y: 150, label: "TICKETED", sub: "/{id}/issue" },
    amend: { x: 820, y: 62, label: "AMENDED", sub: "/{id}/amend" },
    refund: { x: 820, y: 238, label: "REFUNDED", sub: "/{id}/refund" },
  };
  const arcs = {
    "search-quote": "M70 150 L240 150",
    "quote-hold": "M240 150 L410 150",
    "hold-issue": "M410 150 L580 150",
    "issue-amend": "M580 150 Q700 62 820 62",
    "issue-refund": "M580 150 Q700 238 820 238",
    skip: "M240 150 Q410 0 580 150",
  };
  const arcEls = {}, transEls = {}, placeEls = {};
  const gArcs = el("g"), gTrans = el("g"), gPlaces = el("g"), gFx = el("g");
  for (const id in arcs) {
    arcEls[id] = el("path", { d: arcs[id], class: "arc" + (id === "skip" ? "" : ""), "data-id": id }, gArcs);
    if (id === "skip") arcEls[id].style.display = "none";
  }
  // transitions at arc midpoints
  for (const id in arcs) {
    if (id === "skip") continue;
    const p = arcEls[id].getPointAtLength(arcEls[id].getTotalLength() / 2);
    transEls[id] = el("rect", { x: p.x - 4, y: p.y - 15, width: 8, height: 30, rx: 2, class: "trans" }, gTrans);
  }
  for (const id in places) {
    const p = places[id];
    const g = el("g", {}, gPlaces);
    placeEls[id] = el("circle", { cx: p.x, cy: p.y, r: 36, class: "place" }, g);
    el("text", { x: p.x, y: p.y + 4, class: "place-label" }, g).textContent = p.label;
    el("text", { x: p.x, y: p.y + 56, class: "place-sub" }, g).textContent = p.sub;
  }

  const scenarios = {
    happy: {
      title: "Legitimate journey",
      verdict: ["invariants hold", ""],
      steps: [
        { place: "search", log: "POST /flights/search CJB→DEL · 200", cls: "ok" },
        { arc: "search-quote", to: "quote", log: "POST /quotes FL001 ×2 · ₹11,000", cls: "ok" },
        { arc: "quote-hold", to: "hold", log: "POST /bookings/hold · HELD · seats −2", cls: "ok" },
        { arc: "hold-issue", to: "issue", log: "POST /bookings/{id}/issue · TICKETED", cls: "ok" },
        { arc: "issue-refund", to: "refund", log: "POST /bookings/{id}/refund · ₹11,000 once", cls: "ok" },
        { log: "↺ compensation ledger balanced · seats restored", cls: "comp" },
      ],
    },
    skip: {
      title: "Step-skip to issue",
      verdict: ["violation · sequence", "crit"],
      steps: [
        { place: "search", log: "POST /flights/search · 200", cls: "ok" },
        { arc: "search-quote", to: "quote", log: "POST /quotes · quote_id q_4c2", cls: "ok" },
        { arc: "skip", to: "issue", bad: true, label: [410, 52, "HOLD bypassed"], log: "POST /bookings/q_4c2/issue · 200 TICKETED", cls: "bad" },
        { log: "✗ INVARIANT issue ⇒ prior HELD ∧ payment_ok", cls: "bad" },
        { log: "↺ compensate: void ticket · release fare lock", cls: "comp" },
      ],
    },
    tamper: {
      title: "Fare tampering",
      verdict: ["violation · fare integrity", "crit"],
      steps: [
        { place: "search", log: "POST /flights/search · 200", cls: "ok" },
        { arc: "search-quote", to: "quote", log: "POST /quotes · total_price ₹11,000", cls: "ok" },
        { arc: "quote-hold", to: "hold", bad: true, label: [325, 112, "total_price: 11000 → 1"], log: "POST /bookings/hold {…, total_price: 1} · 200", cls: "bad" },
        { arc: "hold-issue", to: "issue", bad: true, log: "POST /bookings/{id}/issue · charged ₹1", cls: "bad" },
        { log: "✗ INVARIANT charged == quoted ± markup", cls: "bad" },
        { log: "↺ compensate: void ticket · cancel hold", cls: "comp" },
      ],
    },
    race: {
      title: "Double-refund race",
      verdict: ["violation · idempotency", "crit"],
      steps: [
        { place: "search", log: "POST /flights/search · 200", cls: "ok" },
        { arc: "search-quote", to: "quote", log: "POST /quotes · ₹11,000", cls: "ok" },
        { arc: "quote-hold", to: "hold", log: "POST /bookings/hold · HELD", cls: "ok" },
        { arc: "hold-issue", to: "issue", log: "POST /bookings/{id}/issue · TICKETED", cls: "ok" },
        { arc: "issue-refund", to: "refund", bad: true, race: true, label: [742, 222, "×2 in 4ms"], log: "2× POST /bookings/{id}/refund (parallel) · 200, 200", cls: "bad" },
        { log: "✗ INVARIANT Σ refunds ≤ fare  (₹22,000 > ₹11,000)", cls: "bad" },
        { log: "↺ compensate: reverse duplicate refund", cls: "comp" },
      ],
    },
  };

  let runId = 0;
  function resetGraph() {
    gFx.innerHTML = "";
    Object.values(placeEls).forEach((p) => p.setAttribute("class", "place"));
    Object.values(transEls).forEach((t) => t.setAttribute("class", "trans"));
    Object.entries(arcEls).forEach(([id, a]) => { a.setAttribute("class", "arc"); if (id === "skip") a.style.display = "none"; });
  }
  function animateToken(path, bad, offset = 0) {
    return new Promise((resolve) => {
      const tok = el("circle", { r: 7, class: "token" + (bad ? " bad" : "") }, gFx);
      const len = path.getTotalLength();
      const dur = reduced ? 1 : 850;
      const t0 = performance.now() + offset;
      const frame = (now) => {
        const t = Math.max(0, Math.min(1, (now - t0) / dur));
        const e = t < .5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
        const p = path.getPointAtLength(len * e);
        tok.setAttribute("cx", p.x); tok.setAttribute("cy", p.y + (offset ? 9 : 0) * Math.sin(Math.PI * e));
        if (t < 1) requestAnimationFrame(frame); else { tok.remove(); resolve(); }
      };
      requestAnimationFrame(frame);
    });
  }
  async function playScenario(key) {
    const my = ++runId;
    const sc = scenarios[key];
    resetGraph();
    $("#scenarioTitle").textContent = sc.title;
    const v = $("#scenarioVerdict");
    v.textContent = "running…"; v.className = "verdict";
    const log = $("#scenarioLog"); log.innerHTML = "";
    for (const st of sc.steps) {
      if (my !== runId) return;
      if (st.place) placeEls[st.place].setAttribute("class", "place lit");
      if (st.arc) {
        const a = arcEls[st.arc];
        a.style.display = "";
        a.setAttribute("class", "arc " + (st.bad ? "attack" : "lit"));
        if (st.label) el("text", { x: st.label[0], y: st.label[1], class: "attack-label" }, gFx).textContent = st.label[2];
        if (st.race) await Promise.all([animateToken(a, true, 0), animateToken(a, true, 90)]);
        else await animateToken(a, st.bad);
        if (my !== runId) return;
        if (transEls[st.arc]) transEls[st.arc].setAttribute("class", "trans lit");
        placeEls[st.to].setAttribute("class", "place " + (st.bad ? "bad" : "lit"));
      }
      const li = document.createElement("li");
      li.className = st.cls; li.textContent = st.log;
      log.appendChild(li);
      requestAnimationFrame(() => li.classList.add("show"));
      await sleep(st.arc ? 250 : 520);
    }
    if (my !== runId) return;
    v.textContent = sc.verdict[0]; v.className = "verdict " + sc.verdict[1];
  }
  $$(".graph-controls .chip").forEach((b) => b.addEventListener("click", () => {
    $$(".graph-controls .chip").forEach((x) => { x.classList.remove("active"); x.setAttribute("aria-selected", "false"); });
    b.classList.add("active"); b.setAttribute("aria-selected", "true");
    playScenario(b.dataset.scenario);
  }));
  // start when visible
  const gio = new IntersectionObserver((e) => { if (e[0].isIntersecting) { playScenario("happy"); gio.disconnect(); } }, { threshold: .3 });
  gio.observe($(".graph-panel"));

  /* ---------------- attack-surface matrix ---------------- */
  const routes = [
    ["POST", "/flights/search", "main.3f1a.js", [6,7,8,7,9,8,9,8,9,9], "48.2k/day", true, "documented"],
    ["POST", "/quotes", "main.3f1a.js", [5,6,6,7,6,8,7,7,8,7], "21.7k/day", true, "documented"],
    ["POST", "/bookings/hold", "checkout.88c.js", [4,5,4,6,5,6,6,5,6,6], "6.4k/day", true, "documented"],
    ["POST", "/bookings/{id}/issue", "checkout.88c.js", [3,4,4,4,5,4,5,5,4,5], "3.9k/day", true, "documented"],
    ["PATCH", "/bookings/{id}/amend", "manage.c02.js", [2,3,2,3,3,2,3,4,3,3], "880/day", true, "documented"],
    ["POST", "/bookings/{id}/refund", "manage.c02.js", [2,2,3,2,2,3,2,3,2,3], "610/day", true, "documented"],
    ["POST", "/internal/fare-override", "admin-lite.1b9.js", [3,3,4,3,4,4,3,4,4,4], "1.2k/day", false, "shadow"],
    ["GET", "/bookings/export.csv", "reports.e77.js", [2,1,2,3,2,2,1,2,3,2], "340/day", false, "shadow"],
    ["POST", "/quotes/{id}/reprice", "main.3f1a.js", [3,4,3,3,4,4,5,4,4,5], "2.6k/day", false, "shadow"],
    ["GET", "/api/v1/booking/{pnr}", "archive · 2023-04", [0,0,0,0,0,0,0,0,0,0], "0 (since 2025-08)", false, "zombie"],
    ["POST", "/api/v1/refund/legacy", "archive · 2022-11", [0,0,0,0,0,0,0,0,0,0], "0 (since 2025-06)", false, "zombie"],
    ["GET", "/debug/agency-wallets", "archive · 2023-01", [0,0,0,0,0,0,0,0,0,0], "0 (since 2025-03)", false, "zombie"],
  ];
  const body = $("#matrixBody");
  body.innerHTML = routes.map(([m, p, js, spark, traffic, spec, cls]) => `
    <tr data-cls="${cls}">
      <td class="route"><span class="m">${m}</span>${esc(p)}</td>
      <td><span class="${js.startsWith("archive") ? "no" : "yes"}">${esc(js)}</span></td>
      <td><span class="spark">${spark.map((h) => `<i style="height:${Math.max(2, h * 2)}px"></i>`).join("")}</span><span class="traffic">${traffic}</span></td>
      <td><span class="${spec ? "yes" : "no"}">${spec ? "✓ 3.1" : "—"}</span></td>
      <td><span class="cls ${cls}">${cls}</span></td>
    </tr>`).join("");
  const counts = routes.reduce((a, r) => (a[r[6]] = (a[r[6]] || 0) + 1, a), { all: routes.length });
  ["all", "documented", "shadow", "zombie"].forEach((k) => $("#cnt-" + k).textContent = counts[k] || 0);
  $$(".matrix-filters .chip").forEach((b) => b.addEventListener("click", () => {
    $$(".matrix-filters .chip").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    const f = b.dataset.filter;
    $$("tr", body).forEach((tr) => tr.classList.toggle("hide", f !== "all" && tr.dataset.cls !== f));
  }));

  /* ---------------- dual-persona oracle ---------------- */
  const booking = [
    ["booking_id", '"bk_7f3a91"'], ["agency_id", '"AGY-A"'], ["flight_id", '"FL001"'],
    ["passengers", "2"], ["passenger_name", '"R. Menon"'], ["total_price", "11000"],
    ["currency", '"INR"'], ["markup", "0.08"], ["status", '"TICKETED"'],
    ["pnr", '"QX7L2P"'], ["created_at", '"2026-11-06T09:14Z"'],
  ];
  const renderJSON = (rows, hl) => "{\n" + rows.map(([k, v], i) =>
    `<span class="${hl ? "same" : ""}">  <span class="k">"${k}"</span>: <span class="v">${esc(v)}</span>${i < rows.length - 1 ? "," : ""}</span>`).join("\n") + "\n}";
  $("#jsonA").innerHTML = renderJSON(booking, false);
  const runBtn = $("#runOracle");
  runBtn.addEventListener("click", async () => {
    runBtn.disabled = true;
    const jb = $("#jsonB"), eq = $("#oracleEq"), pb = $(".persona.b"), res = $("#oracleResult");
    pb.classList.remove("breach"); eq.classList.remove("crit"); res.hidden = true;
    eq.querySelector("b").textContent = "…";
    jb.innerHTML = '<span class="muted">// GET as AGY-B … expecting 403</span>';
    await sleep(reduced ? 0 : 700);
    jb.innerHTML = '<span class="muted">// 200 OK — structural diff vs AGY-A:</span>\n';
    for (let i = 1; i <= booking.length; i++) {
      jb.innerHTML = '<span class="muted">// 200 OK — structural diff vs AGY-A:</span>\n' + renderJSON(booking.slice(0, i), true);
      await sleep(reduced ? 0 : 90);
    }
    eq.querySelector("b").textContent = "∅";
    eq.classList.add("crit"); pb.classList.add("breach"); res.hidden = false;
    runBtn.textContent = "Re-run differential"; runBtn.disabled = false;
  });

  /* ---------------- saga ledger ---------------- */
  const pairs = [
    ["POST /bookings/hold", "POST /bookings/{id}/cancel"],
    ["POST /quotes (markup=-0.99)", "DELETE /quotes/{id}"],
    ["POST /bookings/{id}/issue", "POST /bookings/{id}/void"],
    ["POST /bookings/{id}/refund ×2", "POST /refunds/{id}/reverse"],
    ["PATCH /bookings/{id}/amend", "PATCH /bookings/{id}/amend (restore)"],
    ["POST /seats/lock FL002 ×2", "POST /seats/release"],
  ];
  const sagaList = $("#sagaList"), sagaStatus = $("#sagaStatus");
  let trace = 4100;
  const addSaga = (kind, path, st) => {
    const li = document.createElement("li");
    li.innerHTML = `<span class="kind ${kind}">${kind === "mut" ? "mutate" : "comp"}</span><span class="path">${esc(path)}</span><span class="st ${st === "ok" ? "done" : ""}">${st === "ok" ? "✓ rolled back" : "tr-" + (trace++)}</span>`;
    sagaList.prepend(li);
    while (sagaList.children.length > 8) sagaList.lastChild.remove();
    return li;
  };
  const setBar = (id, pct) => $(id).style.setProperty("--w", pct + "%");
  async function runSaga() {
    let i = 0;
    for (const [m, c] of pairs) { addSaga("mut", m); addSaga("comp", c, "ok"); if (++i > 3) break; }
    if (reduced) return;
    for (;;) {
      const [m, c] = pairs[i++ % pairs.length];
      addSaga("mut", m);
      sagaStatus.textContent = "1 pending"; sagaStatus.className = "pill pending";
      setBar("#brWallet", 10 + Math.random() * 14);
      setBar("#brSeats", 4 + Math.random() * 10);
      setBar("#brErr", 2 + Math.random() * 4);
      await sleep(1100);
      addSaga("comp", c, "ok");
      sagaStatus.textContent = "balanced"; sagaStatus.className = "pill ok";
      setBar("#brSeats", 4);
      await sleep(1500);
    }
  }
  const sio = new IntersectionObserver((e) => { if (e[0].isIntersecting) { runSaga(); sio.disconnect(); } }, { threshold: .2 });
  sio.observe($(".saga"));

  /* ---------------- PR tabs ---------------- */
  $$(".tab").forEach((t) => t.addEventListener("click", () => {
    $$(".tab").forEach((x) => x.classList.remove("active"));
    $$(".tab-panel").forEach((p) => p.classList.toggle("active", p.dataset.panel === t.dataset.tab));
    t.classList.add("active");
  }));

  /* ---------------- CTA form → backend ---------------- */
  // Set by config.js ("" on the deployed site = same origin).
  const API_BASE = window.INVARIANT_API ?? "http://localhost:8000";
  // Start waking the (free-tier) backend in the background, so it's ready by the time
  // a visitor signs in or sends the form.
  if (window.invariantWake) window.invariantWake().catch(() => {});
  const form = $("#ctaForm"), note = $("#formNote"), submitBtn = $("button[type=submit]", form);
  const setNote = (msg, isError) => { note.textContent = msg; note.classList.toggle("error", !!isError); };
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const email = $("#email").value.trim();
    submitBtn.disabled = true;
    submitBtn.textContent = "Sending…";
    setNote("");
    try {
      if (window.invariantWake) {
        submitBtn.textContent = "Waking up the server…";
        await window.invariantWake();
        submitBtn.textContent = "Sending…";
      }
      const res = await fetch(`${API_BASE}/api/pilot-requests`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        const detail = Array.isArray(data.detail) ? data.detail[0]?.msg?.replace(/^Value error, /, "") : data.detail;
        const waking = [502, 503, 504].includes(res.status)
          ? "The server is waking up (free hosting can take up to a minute). Please try again in a few seconds." : null;
        throw new Error(detail || waking || `Request failed (${res.status})`);
      }
      setNote(data.status === "already_registered"
        ? `✓ ${data.email} is already on our list — we'll be in touch.`
        : `✓ Thanks — we'll reach out to ${data.email} to scope your pilot.`);
      form.reset();
    } catch (err) {
      setNote(err instanceof TypeError
        ? "✗ Couldn't reach the server. Please try again in a moment."
        : `✗ ${err.message}`, true);
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = "Request a pilot";
    }
  });
})();
