"""
Seed logic as a reusable function — used by seed.py (manual) and by main.py on startup
when SEED_ON_START=1 (so a fresh cloud deployment has its first unit + login users without
needing shell access). Idempotent: skips anything that already exists.
"""
from .db import SessionLocal
from .models import Unit, User
from .security import hash_password


def run_seed():
    db = SessionLocal()
    try:
        def get_or_create_unit(name):
            u = db.query(Unit).filter(Unit.name == name).first()
            if not u:
                u = Unit(name=name); db.add(u); db.commit(); db.refresh(u)
                print(f"  created unit: {name}")
            return u

        def get_or_create_user(username, password, display, role, units):
            username = username.strip().lower()
            u = db.query(User).filter(User.username == username).first()
            if not u:
                u = User(username=username, password_hash=hash_password(password),
                         display_name=display, role=role, active=True)
                u.units = units
                db.add(u); db.commit()
                print(f"  created user: {username} ({role})")
            return u

        print("Seeding Cerebro data...")
        unit1 = get_or_create_unit("Unit 1")
        get_or_create_user("admin", "admin123", "Admin", "admin", [unit1])
        get_or_create_user("anita", "test123", "Dr Anita", "user", [unit1])
        get_or_create_user("ravi", "test123", "Dr Ravi", "user", [unit1])
        print("Seed complete.")
    finally:
        db.close()
