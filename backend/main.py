
import os
from contextlib import closing
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

import auth
import scans
from db import DBError, get_db, init_db, normalize_email

app = FastAPI(
    title="InvariantDAST",
    description="Autonomous Neuro-Symbolic Dynamic Security Testing Platform",
    version="1.0.0"
)

# Origins allowed to call the API from a browser (the website).
# Override with a comma-separated INVARIANT_CORS_ORIGINS env var in other environments.
ALLOWED_ORIGINS = os.environ.get(
    "INVARIANT_CORS_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173",
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in ALLOWED_ORIGINS if o.strip()],
    allow_credentials=True,  # needed for the session cookie
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type"],
)


class NoStoreAPI:
    """API responses are per-user: never let a browser or CDN (e.g. a Vercel rewrite) cache them.
    Pure ASGI (not BaseHTTPMiddleware) so the live scan stream is passed through untouched."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(NoStoreAPI)


init_db()
app.include_router(auth.router)
app.include_router(scans.router)

# -----------------------------------
# MODELS
# -----------------------------------


class PilotRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return normalize_email(value)


# -----------------------------------
# ROUTES
# -----------------------------------

@app.get("/")
def home():
    return {
        "project": "InvariantDAST",
        "status": "Running",
        "message": "Security testing platform initialized"
    }


@app.get("/health")
def health():
    return {
        "status": "healthy",
        "application": "InvariantDAST"
    }


@app.post("/api/pilot-requests", status_code=201)
def create_pilot_request(request: PilotRequest, response: Response):
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with closing(get_db()) as conn, conn:
            cur = conn.execute(
                "INSERT INTO pilot_requests (email, created_at) VALUES (?, ?) "
                "ON CONFLICT(email) DO NOTHING",
                (request.email, created_at),
            )
            already_registered = cur.rowcount == 0
    except DBError:
        raise HTTPException(500, "Could not save your request. Please try again.")

    if already_registered:
        response.status_code = 200

    return {
        "email": request.email,
        "status": "already_registered" if already_registered else "received",
    }
