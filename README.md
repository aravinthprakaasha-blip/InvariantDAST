# InvariantDAST

Security testing for stateful B2B travel APIs. It drives a booking through its lifecycle
(search → quote → hold → issue → amend → refund) with two tenant personas, tries the
transitions that should be impossible, and reports only findings it has proven with a
real request, each with evidence and a `curl` repro. Every test booking it creates is
rolled back.

**Live site:** https://invariantdast.vercel.app

## Layout

| Path | What it is |
|---|---|
| `frontend/` | Website, login and dashboard (static; deployed on Vercel) |
| `backend/` | FastAPI API: accounts, targets, live scan stream, findings |
| `backend/scanner.py` | The scan engine (12 business-logic and access-control checks) |
| `sample_travel_api/main.py` | Secure demo travel API: every check should pass |
| `sample_travel_api/vulnerable_main.py` | **Deliberately vulnerable** demo API, for showing findings. Never deploy it for real use. |
| `render.yaml` | Render Blueprint: backend, Postgres and the two demo APIs |

## Run locally

```bash
python -m venv backend/venv
backend/venv/Scripts/pip install -r requirements.txt     # Windows (use bin/ on macOS/Linux)
backend/venv/Scripts/python -m uvicorn main:app --app-dir backend --port 8000 --reload
backend/venv/Scripts/python -m uvicorn sample_travel_api.main:app --port 8001
backend/venv/Scripts/python -m uvicorn sample_travel_api.vulnerable_main:app --port 8003
python scripts/serve_frontend.py 5173
```

Open http://localhost:5173. Locally the backend uses SQLite (`backend/data/`) and can scan any URL.

## Hosted configuration

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Postgres connection string (otherwise SQLite is used) |
| `INVARIANT_ALLOWED_TARGETS` | Comma-separated origins the server may scan. Always set it on a hosted server. |
| `INVARIANT_CORS_ORIGINS` | Website origins allowed to call the API |
| `INVARIANT_SECURE_COOKIES` | `1` when served over HTTPS |

Only scan APIs you own or have written permission to test.
