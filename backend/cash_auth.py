"""Authentification : identifiant + code, jetons signés HMAC, sans session serveur.

Le dépôt est public : aucun code ni secret n'est écrit dans le code ou la configuration versionnée.
  - Comptes : variable d'env CASH_USERS (identifiant:code:role[:libellé],...) si elle existe, sinon le
    réglage « users » en base, où les codes sont stockés hachés (PBKDF2). Ils se définissent avec
    PUT /cash/admin/users (jeton d'administration).
  - Secret de signature : CASH_SECRET si défini, sinon un secret aléatoire créé et gardé en base.
  - Jetons d'administration et de la tâche d'analyse : seule leur empreinte SHA-256 est versionnée
    (config.json, cash.admin_token_sha256 et cash.worker_token_sha256).
role = owner (gérant, tableau de bord) ou manager (responsable, envoi des dépôts).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, Request

log = logging.getLogger("cash.auth")

TOKEN_TTL = 60 * 60 * 24 * 90  # 90 jours : la responsable ne doit pas ressaisir son code
ROLES = ("owner", "manager")
PBKDF2_ITER = 200_000
CACHE_TTL = 30.0
MAX_FAILURES = 5
FAILURE_WINDOW = 15 * 60

_lock = threading.Lock()
_cache: dict = {}
_failures: dict[str, list[float]] = {}


@dataclass(frozen=True)
class User:
    id: str
    code: str  # code en clair (CASH_USERS) ou empreinte « pbkdf2$... » (base)
    role: str
    label: str


# ----------------------------------------------------------------------------- stockage en base

def _setting(key: str):
    from .database import SessionLocal
    from .cash_models import CashSetting

    db = SessionLocal()
    try:
        row = db.get(CashSetting, key)
        return json.loads(row.value) if row else None
    finally:
        db.close()


def _set_setting(key: str, value) -> None:
    from .database import SessionLocal
    from .cash_models import CashSetting

    db = SessionLocal()
    try:
        row = db.get(CashSetting, key)
        if row:
            row.value = json.dumps(value)
        else:
            db.add(CashSetting(key=key, value=json.dumps(value)))
        db.commit()
    finally:
        db.close()
    with _lock:
        _cache.pop(key, None)


def _cached(key: str):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
    try:
        value = _setting(key)
    except Exception as e:  # base injoignable : on garde la dernière valeur connue
        log.warning("lecture du réglage %s impossible : %s", key, e)
        return hit[1] if hit else None
    with _lock:
        _cache[key] = (now, value)
    return value


def clear_cache() -> None:
    with _lock:
        _cache.clear()


# ----------------------------------------------------------------------------- codes

def hash_code(code: str) -> str:
    salt = secrets.token_hex(8)
    dk = hashlib.pbkdf2_hmac("sha256", code.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITER)
    return f"pbkdf2${PBKDF2_ITER}${salt}${dk.hex()}"


def _check_code(given: str, stored: str) -> bool:
    if stored.startswith("pbkdf2$"):
        try:
            _, it, salt, hexdk = stored.split("$", 3)
            dk = hashlib.pbkdf2_hmac("sha256", given.encode("utf-8"), bytes.fromhex(salt), int(it))
            return hmac.compare_digest(dk.hex(), hexdk)
        except Exception:
            return False
    return hmac.compare_digest(given, stored)


def parse_users_spec(raw: str) -> list[dict]:
    out = []
    for item in (raw or "").split(","):
        parts = [p.strip() for p in item.strip().split(":")]
        if len(parts) < 3 or parts[2] not in ROLES or not parts[0] or not parts[1]:
            if item.strip():
                raise ValueError(f"Entrée de compte invalide : {item.strip()!r} (format identifiant:code:role[:libellé])")
            continue
        label = parts[3] if len(parts) > 3 and parts[3] else ("Gérant" if parts[2] == "owner" else "Responsable")
        out.append({"id": parts[0], "code": parts[1], "role": parts[2], "label": label})
    return out


def users() -> dict[str, User]:
    env = os.getenv("CASH_USERS")
    if env:
        try:
            return {u["id"]: User(**u) for u in parse_users_spec(env)}
        except ValueError as e:
            log.error("CASH_USERS invalide : %s", e)
            return {}
    stored = _cached("users") or []
    return {u["id"]: User(id=u["id"], code=u["code_hash"], role=u["role"], label=u.get("label") or u["id"]) for u in stored}


def store_users(spec: str) -> list[dict]:
    """Enregistre les comptes en base (codes hachés). Retourne la liste sans les codes."""
    parsed = parse_users_spec(spec)
    if not any(u["role"] == "owner" for u in parsed):
        raise ValueError("Il faut au moins un compte gérant (role owner)")
    rows = [{"id": u["id"], "code_hash": hash_code(u["code"]), "role": u["role"], "label": u["label"]} for u in parsed]
    _set_setting("users", rows)
    return [{"id": r["id"], "role": r["role"], "label": r["label"]} for r in rows]


# ----------------------------------------------------------------------------- secret de signature

_memory_secret: Optional[str] = None


def _secret() -> bytes:
    global _memory_secret
    env = os.getenv("CASH_SECRET")
    if env:
        return env.encode("utf-8")
    stored = _cached("secret")
    if stored:
        return str(stored).encode("utf-8")
    new = secrets.token_urlsafe(48)
    try:
        _set_setting("secret", new)
        return new.encode("utf-8")
    except Exception as e:
        # Sans base, un secret en mémoire : les connexions tiennent jusqu'au prochain redémarrage
        log.warning("secret non enregistré en base (%s) : secret temporaire en mémoire", e)
        if not _memory_secret:
            _memory_secret = new
        return _memory_secret.encode("utf-8")


# ----------------------------------------------------------------------------- connexion

def _throttled(user_id: str) -> bool:
    now = time.time()
    with _lock:
        recent = [t for t in _failures.get(user_id, []) if now - t < FAILURE_WINDOW]
        _failures[user_id] = recent
        return len(recent) >= MAX_FAILURES


def _record_failure(user_id: str) -> None:
    with _lock:
        _failures.setdefault(user_id, []).append(time.time())


def authenticate(user_id: str, code: str) -> Optional[User]:
    """None si identifiant ou code faux. Lève HTTPException 429 après trop d'essais."""
    uid = (user_id or "").strip()
    if _throttled(uid):
        raise HTTPException(status_code=429, detail="Trop d'essais. Réessaie dans un quart d'heure.")
    u = users().get(uid)
    if not u or not _check_code((code or "").strip(), u.code):
        _record_failure(uid)
        return None
    with _lock:
        _failures.pop(uid, None)
    return u


def make_token(user: User) -> str:
    exp = int(time.time()) + TOKEN_TTL
    payload = f"{user.id}|{user.role}|{exp}".encode("utf-8")
    sig = hmac.new(_secret(), payload, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(payload + b"|" + sig.encode("ascii")).decode("ascii")


def parse_token(token: str) -> Optional[User]:
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        uid, role, exp, sig = raw.split("|", 3)
        payload = f"{uid}|{role}|{exp}".encode("utf-8")
        expected = hmac.new(_secret(), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected) or int(exp) < time.time():
            return None
        u = users().get(uid)
        if not u or u.role != role:
            return None  # compte supprimé ou rôle modifié depuis l'émission du jeton
        return u
    except Exception:
        return None


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization") or ""
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


def current_user(request: Request) -> Optional[User]:
    token = _bearer(request) or request.query_params.get("token", "")
    return parse_token(token) if token else None


def require_any(request: Request) -> User:
    u = current_user(request)
    if not u:
        raise HTTPException(status_code=401, detail="Connexion requise")
    return u


def require_owner(request: Request) -> User:
    u = current_user(request)
    if not u:
        raise HTTPException(status_code=401, detail="Connexion requise")
    if u.role != "owner":
        raise HTTPException(status_code=403, detail="Réservé au gérant")
    return u


# ----------------------------------------------------------------------------- jetons techniques

def _config_hash(key: str) -> Optional[str]:
    from .cash_config import cash_config

    return (cash_config().get(key) or None)


def _matches_hash(token: str, expected_sha256: Optional[str]) -> bool:
    if not token or not expected_sha256:
        return False
    return hmac.compare_digest(hashlib.sha256(token.encode("utf-8")).hexdigest(), expected_sha256)


def worker_token_valid(token: str) -> bool:
    explicit = os.getenv("CASH_WORKER_TOKEN")
    if explicit and token and hmac.compare_digest(token, explicit):
        return True
    return _matches_hash(token, _config_hash("worker_token_sha256"))


def require_worker(request: Request) -> str:
    """Jeton de la tâche Claude planifiée qui analyse les photos, ou un gérant connecté."""
    token = _bearer(request)
    if worker_token_valid(token):
        return "worker"
    u = parse_token(token) if token else None
    if u and u.role == "owner":
        return f"owner:{u.id}"
    raise HTTPException(status_code=401, detail="Jeton d'analyse invalide")


def require_admin(request: Request) -> str:
    """Jeton d'administration (configuration à distance), ou CASH_ADMIN_TOKEN s'il est défini."""
    token = _bearer(request)
    explicit = os.getenv("CASH_ADMIN_TOKEN")
    if explicit and token and hmac.compare_digest(token, explicit):
        return "admin"
    if _matches_hash(token, _config_hash("admin_token_sha256")):
        return "admin"
    raise HTTPException(status_code=401, detail="Jeton d'administration invalide")
