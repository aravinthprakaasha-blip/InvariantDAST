"""InvariantDAST scan engine.

Drives a B2B travel API through its booking lifecycle with two tenant personas
(Agency A = owner, Agency B = adversary), runs business-logic and authorization
checks, records verified findings with evidence and a curl repro, and
compensates every booking it creates so no seats stay locked.

Target contract (matches sample_travel_api):
  POST  /flights/search            {origin, destination}
  POST  /quotes                    {flight_id, passengers}
  POST  /bookings/hold             {quote_id}
  POST  /bookings/{id}/issue
  PATCH /bookings/{id}/amend       {passenger_name}
  POST  /bookings/{id}/refund
  GET   /bookings/{id}
Tenancy is identified by a request header (default X-Agency-ID).
"""

import json
import shlex
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone

import httpx

from db import get_db

# Circuit breakers / safety limits
MAX_REQUESTS = 250          # hard cap per run
COMPENSATION_BUDGET = 80    # extra requests reserved for rollback, even after the cap
MAX_SERVER_ERRORS = 5       # stop if the target starts failing
MAX_OPEN_HOLDS = 10         # never lock more than this many seats at once
MIN_INTERVAL = 0.03         # seconds between requests (rate limit)
RACE_PARALLELISM = 5
TIMEOUT = 10.0
WAKE_TIMEOUT = 90.0         # sleeping (free-tier) targets can take ~1 min to wake up

CONTRACT = [
    ("POST", "/flights/search"), ("POST", "/quotes"), ("POST", "/bookings/hold"),
    ("POST", "/bookings/{booking_id}/issue"), ("PATCH", "/bookings/{booking_id}/amend"),
    ("POST", "/bookings/{booking_id}/refund"), ("GET", "/bookings/{booking_id}"),
]

CHECKS = [
    ("bola_read", "Cross-tenant booking read (BOLA)"),
    ("bola_quote", "Cross-tenant quote hijack"),
    ("bola_issue", "Cross-tenant ticket issuance"),
    ("skip_refund", "Refund before ticketing (step skip)"),
    ("skip_amend", "Amend before ticketing (step skip)"),
    ("bola_amend", "Cross-tenant booking amendment"),
    ("bola_refund", "Cross-tenant refund"),
    ("quote_replay", "Quote replay (double booking)"),
    ("fare_tamper", "Fare / currency tampering on hold"),
    ("refund_race", "Double-refund race condition"),
    ("input_validation", "Passenger-count validation"),
    ("unauth_read", "Booking read without valid tenant"),
]

_active_runs: set[int] = set()
_active_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Abort(Exception):
    """Stops the scan (circuit breaker or unrecoverable target error)."""


class Scan:
    def __init__(self, run_id: int, target: dict, user_id: int):
        self.run_id = run_id
        self.t = target
        self.user_id = user_id
        self.base = target["base_url"].rstrip("/")
        self.client = httpx.Client(base_url=self.base, timeout=TIMEOUT, follow_redirects=False)
        self.n_requests = 0
        self.n_mutations = 0
        self.n_5xx = 0
        self.n_compensations = 0
        self.passed = 0
        self.skipped = 0
        self.failed = 0
        self.errored = 0
        self.findings = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        self._last = 0.0
        self.bookings: dict[str, str] = {}   # booking_id -> owner persona ("A"/"B")
        self.flight: dict | None = None
        self.compensating = False
        self.started = time.monotonic()

    # ---------------- persistence ----------------

    def log(self, level: str, message: str) -> None:
        with closing(get_db()) as conn, conn:
            conn.execute(
                "INSERT INTO run_events (run_id, ts, level, message) VALUES (?, ?, ?, ?)",
                (self.run_id, now_iso(), level, message),
            )

    def update_run(self, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        with closing(get_db()) as conn, conn:
            conn.execute(f"UPDATE runs SET {cols} WHERE id = ?", (*fields.values(), self.run_id))

    def stats(self) -> dict:
        return {
            "requests": self.n_requests,
            "mutations": self.n_mutations,
            "compensations": self.n_compensations,
            "server_errors": self.n_5xx,
            "passed": self.passed,
            "skipped": self.skipped,
            "failed": self.failed,
            "errored": self.errored,
            "findings": self.findings,
            "duration_ms": int((time.monotonic() - self.started) * 1000),
        }

    def record_finding(self, check_id, severity, title, endpoint, description, evidence, repro_steps):
        self.findings[severity] += 1
        repro = "\n".join([f'API="{self.base}"', ""] + repro_steps)
        with closing(get_db()) as conn, conn:
            conn.execute(
                """INSERT INTO findings (run_id, target_id, user_id, check_id, severity, title, endpoint,
                                         description, evidence, repro, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (self.run_id, self.t["id"], self.user_id, check_id, severity, title, endpoint,
                 description, json.dumps(evidence, default=str), repro, now_iso()),
            )

    # ---------------- HTTP ----------------

    def headers(self, persona: str | None) -> dict:
        h = {"X-Invariant-Trace": f"run-{self.run_id}-{self.n_requests + 1}"}
        if persona == "A":
            h[self.t["auth_header"]] = self.t["persona_a"]
        elif persona == "B":
            h[self.t["auth_header"]] = self.t["persona_b"]
        elif persona and persona.startswith("raw:"):
            h[self.t["auth_header"]] = persona[4:]
        return h

    def req(self, persona, method, path, body=None) -> httpx.Response:
        cap = MAX_REQUESTS + (COMPENSATION_BUDGET if self.compensating else 0)
        if self.n_requests >= cap:
            raise Abort(f"Circuit breaker: request cap ({cap}) reached")
        wait = MIN_INTERVAL - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        self.n_requests += 1
        if method != "GET":
            self.n_mutations += 1
        try:
            r = self.client.request(method, path, json=body, headers=self.headers(persona))
        except httpx.HTTPError as e:
            raise Abort(f"Target unreachable: {method} {path} → {type(e).__name__}: {e}") from None
        if r.status_code >= 500:
            self.n_5xx += 1
            if self.n_5xx >= MAX_SERVER_ERRORS:
                raise Abort(f"Circuit breaker: {self.n_5xx} server errors from target")
        return r

    def curl(self, persona, method, path, body=None) -> str:
        parts = ["curl -s", f"-X {method}", f'"$API{path}"']
        hdr_val = {"A": self.t["persona_a"], "B": self.t["persona_b"]}.get(persona)
        if hdr_val:
            parts.append("-H " + shlex.quote(f'{self.t["auth_header"]}: {hdr_val}'))
        if body is not None:
            parts.append("-H 'Content-Type: application/json' -d " + shlex.quote(json.dumps(body)))
        return " ".join(parts)

    @staticmethod
    def body(r: httpx.Response):
        try:
            return r.json()
        except ValueError:
            return r.text[:500]

    @staticmethod
    def ok(r: httpx.Response) -> bool:
        return 200 <= r.status_code < 300

    def snap(self, persona, method, path, body, r):
        return {"request": {"persona": persona, "method": method, "path": path, "body": body},
                "response": {"status": r.status_code, "body": self.body(r)}}

    # ---------------- lifecycle helpers ----------------

    def new_quote(self, persona="A", passengers=1) -> dict:
        body = {"flight_id": self.flight["flight_id"], "passengers": passengers}
        r = self.req(persona, "POST", "/quotes", body)
        if not self.ok(r):
            raise Abort(f"Could not create quote as Agency {persona}: {r.status_code} {self.body(r)}")
        return r.json()

    def hold(self, persona, quote_id, extra=None) -> httpx.Response:
        if len(self.bookings) >= MAX_OPEN_HOLDS:
            raise Abort(f"Circuit breaker: {len(self.bookings)} bookings created (seat-lock cap)")
        r = self.req(persona, "POST", "/bookings/hold", {"quote_id": quote_id, **(extra or {})})
        if self.ok(r):
            bid = self.body(r).get("booking_id") if isinstance(self.body(r), dict) else None
            if bid:
                self.bookings[bid] = persona
        return r

    def new_booking(self, persona="A") -> dict:
        q = self.new_quote(persona)
        r = self.hold(persona, q["quote_id"])
        if not self.ok(r):
            raise Abort(f"Could not hold booking as Agency {persona}: {r.status_code} {self.body(r)}")
        return r.json()

    def seats(self):
        r = self.req("A", "POST", "/flights/search", {"origin": self.t["origin"], "destination": self.t["destination"]})
        if not self.ok(r):
            return None
        for f in r.json().get("flights") or []:
            if f.get("flight_id") == self.flight["flight_id"]:
                return f.get("available_seats")
        return None

    def get_booking(self, persona, bid):
        r = self.req(persona, "GET", f"/bookings/{bid}")
        return r.json() if self.ok(r) else None

    # ---------------- run ----------------

    def run(self) -> None:
        self.update_run(status="running", started_at=now_iso(), checks_total=len(CHECKS))
        self.log("info", f"Scan started against {self.base}")
        status, error = "completed", None
        try:
            self.wake_target()
            self.discover()
            self.prepare()
            for i, (check_id, title) in enumerate(CHECKS, 1):
                self.log("info", f"[{i}/{len(CHECKS)}] {title}")
                try:
                    getattr(self, "check_" + check_id)(title)
                except Abort:
                    raise
                except Exception as e:  # one broken check must not kill the run
                    self.errored += 1
                    self.log("error", f"Check errored: {type(e).__name__}: {e}")
                self.update_run(checks_done=i, stats=json.dumps(self.stats()))
        except Abort as e:
            status, error = "failed", str(e)
            self.log("error", str(e))
        except Exception as e:
            status, error = "failed", f"{type(e).__name__}: {e}"
            self.log("error", f"Unexpected error: {error}")
        finally:
            try:
                self.compensate()
            except Exception as e:
                self.log("error", f"Compensation error: {e}")
            self.client.close()
            total = sum(self.findings.values())
            self.log("ok" if total == 0 and status == "completed" else "warn" if status == "completed" else "error",
                     f"Scan {status}: {self.passed} passed · {self.failed} failed · {self.skipped} skipped · {total} finding(s) · "
                     f"{self.n_requests} requests · {self.n_compensations} compensations")
            self.update_run(status=status, error=error, finished_at=now_iso(), stats=json.dumps(self.stats()))

    def wake_target(self) -> None:
        """One patient read-only request, so a target that is asleep has time to start."""
        started = time.monotonic()
        try:
            self.n_requests += 1
            self.client.get("/", headers=self.headers(None), timeout=WAKE_TIMEOUT)
        except httpx.HTTPError as e:
            raise Abort(f"Target unreachable: {type(e).__name__}: {e}") from None
        waited = time.monotonic() - started
        if waited > 3:
            self.log("info", f"Target woke up after {waited:.0f}s")

    def discover(self) -> None:
        surface, documented = [], set()
        r = self.req(None, "GET", "/openapi.json")
        if self.ok(r) and isinstance(self.body(r), dict):
            spec = r.json()
            for path, ops in (spec.get("paths") or {}).items():
                for method in ops:
                    if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                        documented.add((method.upper(), path))
                        surface.append({"method": method.upper(), "path": path, "documented": True,
                                        "in_contract": False})
            self.log("ok", f"OpenAPI spec found: {len(surface)} documented operations")
        else:
            self.log("warn", f"No OpenAPI spec at /openapi.json ({r.status_code}); using booking contract only")
        for m, p in CONTRACT:
            hit = next((s for s in surface if s["method"] == m and s["path"] == p), None)
            if hit:
                hit["in_contract"] = True
            else:
                surface.append({"method": m, "path": p, "documented": False, "in_contract": True})
        undocumented = [s for s in surface if not s["documented"]]
        if documented and undocumented:
            self.log("warn", f"{len(undocumented)} lifecycle route(s) missing from the spec")
        self.update_run(surface=json.dumps(surface))

    def prepare(self) -> None:
        body = {"origin": self.t["origin"], "destination": self.t["destination"]}
        r = self.req("A", "POST", "/flights/search", body)
        if not self.ok(r):
            raise Abort(f"Flight search failed: {r.status_code} {self.body(r)}")
        flights = [f for f in (r.json().get("flights") or []) if f.get("available_seats", 0) >= 1]
        if not flights:
            raise Abort(f"No flights with free seats for {body['origin']}→{body['destination']}")
        self.flight = flights[0]
        self.log("ok", f"Search {body['origin']}→{body['destination']}: using {self.flight['flight_id']} "
                       f"({self.flight.get('available_seats')} seats)")
        self.bk = self.new_booking("A")
        self.log("ok", f"Agency A holds booking {self.bk['booking_id'][:8]}… (state HELD)")

    def passed_check(self, msg):
        self.passed += 1
        self.log("ok", "  ✓ " + msg)

    def skipped_check(self, msg):
        self.skipped += 1
        self.log("warn", "  – skipped: " + msg)

    def failed_check(self, msg):
        self.failed += 1
        self.log("fail", "  ✗ " + msg)

    # ---------------- checks ----------------

    def check_bola_read(self, title):
        bid = self.bk["booking_id"]
        path = f"/bookings/{bid}"
        owner = self.get_booking("A", bid) or {}
        r = self.req("B", "GET", path)
        if self.ok(r) and isinstance(self.body(r), dict):
            other = r.json()
            same = sorted(k for k in owner if k in other and other[k] == owner[k])
            self.failed_check(f"Agency B read Agency A's booking ({len(same)}/{len(owner)} fields identical)")
            self.record_finding(
                "bola_read", "critical", "Cross-tenant booking read (BOLA/IDOR)", f"GET {path.replace(bid, '{id}')}",
                "Agency B can read a booking owned by Agency A. The structural diff against the owner's view "
                f"shows {len(same)} of {len(owner)} fields identical, including tenant-owned data.",
                {"identical_fields": same, **self.snap("B", "GET", path, None, r), "owner_view": owner},
                ["# Agency B reads Agency A's booking (expected 403/404)", self.curl("B", "GET", "/bookings/$BOOKING_ID")],
            )
        else:
            self.passed_check(f"Agency B denied ({r.status_code})")

    def check_bola_quote(self, title):
        q = self.new_quote("A")
        r = self.hold("B", q["quote_id"])
        if self.ok(r):
            self.failed_check("Agency B placed a hold using Agency A's quote")
            self.record_finding(
                "bola_quote", "critical", "Cross-tenant quote hijack", "POST /bookings/hold",
                "Agency B converted a quote created by Agency A into its own booking, taking A's fare and seats.",
                self.snap("B", "POST", "/bookings/hold", {"quote_id": q["quote_id"]}, r),
                ["# 1. Agency A creates a quote", self.curl("A", "POST", "/quotes", {"flight_id": self.flight["flight_id"], "passengers": 1}),
                 "# 2. Agency B holds it (expected 403)", self.curl("B", "POST", "/bookings/hold", {"quote_id": "$QUOTE_ID"})],
            )
        else:
            self.passed_check(f"Hold with another tenant's quote denied ({r.status_code})")

    def check_bola_issue(self, title):
        bid = self.bk["booking_id"]
        path = f"/bookings/{bid}/issue"
        r = self.req("B", "POST", path)
        if self.ok(r):
            self.failed_check("Agency B issued a ticket on Agency A's booking")
            self.record_finding(
                "bola_issue", "critical", "Cross-tenant ticket issuance", "POST /bookings/{id}/issue",
                "Agency B can trigger ticket issuance on a booking owned by Agency A.",
                self.snap("B", "POST", path, None, r),
                ["# Agency B issues Agency A's held booking (expected 403)", self.curl("B", "POST", "/bookings/$BOOKING_ID/issue")],
            )
        else:
            self.passed_check(f"Agency B denied ({r.status_code})")

    def check_skip_refund(self, title):
        bid = self.bk["booking_id"]
        state = (self.get_booking("A", bid) or {}).get("status")
        if state != "HELD":
            self.skipped_check(f"booking is {state}, not HELD")
            return
        path = f"/bookings/{bid}/refund"
        r = self.req("A", "POST", path)
        if self.ok(r):
            self.failed_check("Refund accepted on an un-ticketed (HELD) booking")
            self.record_finding(
                "skip_refund", "high", "Refund before ticketing (state skip)", "POST /bookings/{id}/refund",
                "The refund transition fires from HELD, skipping ticket issuance. Funds can be refunded for a "
                "booking that was never paid or ticketed.",
                self.snap("A", "POST", path, None, r),
                ["# Refund a booking that is only HELD (expected 400/409)", self.curl("A", "POST", "/bookings/$BOOKING_ID/refund")],
            )
            self.bk = self.new_booking("A")
            self.log("info", f"  → fresh booking {self.bk['booking_id'][:8]}… for the remaining checks")
        else:
            self.passed_check(f"Out-of-order refund rejected ({r.status_code})")

    def check_skip_amend(self, title):
        bid = self.bk["booking_id"]
        state = (self.get_booking("A", bid) or {}).get("status")
        if state != "HELD":
            self.skipped_check(f"booking is {state}, not HELD")
            return
        path, body = f"/bookings/{bid}/amend", {"passenger_name": "Invariant Probe"}
        r = self.req("A", "PATCH", path, body)
        if self.ok(r):
            self.failed_check("Amendment accepted before ticketing")
            self.record_finding(
                "skip_amend", "medium", "Amendment before ticketing (state skip)", "PATCH /bookings/{id}/amend",
                "Passenger details can be amended while the booking is only HELD, bypassing the post-issue "
                "amendment workflow.",
                self.snap("A", "PATCH", path, body, r),
                ["# Amend a HELD booking (expected 400)", self.curl("A", "PATCH", "/bookings/$BOOKING_ID/amend", body)],
            )
        else:
            self.passed_check(f"Out-of-order amendment rejected ({r.status_code})")
        # Advance the legitimate lifecycle for the following checks.
        if (self.get_booking("A", bid) or {}).get("status") == "HELD":
            r = self.req("A", "POST", f"/bookings/{bid}/issue")
            if self.ok(r):
                self.log("info", "  → Agency A issues its booking (legitimate transition)")

    def check_bola_amend(self, title):
        bid = self.bk["booking_id"]
        before = self.get_booking("A", bid) or {}
        if before.get("status") != "ISSUED":
            self.skipped_check(f"booking is {before.get('status')}, not ISSUED")
            return
        path, body = f"/bookings/{bid}/amend", {"passenger_name": "Invariant Cross Tenant"}
        r = self.req("B", "PATCH", path, body)
        after = self.get_booking("A", bid) or {}
        changed = after.get("passenger_name") != before.get("passenger_name")
        if self.ok(r) or changed:
            self.failed_check("Agency B amended Agency A's booking")
            self.record_finding(
                "bola_amend", "critical", "Cross-tenant booking amendment", "PATCH /bookings/{id}/amend",
                "Agency B modified passenger details on a booking owned by Agency A.",
                {**self.snap("B", "PATCH", path, body, r),
                 "passenger_name_before": before.get("passenger_name"), "passenger_name_after": after.get("passenger_name")},
                ["# Agency B amends Agency A's booking (expected 403)", self.curl("B", "PATCH", "/bookings/$BOOKING_ID/amend", body)],
            )
        else:
            self.passed_check(f"Agency B denied ({r.status_code}); owner data unchanged")

    def check_bola_refund(self, title):
        bid = self.bk["booking_id"]
        path = f"/bookings/{bid}/refund"
        r = self.req("B", "POST", path)
        if self.ok(r):
            self.failed_check("Agency B refunded Agency A's booking")
            self.record_finding(
                "bola_refund", "critical", "Cross-tenant refund", "POST /bookings/{id}/refund",
                "Agency B can trigger a refund on a booking owned by Agency A.",
                self.snap("B", "POST", path, None, r),
                ["# Agency B refunds Agency A's booking (expected 403)", self.curl("B", "POST", "/bookings/$BOOKING_ID/refund")],
            )
        else:
            self.passed_check(f"Agency B denied ({r.status_code})")

    def check_quote_replay(self, title):
        q = self.new_quote("A")
        first = self.hold("A", q["quote_id"])
        if not self.ok(first):
            self.skipped_check(f"first hold failed ({first.status_code})")
            return
        r = self.hold("A", q["quote_id"])
        if self.ok(r):
            self.failed_check("Same quote produced two bookings")
            self.record_finding(
                "quote_replay", "high", "Quote replay creates duplicate bookings", "POST /bookings/hold",
                "A quote can be redeemed more than once, letting an agency lock extra seats at a stale fare.",
                self.snap("A", "POST", "/bookings/hold", {"quote_id": q["quote_id"]}, r),
                ["# Hold the same quote twice (second should be rejected)",
                 self.curl("A", "POST", "/bookings/hold", {"quote_id": "$QUOTE_ID"}),
                 self.curl("A", "POST", "/bookings/hold", {"quote_id": "$QUOTE_ID"})],
            )
        else:
            self.passed_check(f"Quote replay rejected ({r.status_code})")

    def check_fare_tamper(self, title):
        q = self.new_quote("A")
        tamper = {"total_price": 1, "currency": "USD", "passengers": 9, "agency_id": self.t["persona_b"]}
        r = self.hold("A", q["quote_id"], tamper)
        if not self.ok(r):
            self.passed_check(f"Tampered hold rejected ({r.status_code})")
            self.tamper_bk = None
            return
        bk = r.json()
        self.tamper_bk = bk
        diffs = {k: {"quoted": q.get(k), "booked": bk.get(k)}
                 for k in ("total_price", "currency", "passengers") if k in bk and bk.get(k) != q.get(k)}
        if bk.get("agency_id") not in (None, self.t["persona_a"]):
            diffs["agency_id"] = {"expected": self.t["persona_a"], "booked": bk.get("agency_id")}
        if diffs:
            self.failed_check("Client-supplied fields overrode the quoted fare: " + ", ".join(diffs))
            self.record_finding(
                "fare_tamper", "critical", "Fare tampering on booking hold", "POST /bookings/hold",
                "The hold endpoint trusts client-supplied pricing/ownership fields instead of the server-side quote.",
                {"differences": diffs, **self.snap("A", "POST", "/bookings/hold", {"quote_id": q["quote_id"], **tamper}, r)},
                ["# Hold with injected price fields (server must use the quote)",
                 self.curl("A", "POST", "/bookings/hold", {"quote_id": "$QUOTE_ID", **tamper})],
            )
        else:
            self.passed_check("Injected price/currency/owner fields ignored; booking matches quote")

    def check_refund_race(self, title):
        bk = getattr(self, "tamper_bk", None) or self.new_booking("A")
        bid = bk["booking_id"]
        r = self.req("A", "POST", f"/bookings/{bid}/issue")
        if not self.ok(r):
            self.skipped_check(f"could not issue booking ({r.status_code})")
            return
        path = f"/bookings/{bid}/refund"
        seats_before = self.seats()
        barrier = threading.Barrier(RACE_PARALLELISM)
        results: list[int] = []
        lock = threading.Lock()

        def fire():
            with httpx.Client(base_url=self.base, timeout=TIMEOUT) as c:
                barrier.wait()
                try:
                    code = c.post(path, headers=self.headers("A")).status_code
                except httpx.HTTPError:
                    code = 0
            with lock:
                results.append(code)

        threads = [threading.Thread(target=fire) for _ in range(RACE_PARALLELISM)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.n_requests += RACE_PARALLELISM
        self.n_mutations += RACE_PARALLELISM
        wins = sum(1 for c in results if 200 <= c < 300)
        seats_after = self.seats()
        over = None
        if seats_before is not None and seats_after is not None:
            over = seats_after - seats_before - bk.get("passengers", 1)
        if wins > 1:
            self.failed_check(f"{wins} of {RACE_PARALLELISM} parallel refunds succeeded")
            if over:
                self.log("warn", f"  ! target inventory over-released by {over} seat(s); this cannot be "
                                 "compensated through the API — correct it manually")
            self.record_finding(
                "refund_race", "critical", "Double refund via race condition", "POST /bookings/{id}/refund",
                f"{wins} concurrent refund requests for one ticket all succeeded, refunding the fare {wins} times "
                "and releasing seats more than once.",
                {"parallel_requests": RACE_PARALLELISM, "status_codes": sorted(results), "successful": wins,
                 "seats_before": seats_before, "seats_after": seats_after, "seats_over_released": over},
                ["# Fire 5 refunds concurrently for one ISSUED booking (only one may succeed)",
                 "for i in 1 2 3 4 5; do " + self.curl("A", "POST", "/bookings/$BOOKING_ID/refund") + " & done; wait"],
            )
        else:
            self.passed_check(f"Only {wins} of {RACE_PARALLELISM} parallel refunds succeeded")

    def check_input_validation(self, title):
        bad = []
        for pax in (0, -1):
            body = {"flight_id": self.flight["flight_id"], "passengers": pax}
            r = self.req("A", "POST", "/quotes", body)
            if self.ok(r):
                bad.append((pax, r))
        if bad:
            pax, r = bad[0]
            self.failed_check(f"Quote accepted with passengers={', '.join(str(p) for p, _ in bad)}")
            body = {"flight_id": self.flight["flight_id"], "passengers": pax}
            self.record_finding(
                "input_validation", "high", "Non-positive passenger count accepted", "POST /quotes",
                "Quotes accept zero or negative passenger counts, producing zero/negative fares that can "
                "flow into holds and refunds.",
                self.snap("A", "POST", "/quotes", body, r),
                ["# Quote with a non-positive passenger count (expected 422)", self.curl("A", "POST", "/quotes", body)],
            )
        else:
            self.passed_check("passengers=0 and passengers=-1 rejected")

    def check_unauth_read(self, title):
        bid = self.bk["booking_id"]
        path = f"/bookings/{bid}"
        probes = [(None, "no tenant header"), ("raw:" + "inv-" + uuid.uuid4().hex[:8], "unknown tenant id")]
        for persona, label in probes:
            r = self.req(persona, "GET", path)
            if self.ok(r):
                self.failed_check(f"Booking readable with {label}")
                self.record_finding(
                    "unauth_read", "critical", f"Booking readable with {label}", "GET /bookings/{id}",
                    f"A booking can be read with {label}, bypassing tenant authentication.",
                    self.snap(persona, "GET", path, None, r),
                    [f"# Read a booking with {label} (expected 401/403)",
                     f'curl -s "$API/bookings/$BOOKING_ID"' + ("" if persona is None else
                         " -H " + shlex.quote(f'{self.t["auth_header"]}: {persona[4:]}'))],
                )
                return
        self.passed_check("Requests without a valid tenant were denied")

    # ---------------- compensation ----------------

    def compensate(self) -> None:
        if not self.bookings:
            return
        self.compensating = True
        self.n_5xx = 0  # don't let earlier target errors block cleanup
        self.log("comp", f"Compensating {len(self.bookings)} booking(s) created by this scan")
        for bid, owner in list(self.bookings.items()):
            try:
                state = (self.get_booking(owner, bid) or {}).get("status")
                if state == "HELD":
                    self.req(owner, "POST", f"/bookings/{bid}/issue")
                    state = (self.get_booking(owner, bid) or {}).get("status")
                if state == "ISSUED":
                    r = self.req(owner, "POST", f"/bookings/{bid}/refund")
                    state = "REFUNDED" if self.ok(r) else state
                if state == "REFUNDED":
                    self.n_compensations += 1
                    self.log("comp", f"  ↺ {bid[:8]}… refunded, seats released")
                else:
                    self.log("warn", f"  ! {bid[:8]}… left in state {state}; release manually")
            except Abort as e:
                self.log("warn", f"  ! {bid[:8]}… not compensated: {e}")


def start_scan(run_id: int, target: dict, user_id: int) -> None:
    def worker():
        try:
            Scan(run_id, target, user_id).run()
        finally:
            with _active_lock:
                _active_runs.discard(target["id"])

    with _active_lock:
        _active_runs.add(target["id"])
    threading.Thread(target=worker, name=f"scan-{run_id}", daemon=True).start()


def target_busy(target_id: int) -> bool:
    with _active_lock:
        return target_id in _active_runs
