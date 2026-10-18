"""
Seed script — creates the first unit, an admin, and two test users, for Stage 1 testing.
Run once: `python seed.py`. Safe to re-run (skips anything that already exists).
Change the passwords before any real use. (On a cloud deploy, SEED_ON_START=1 does this
automatically on startup — see app/main.py.)
"""
from app.db import Base, engine
from app.seed_data import run_seed

Base.metadata.create_all(bind=engine)
run_seed()
