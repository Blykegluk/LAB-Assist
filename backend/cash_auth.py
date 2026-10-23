"""Authentification minimaliste : un code PIN pour la responsable, un mot de passe pour le gérant.

Jetons signés HMAC, sans session serveur. Variables d'env :
  CASH_MANAGER_PIN      code de la responsable (défaut: 1234, à changer !)
  CASH_OWNER_PASSWORD   mot de passe du gérant (défaut: admin, à changer !)
  CASH_SECRET           secret de signature des jetons
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
from typing import Optional

from fastapi import Depends, HTTPException, Request

log = logging.getLogger("cash.auth")

TOKEN_TTL = 60 * 60 * 24 * 90  # 90 jours : la responsable ne doit pas ressaisir son code


def _secret() -> bytes:
    s = os.getenv("CASH_SECRET") or (os.getenv("CASH_OWNER_PASSWORD") or "lab-assist-dev-secret")
    return s.encode("utf-8")


def _manager_pin() -> str:
    v = os.getenv("CASH_MANAGER_PIN")
    if not v:
        log.warning("CASH_MANAGER_PIN non défini : PIN par défaut 1234 utilisé")
        return "1234"
    return v


def _owner_password() -> str:
    v = os.getenv("CASH_OWNER_PASSWORD")
    if not v:
        log.warning("CASH_OWNER_PASSWORD non défini : mot de passe par défaut 'admin' utilisé")
        return "admin"
    return v


def role_for_code(code: str) -> Optional[str]:
    code = (code or "").strip()
    if not code:
        return None
    if hmac.compare_digest(code, _owner_password()):
        return "owner"
    if hmac.compare_digest(code, _manager_pin()):
        return "manager"
    return None


def make_token(role: str) -> str:
    exp = int(time.time()) + TOKEN_TTL
    payload = f"{role}:{exp}".encode("utf-8")
    sig = hmac.new(_secret(), payload, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(payload + b":" + sig.encode("ascii")).decode("ascii")


def parse_token(token: str) -> Optional[str]:
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        role, exp, sig = raw.split(":", 2)
        payload = f"{role}:{exp}".encode("utf-8")
        expected = hmac.new(_secret(), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        if int(exp) < time.time():
            return None
        if role not in ("manager", "owner"):
            return None
        return role
    except Exception:
        return None


def current_role(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization") or ""
    token = auth[7:] if auth.lower().startswith("bearer ") else request.query_params.get("token", "")
    return parse_token(token) if token else None


def require_any(request: Request) -> str:
    role = current_role(request)
    if not role:
        raise HTTPException(status_code=401, detail="Connexion requise")
    return role


def require_owner(request: Request) -> str:
    role = current_role(request)
    if role != "owner":
        raise HTTPException(status_code=403, detail="Réservé au gérant")
    return role
