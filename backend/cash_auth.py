"""Authentification minimaliste : identifiant + code, sans session serveur (jetons signés HMAC).

Comptes définis dans la variable d'env CASH_USERS, au format
    identifiant:code:role[:libellé],identifiant:code:role[:libellé],...
avec role = owner (gérant, accès au tableau de bord) ou manager (responsable, envoi des dépôts).
Exemple :  CASH_USERS="1:8626:owner:Anthony,2:1234:manager:Responsable,3:1234:manager:Adjoint"

Sans CASH_USERS, les comptes par défaut ci-dessous sont utilisés (gérant 1/8626, responsable 2/1234).
CASH_SECRET signe les jetons (mettre une longue chaîne aléatoire en production).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, Request

log = logging.getLogger("cash.auth")

TOKEN_TTL = 60 * 60 * 24 * 90  # 90 jours : la responsable ne doit pas ressaisir son code
DEFAULT_USERS = "1:8626:owner:Anthony,2:1234:manager:Responsable"
ROLES = ("owner", "manager")


@dataclass(frozen=True)
class User:
    id: str
    code: str
    role: str
    label: str


def users() -> dict[str, User]:
    raw = os.getenv("CASH_USERS") or DEFAULT_USERS
    if not os.getenv("CASH_USERS"):
        log.warning("CASH_USERS non défini : comptes par défaut utilisés")
    out: dict[str, User] = {}
    for item in raw.split(","):
        parts = [p.strip() for p in item.strip().split(":")]
        if len(parts) < 3 or parts[2] not in ROLES or not parts[0] or not parts[1]:
            if item.strip():
                log.warning("Entrée CASH_USERS ignorée : %r", item)
            continue
        label = parts[3] if len(parts) > 3 and parts[3] else ("Gérant" if parts[2] == "owner" else "Responsable")
        out[parts[0]] = User(id=parts[0], code=parts[1], role=parts[2], label=label)
    return out


def _secret() -> bytes:
    s = os.getenv("CASH_SECRET") or ("lab-assist-dev-secret:" + (os.getenv("CASH_USERS") or DEFAULT_USERS))
    return s.encode("utf-8")


def authenticate(user_id: str, code: str) -> Optional[User]:
    u = users().get((user_id or "").strip())
    if not u:
        return None
    if not hmac.compare_digest((code or "").strip(), u.code):
        return None
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


def current_user(request: Request) -> Optional[User]:
    auth = request.headers.get("authorization") or ""
    token = auth[7:] if auth.lower().startswith("bearer ") else request.query_params.get("token", "")
    return parse_token(token) if token else None


def require_any(request: Request) -> User:
    u = current_user(request)
    if not u:
        raise HTTPException(status_code=401, detail="Connexion requise")
    return u


def worker_token() -> str:
    """Jeton de la tâche Claude planifiée qui analyse les photos (accès limité à la file d'analyse).

    CASH_WORKER_TOKEN s'il est défini, sinon dérivé de CASH_SECRET (rien de plus à configurer).
    """
    explicit = os.getenv("CASH_WORKER_TOKEN")
    if explicit:
        return explicit
    return hmac.new(_secret(), b"cash-worker", hashlib.sha256).hexdigest()


def require_worker(request: Request) -> str:
    """Accepte le jeton de la tâche d'analyse, ou un gérant connecté (pour lancer l'analyse à la main)."""
    auth = request.headers.get("authorization") or ""
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    if token and hmac.compare_digest(token, worker_token()):
        return "worker"
    u = parse_token(token) if token else None
    if u and u.role == "owner":
        return f"owner:{u.id}"
    raise HTTPException(status_code=401, detail="Jeton d'analyse invalide")


def require_owner(request: Request) -> User:
    u = current_user(request)
    if not u:
        raise HTTPException(status_code=401, detail="Connexion requise")
    if u.role != "owner":
        raise HTTPException(status_code=403, detail="Réservé au gérant")
    return u
