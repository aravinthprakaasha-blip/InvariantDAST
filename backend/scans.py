"""Targets, scan runs, live run events (SSE) and findings — all scoped to the signed-in user."""

import asyncio
import json
import os
import re
from contextlib import closing
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from auth import require_user
from db import get_db
from scanner import CHECKS, now_iso, start_scan, target_busy

router = APIRouter(prefix="/api", tags=["scans"])

FINISHED = {"completed", "failed", "interrupted"}

# Scope allowlist. When set (always on a hosted deployment), only these origins can be scanned,
# e.g. "https://demo-a.onrender.com,https://demo-b.onrender.com". Unset = any URL (local dev only).
ALLOWED_TARGETS = [
    o.strip().rstrip("/").lower()
    for o in os.environ.get("INVARIANT_ALLOWED_TARGETS", "").split(",") if o.strip()
]


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}".lower()


def check_scope(url: str) -> None:
    if ALLOWED_TARGETS and _origin(url) not in ALLOWED_TARGETS:
        raise ValueError("This server only scans approved targets: " + ", ".join(ALLOWED_TARGETS))

# -----------------------------------
# MODELS
# -----------------------------------


class TargetIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    base_url: str = Field(min_length=8, max_length=300)
    auth_header: str = Field(default="X-Agency-ID", min_length=1, max_length=64)
    persona_a: str = Field(min_length=1, max_length=200)
    persona_b: str = Field(min_length=1, max_length=200)
    origin: str = Field(min_length=2, max_length=8)
    destination: str = Field(min_length=2, max_length=8)
    authorized: bool

    @field_validator("name", "persona_a", "persona_b")
    @classmethod
    def strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("origin", "destination")
    @classmethod
    def code(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("auth_header")
    @classmethod
    def header_name(cls, v: str) -> str:
        v = v.strip()
        if not re.fullmatch(r"[A-Za-z0-9-]+", v):
            raise ValueError("Tenant header can only use letters, numbers and hyphens (for example X-Agency-ID)")
        return v

    @field_validator("base_url")
    @classmethod
    def url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        p = urlparse(v)
        if p.scheme not in ("http", "https") or not p.netloc:
            raise ValueError("Enter a full http:// or https:// URL")
        check_scope(v)
        return v

    @model_validator(mode="after")
    def checks(self):
        if not self.authorized:
            raise ValueError("Confirm you are authorised to test this API")
        if self.persona_a == self.persona_b:
            raise ValueError("Agency A and Agency B must be different tenants")
        return self


# -----------------------------------
# HELPERS
# -----------------------------------


def _run_dict(row) -> dict:
    d = dict(row)
    d["stats"] = json.loads(d.get("stats") or "{}")
    d["surface"] = json.loads(d.get("surface") or "[]")
    return d


def _finding_dict(row) -> dict:
    d = dict(row)
    d["evidence"] = json.loads(d["evidence"])
    return d


def _own_target(conn, user_id: int, target_id: int):
    t = conn.execute("SELECT * FROM targets WHERE id = ? AND user_id = ?", (target_id, user_id)).fetchone()
    if not t:
        raise HTTPException(404, "Target not found")
    return t


def _own_run(conn, user_id: int, run_id: int):
    r = conn.execute(
        "SELECT r.*, t.name AS target_name, t.base_url FROM runs r JOIN targets t ON t.id = r.target_id "
        "WHERE r.id = ? AND r.user_id = ?", (run_id, user_id)).fetchone()
    if not r:
        raise HTTPException(404, "Run not found")
    return r


LATEST_RUN_SQL = """
    SELECT r.* FROM runs r
    WHERE r.user_id = ? AND r.status = 'completed' AND r.id = (
        SELECT MAX(id) FROM runs WHERE target_id = r.target_id AND status = 'completed')
"""

# -----------------------------------
# TARGETS
# -----------------------------------


@router.get("/targets/allowed")
def allowed_targets(user=Depends(require_user)):
    """The scope allowlist, so the dashboard can offer the approved targets. Empty = unrestricted."""
    return {"allowed": ALLOWED_TARGETS}


@router.get("/targets")
def list_targets(user=Depends(require_user)):
    with closing(get_db()) as conn:
        rows = conn.execute("SELECT * FROM targets WHERE user_id = ? ORDER BY id DESC", (user["id"],)).fetchall()
        out = []
        for t in rows:
            last = conn.execute("SELECT * FROM runs WHERE target_id = ? ORDER BY id DESC LIMIT 1", (t["id"],)).fetchone()
            d = dict(t)
            d["last_run"] = _run_dict(last) if last else None
            d["busy"] = target_busy(t["id"])
            out.append(d)
    return {"targets": out}


@router.post("/targets", status_code=201)
def create_target(body: TargetIn, user=Depends(require_user)):
    with closing(get_db()) as conn, conn:
        new_id = conn.execute(
            """INSERT INTO targets (user_id, name, base_url, auth_header, persona_a, persona_b, origin, destination, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
            (user["id"], body.name, body.base_url, body.auth_header, body.persona_a, body.persona_b,
             body.origin, body.destination, now_iso()),
        ).fetchall()[0]["id"]
        t = conn.execute("SELECT * FROM targets WHERE id = ?", (new_id,)).fetchone()
    return {"target": {**dict(t), "last_run": None, "busy": False}}


@router.delete("/targets/{target_id}")
def delete_target(target_id: int, user=Depends(require_user)):
    if target_busy(target_id):
        raise HTTPException(409, "A scan is running on this target. Wait for it to finish.")
    with closing(get_db()) as conn, conn:
        _own_target(conn, user["id"], target_id)
        conn.execute("DELETE FROM targets WHERE id = ?", (target_id,))
    return {"status": "deleted"}


# -----------------------------------
# RUNS
# -----------------------------------


@router.post("/targets/{target_id}/runs", status_code=201)
def start_run(target_id: int, user=Depends(require_user)):
    if target_busy(target_id):
        raise HTTPException(409, "A scan is already running on this target.")
    with closing(get_db()) as conn, conn:
        t = dict(_own_target(conn, user["id"], target_id))
        try:
            check_scope(t["base_url"])   # re-checked at scan time in case the allowlist changed
        except ValueError as e:
            raise HTTPException(403, str(e))
        run_id = conn.execute(
            "INSERT INTO runs (target_id, user_id, status, checks_total, created_at) VALUES (?, ?, 'queued', ?, ?) RETURNING id",
            (target_id, user["id"], len(CHECKS), now_iso()),
        ).fetchall()[0]["id"]
    start_scan(run_id, t, user["id"])
    return {"run_id": run_id}


@router.get("/runs")
def list_runs(limit: int = 20, user=Depends(require_user)):
    limit = max(1, min(limit, 100))
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT r.*, t.name AS target_name FROM runs r JOIN targets t ON t.id = r.target_id "
            "WHERE r.user_id = ? ORDER BY r.id DESC LIMIT ?", (user["id"], limit)).fetchall()
    return {"runs": [_run_dict(r) for r in rows]}


@router.get("/runs/{run_id}")
def get_run(run_id: int, user=Depends(require_user)):
    with closing(get_db()) as conn:
        run = _own_run(conn, user["id"], run_id)
        findings = conn.execute("SELECT * FROM findings WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
        events = conn.execute("SELECT id, ts, level, message FROM run_events WHERE run_id = ? ORDER BY id",
                              (run_id,)).fetchall()
    return {"run": _run_dict(run), "findings": [_finding_dict(f) for f in findings],
            "events": [dict(e) for e in events]}


@router.get("/runs/{run_id}/stream")
async def stream_run(run_id: int, request: Request, after: int = 0, user=Depends(require_user)):
    """Server-Sent Events: `log` for each new event, `run` for progress, `done` when finished."""
    with closing(get_db()) as conn:
        _own_run(conn, user["id"], run_id)

    async def gen():
        last_id, last_state = after, None
        while True:
            if await request.is_disconnected():
                return
            with closing(get_db()) as conn:
                events = conn.execute(
                    "SELECT id, ts, level, message FROM run_events WHERE run_id = ? AND id > ? ORDER BY id LIMIT 200",
                    (run_id, last_id)).fetchall()
                run = _run_dict(conn.execute(
                    "SELECT id, status, checks_done, checks_total, stats, error, started_at, finished_at, '[]' AS surface "
                    "FROM runs WHERE id = ?", (run_id,)).fetchone())
            for e in events:
                last_id = e["id"]
                yield f"id: {e['id']}\nevent: log\ndata: {json.dumps(dict(e))}\n\n"
            state = json.dumps(run, sort_keys=True)
            if state != last_state:
                last_state = state
                yield f"event: run\ndata: {state}\n\n"
            if run["status"] in FINISHED and not events:
                yield "event: done\ndata: {}\n\n"
                return
            yield ": keep-alive\n\n"
            await asyncio.sleep(0.4)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# -----------------------------------
# FINDINGS & OVERVIEW
# -----------------------------------


@router.get("/findings")
def current_findings(user=Depends(require_user)):
    """Findings reproduced by the latest completed run of each target (fixed ones drop off automatically)."""
    with closing(get_db()) as conn:
        rows = conn.execute(
            f"""SELECT f.*, t.name AS target_name FROM findings f
                JOIN ({LATEST_RUN_SQL}) lr ON lr.id = f.run_id
                JOIN targets t ON t.id = f.target_id
                ORDER BY CASE f.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, f.id""",
            (user["id"],)).fetchall()
    return {"findings": [_finding_dict(r) for r in rows]}


@router.get("/overview")
def overview(user=Depends(require_user)):
    uid = user["id"]
    with closing(get_db()) as conn:
        targets = conn.execute("SELECT COUNT(*) AS n FROM targets WHERE user_id = ?", (uid,)).fetchone()["n"]
        runs = conn.execute("SELECT COUNT(*) AS n FROM runs WHERE user_id = ?", (uid,)).fetchone()["n"]
        sev = conn.execute(
            f"SELECT f.severity AS severity, COUNT(*) AS n FROM findings f JOIN ({LATEST_RUN_SQL}) lr ON lr.id = f.run_id "
            "GROUP BY f.severity",
            (uid,)).fetchall()
        totals = conn.execute(
            "SELECT stats FROM runs WHERE user_id = ? AND status IN ('completed','failed','interrupted')", (uid,)).fetchall()
    requests = compensations = 0
    for row in totals:
        s = json.loads(row["stats"] or "{}")
        requests += s.get("requests", 0)
        compensations += s.get("compensations", 0)
    by_sev = {"critical": 0, "high": 0, "medium": 0, "low": 0, **{r["severity"]: r["n"] for r in sev}}
    return {"targets": targets, "runs": runs, "open_findings": sum(by_sev.values()),
            "by_severity": by_sev, "requests": requests, "compensations": compensations}
