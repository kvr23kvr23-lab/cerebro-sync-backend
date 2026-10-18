"""
Database setup — SQLAlchemy engine + session.

Uses DATABASE_URL from the environment. For local testing without Postgres it falls
back to a local SQLite file, so the whole backend can be run and tested on a laptop
with zero setup; in production (Render/Railway + Postgres) DATABASE_URL points at Postgres.
"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# Postgres in production; SQLite file for zero-setup local testing.
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./cerebro_local.db")

# Render/Heroku sometimes give "postgres://"; SQLAlchemy needs "postgresql://".
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI dependency: one DB session per request, always closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
