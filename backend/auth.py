"""Email + password accounts with server-side sessions in an HttpOnly cookie."""

import base64
import hashlib
import hmac
import os
import secrets
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Cookie, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator

from db import IntegrityError, get_db, normalize_email

router = APIRouter(prefix="/api/auth", tags=["auth"])

SESSION_COOKIE = "invariant_session"
SESSION_TTL = timedelta(days=7)
# Set INVARIANT_SECURE_COOKIES=1 when serving over HTTPS.
SECURE_COOKIES = os.environ.get("INVARIANT_SECURE_COOKIES", "0") == "1"

# Brute-force guard: max failed logins per email within the window (in-memory, per process).
MAX_FAILED_LOGINS = 5
LOCKOUT_WINDOW_SECONDS = 15 * 60
_failed_logins: dict[str, list[float]] = {}

# -----------------------------------
# PASSWORD HASHING (scrypt)
# -----------------------------------

_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_b64, digest_b64 = stored.split("$")
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(digest_b64)
    except ValueError:
        return False
    actual = hashlib.scrypt(password.encode(), salt=salt, dklen=len(expected), **_SCRYPT)
    return hmac.compare_digest(actual, expected)


# Used to keep login timing similar whether or not the email exists.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))

# -----------------------------------
# SESSIONS
# -----------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _start_session(response: Response, user_id: int) -> None:
    token = secrets.token_urlsafe(32)
    now = _now()
    with closing(get_db()) as conn, conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now.isoformat(),))
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (_token_hash(token), user_id, now.isoformat(), (now + SESSION_TTL).isoformat()),
        )
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True, samesite="lax", secure=SECURE_COOKIES, path="/",
    )


def _user_from_token(token: str | None):
    if not token:
        return None
    with closing(get_db()) as conn:
        return conn.execute(
            """
            SELECT u.id, u.name, u.email, u.company, u.created_at
            FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ? AND s.expires_at > ?
            """,
            (_token_hash(token), _now().isoformat()),
        ).fetchone()


def _public(user) -> dict:
    return {k: user[k] for k in ("id", "name", "email", "company", "created_at")}


def _too_many_failures(email: str) -> bool:
    cutoff = time.monotonic() - LOCKOUT_WINDOW_SECONDS
    attempts = [t for t in _failed_logins.get(email, []) if t > cutoff]
    _failed_logins[email] = attempts
    return len(attempts) >= MAX_FAILED_LOGINS


# -----------------------------------
# MODELS
# -----------------------------------


class SignupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=254)
    company: str | None = Field(default=None, max_length=120)
    password: str = Field(min_length=10, max_length=128)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        return normalize_email(value)

    @field_validator("name", "company")
    @classmethod
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("password")
    @classmethod
    def password_strength(cls, value: str) -> str:
        if value.isalpha() or value.isdigit():
            raise ValueError("Use a mix of letters and numbers or symbols")
        return value


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def clean_email(cls, value: str) -> str:
        return value.strip().lower()


# -----------------------------------
# ROUTES
# -----------------------------------


@router.post("/signup", status_code=201)
def signup(body: SignupRequest, response: Response):
    try:
        with closing(get_db()) as conn, conn:
            user_id = conn.execute(
                "INSERT INTO users (name, email, company, password_hash, created_at) VALUES (?, ?, ?, ?, ?) RETURNING id",
                (body.name, body.email, body.company or None, hash_password(body.password), _now().isoformat(timespec="seconds")),
            ).fetchall()[0]["id"]
    except IntegrityError:
        raise HTTPException(409, "An account with this email already exists. Try signing in.")
    _start_session(response, user_id)
    return {"user": {"id": user_id, "name": body.name, "email": body.email, "company": body.company or None}}


@router.post("/login")
def login(body: LoginRequest, response: Response):
    if _too_many_failures(body.email):
        raise HTTPException(429, "Too many failed attempts. Try again in 15 minutes.")
    with closing(get_db()) as conn:
        user = conn.execute(
            "SELECT id, name, email, company, created_at, password_hash FROM users WHERE email = ?",
            (body.email,),
        ).fetchone()
    valid = verify_password(body.password, user["password_hash"] if user else _DUMMY_HASH)
    if not user or not valid:
        _failed_logins.setdefault(body.email, []).append(time.monotonic())
        raise HTTPException(401, "Invalid email or password.")
    _failed_logins.pop(body.email, None)
    _start_session(response, user["id"])
    return {"user": _public(user)}


@router.post("/logout")
def logout(response: Response, invariant_session: str | None = Cookie(default=None)):
    if invariant_session:
        with closing(get_db()) as conn, conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(invariant_session),))
    response.delete_cookie(SESSION_COOKIE, path="/", samesite="lax", secure=SECURE_COOKIES, httponly=True)
    return {"status": "signed_out"}


def require_user(request: Request) -> dict:
    """FastAPI dependency: the signed-in user, or 401."""
    user = _user_from_token(request.cookies.get(SESSION_COOKIE))
    if not user:
        raise HTTPException(401, "Not signed in.")
    return _public(user)


@router.get("/me")
def me(request: Request):
    return {"user": require_user(request)}


@router.get("/session")
def session(request: Request):
    """Like /me, but answers 200 either way — for pages that only need to know whether to redirect."""
    user = _user_from_token(request.cookies.get(SESSION_COOKIE))
    return {"signed_in": bool(user), "user": _public(user) if user else None}
