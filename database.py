import os
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase


class Base(DeclarativeBase):
    pass


def _get_database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if url and url.strip():
        # Support for postgres URLs without driver
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+psycopg://", 1)
        elif url.startswith("postgresql://") and "+" not in url.split("://", 1)[1].split(":", 1)[0]:
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        return url
    # Fallback local SQLite for dev
    return "sqlite:///./data.db"


DATABASE_URL = _get_database_url()

# SQLite needs check_same_thread=False for multiple threads in dev
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite:") else {}

engine = create_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    connect_args=connect_args,
    pool_pre_ping=True,           # ré-ouvre auto les connexions rompues (pgbouncer/pooler)
    pool_recycle=300,             # recycle connexions inactives (en secondes)
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

