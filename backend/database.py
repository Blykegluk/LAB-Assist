import logging
import os
import re
import time
from typing import Generator, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker, DeclarativeBase

log = logging.getLogger("database")


class Base(DeclarativeBase):
    pass


# État de la connexion, exposé (sans mot de passe) par le diagnostic d'administration
DB_DIAG: dict = {"configured": False, "original_host": None, "used_host": None, "attempts": []}

SUPABASE_DIRECT = re.compile(r"^db\.([a-z0-9]{20})\.supabase\.co$")
SUPABASE_REGIONS = [
    "eu-west-1", "eu-west-2", "eu-west-3", "eu-central-1", "eu-central-2", "eu-north-1",
    "us-east-1", "us-east-2", "us-west-1", "us-west-2", "ca-central-1", "sa-east-1",
    "ap-southeast-1", "ap-southeast-2", "ap-northeast-1", "ap-northeast-2", "ap-south-1",
]


def _normalize(url: str) -> str:
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _short_error(e: Exception) -> str:
    msg = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    return msg[:300]


def _can_connect(url, timeout: int = 6) -> Optional[str]:
    """Retourne None si la connexion marche, sinon le message d'erreur."""
    try:
        eng = create_engine(url, connect_args={"connect_timeout": timeout}, pool_pre_ping=True)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
        return None
    except Exception as e:  # noqa: BLE001
        return _short_error(e)


def _supabase_pooler_candidates(url):
    """Une URL Supabase « directe » (db.<ref>.supabase.co) n'est joignable qu'en IPv6, ce que Render
    gratuit ne sait pas faire. Le pooler (Supavisor) offre le même accès en IPv4 : même mot de passe,
    utilisateur suffixé par la référence du projet."""
    m = SUPABASE_DIRECT.match(url.host or "")
    if not m:
        return []
    ref = m.group(1)
    user = url.username or "postgres"
    if "." not in user:
        user = f"{user}.{ref}"
    region_hint = os.getenv("SUPABASE_REGION")
    regions = ([region_hint] if region_hint else []) + [r for r in SUPABASE_REGIONS if r != region_hint]
    out = []
    for region in regions:
        for n in (0, 1):
            out.append(url.set(host=f"aws-{n}-{region}.pooler.supabase.com", port=5432, username=user))
    return out


def _get_database_url() -> str:
    raw = (os.getenv("DATABASE_URL") or "").strip()
    if not raw:
        return "sqlite:///./data.db"  # développement local
    DB_DIAG["configured"] = True
    url = make_url(_normalize(raw))
    DB_DIAG["original_host"] = url.host
    DB_DIAG["user"] = url.username
    DB_DIAG["database"] = url.database
    if os.getenv("DB_SKIP_PROBE") in ("1", "true"):
        DB_DIAG["used_host"] = url.host
        return url.render_as_string(hide_password=False)
    err = _can_connect(url)
    DB_DIAG["attempts"].append({"host": url.host, "error": err})
    if err is None:
        DB_DIAG["used_host"] = url.host
        return url.render_as_string(hide_password=False)
    deadline = time.time() + 60
    for cand in _supabase_pooler_candidates(url):
        if time.time() > deadline:
            break
        cerr = _can_connect(cand, timeout=4)
        DB_DIAG["attempts"].append({"host": cand.host, "error": cerr})
        if cerr is None:
            DB_DIAG["used_host"] = cand.host
            log.warning("Base joignable via le pooler Supabase %s", cand.host)
            return cand.render_as_string(hide_password=False)
        # « Tenant or user not found » : mauvaise région, on continue ; autre erreur d'authentification : inutile d'insister
        if "password authentication failed" in (cerr or ""):
            break
    log.error("Base de données injoignable : %s", err)
    return url.render_as_string(hide_password=False)


DATABASE_URL = _get_database_url()

# SQLite needs check_same_thread=False for multiple threads in dev
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite:") else {"connect_timeout": 10}

engine = create_engine(DATABASE_URL, echo=False, future=True, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
