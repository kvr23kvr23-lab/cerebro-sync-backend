# Cerebro Multi-User Backend (Stage 1)

Shared data server for ONE institution: units, users, unit-scoped patients & clinical
records, with login and sync. One deployment = one institution (its own server + database).

## What works (live-tested)
- Login (username/password -> token), /api/auth/me, logout
- Patients: create / list / get / delete -- auto-scoped to the user's unit
- Clinical records (notes, investigations, etc.): create / list / delete -- unit-scoped
- Sync: /api/sync?since=<ISO time> returns everything in the unit changed since then
- Unit isolation enforced on the server (a user never receives another unit's data)

## Run locally (SQLite, zero setup)
    pip install -r requirements.txt
    python seed.py                       # creates Unit 1 + admin/anita/ravi (test passwords)
    uvicorn app.main:app --reload --port 8000
API at http://127.0.0.1:8000  (health: /api/health, interactive docs: /docs)

Test logins (CHANGE before any real use):
  admin / admin123   (role: admin)
  anita / test123    (Unit 1)
  ravi  / test123    (Unit 1)

## Deploy (production -- each institution)
Set environment variables:
  DATABASE_URL     -> a Postgres URL (managed Postgres)
  JWT_SECRET       -> a long random string (signs login tokens; keep secret)
  TOKEN_TTL_HOURS  -> optional, default 720 (30 days)
Start command: uvicorn app.main:app --host 0.0.0.0 --port $PORT
Without DATABASE_URL it uses a local SQLite file -- fine for testing, not production.

## Notes
- Clinical payloads stored as JSON (data) so the server stays generic.
- Nothing hard-deleted (soft deleted flag) -- history preserved; sync tells peers of removals.
- Stage 2 adds the admin screen; admin endpoints stubbed, require_admin ready.
