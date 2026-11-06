"""Storage shared by the InvariantDAST API.

- Local development: SQLite file (stdlib only), no setup needed.
- Hosted (Render): Postgres, selected automatically when DATABASE_URL is set.

Every caller uses the same small interface:
    with closing(get_db()) as conn, conn:          # commits on success, rolls back on error
        row = conn.execute("... WHERE id = ?", (x,)).fetchone()
        row["column"]                                # rows are accessed by column name
SQL is written with `?` placeholders and is converted for Postgres.
"""

import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))

DB_PATH = Path(os.environ.get(
    "INVARIANT_DB_PATH",
    Path(__file__).parent / "data" / "invariant.db",
))

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

if USE_POSTGRES:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    _pool = ConnectionPool(
        DATABASE_URL,
        min_size=1,
        max_size=int(os.environ.get("INVARIANT_DB_POOL", "8")),
        kwargs={"row_factory": dict_row},
        open=True,
    )
    IntegrityError = (psycopg.IntegrityError,)
    DBError = (psycopg.Error,)
else:
    IntegrityError = (sqlite3.IntegrityError,)
    DBError = (sqlite3.Error,)


def normalize_email(value: str) -> str:
    value = value.strip().lower()
    if not EMAIL_RE.match(value):
        raise ValueError("Enter a valid email address")
    return value


class Connection:
    """Thin wrapper giving SQLite and Postgres the same calling convention."""

    def __init__(self, raw, postgres: bool):
        self.raw = raw
        self.postgres = postgres

    def execute(self, sql: str, params=()):
        if self.postgres:
            sql = sql.replace("?", "%s")
        return self.raw.execute(sql, params)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.raw.commit()
        else:
            self.raw.rollback()
        return False

    def close(self):
        if self.postgres:
            _pool.putconn(self.raw)   # back to the pool (rolled back if left mid-transaction)
        else:
            self.raw.close()


def get_db() -> Connection:
    if USE_POSTGRES:
        return Connection(_pool.getconn(), postgres=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return Connection(conn, postgres=False)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS pilot_requests (
    id         {pk},
    email      TEXT NOT NULL UNIQUE{nocase},
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            {pk},
    name          TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE{nocase},
    company       TEXT,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS targets (
    id           {pk},
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    base_url     TEXT NOT NULL,
    auth_header  TEXT NOT NULL,
    persona_a    TEXT NOT NULL,
    persona_b    TEXT NOT NULL,
    origin       TEXT NOT NULL,
    destination  TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_targets_user ON targets(user_id);

CREATE TABLE IF NOT EXISTS runs (
    id           {pk},
    target_id    INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status       TEXT NOT NULL,
    checks_total INTEGER NOT NULL DEFAULT 0,
    checks_done  INTEGER NOT NULL DEFAULT 0,
    stats        TEXT NOT NULL DEFAULT '{{}}',
    surface      TEXT NOT NULL DEFAULT '[]',
    error        TEXT,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_target ON runs(target_id);
CREATE INDEX IF NOT EXISTS idx_runs_user ON runs(user_id);

CREATE TABLE IF NOT EXISTS run_events (
    id       {pk},
    run_id   INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    ts       TEXT NOT NULL,
    level    TEXT NOT NULL,
    message  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run ON run_events(run_id, id);

CREATE TABLE IF NOT EXISTS findings (
    id          {pk},
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    target_id   INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    check_id    TEXT NOT NULL,
    severity    TEXT NOT NULL,
    title       TEXT NOT NULL,
    endpoint    TEXT NOT NULL,
    description TEXT NOT NULL,
    evidence    TEXT NOT NULL,
    repro       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_findings_user ON findings(user_id);
CREATE INDEX IF NOT EXISTS idx_findings_run ON findings(run_id);
"""


def init_db() -> None:
    # Emails are always stored lower-cased, so Postgres doesn't need a case-insensitive collation.
    schema = _SCHEMA.format(
        pk="SERIAL PRIMARY KEY" if USE_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT",
        nocase="" if USE_POSTGRES else " COLLATE NOCASE",
    )
    with closing(get_db()) as conn, conn:
        if not USE_POSTGRES:
            # WAL lets the dashboard read live events while a scan thread writes them.
            conn.execute("PRAGMA journal_mode = WAL")
        for statement in (s.strip() for s in schema.split(";")):
            if statement:
                conn.execute(statement)
        # Scans run in background threads; any left "running" by a restart can't finish.
        conn.execute(
            "UPDATE runs SET status = 'interrupted', error = 'Server restarted during the scan' "
            "WHERE status IN ('queued', 'running')"
        )
