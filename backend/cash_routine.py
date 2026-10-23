"""Déclenchement immédiat de la tâche Claude planifiée « Analyse des dépôts d'espèces » (déclencheur API).

L'adresse et le jeton du déclencheur sont créés dans claude.ai/code/routines. Le dépôt étant public, ils
sont gardés scellés (chiffrés par le serveur) dans config.json, cash.sealed_routine_fire, au format
JSON {"url": "...", "token": "..."} ; ou en clair dans les variables CASH_ROUTINE_FIRE_URL / CASH_ROUTINE_TOKEN.
Documentation : https://code.claude.com/docs/en/routines#trigger-a-scheduled-task
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

log = logging.getLogger("cash.routine")

BETA = "experimental-cc-routine-2026-04-01"


def _credentials() -> Optional[tuple[str, str]]:
    url, token = os.getenv("CASH_ROUTINE_FIRE_URL"), os.getenv("CASH_ROUTINE_TOKEN")
    if url and token:
        return url, token
    try:
        from .cash_config import CONFIG_PATH
        from .sealed import unseal

        sealed = None
        try:  # d'abord la base (réglé par PUT /cash/admin/routine), puis config.json
            from .cash_models import CashSetting
            from .database import SessionLocal

            db = SessionLocal()
            try:
                row = db.get(CashSetting, "sealed_routine_fire")
                sealed = json.loads(row.value) if row else None
            finally:
                db.close()
        except Exception:
            sealed = None
        if not sealed:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            sealed = (cfg.get("cash") or {}).get("sealed_routine_fire")
        raw = unseal(sealed)
        if raw:
            data = json.loads(raw)
            return data["url"], data["token"]
    except Exception as e:  # noqa: BLE001
        log.warning("déclencheur de la tâche illisible : %s", e)
    return None


def store(db, url: str, token: str) -> None:
    """Enregistre l'adresse et le jeton du déclencheur, scellés, en base."""
    from .cash_service import set_setting
    from .sealed import seal

    if not url.startswith("https://api.anthropic.com/") or not url.rstrip("/").endswith("/fire"):
        raise ValueError("Adresse de déclenchement inattendue (https://api.anthropic.com/v1/claude_code/routines/<id>/fire)")
    if len(token.strip()) < 20:
        raise ValueError("Jeton trop court")
    set_setting(db, "sealed_routine_fire", seal(json.dumps({"url": url.strip(), "token": token.strip()})))


def configured() -> bool:
    return _credentials() is not None


def fire(text: str = "") -> dict:
    """Lance la tâche tout de suite. Retourne {"ok", "session_url" | "error"}."""
    creds = _credentials()
    if not creds:
        return {"ok": False, "error": "déclencheur non configuré"}
    url, token = creds
    import httpx

    try:
        r = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": BETA,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={"text": text} if text else {},
            timeout=30,
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"appel impossible : {type(e).__name__}"}
    if r.status_code >= 300:
        detail = ""
        try:
            detail = (r.json().get("error") or {}).get("message") or ""
        except Exception:
            detail = r.text[:200]
        return {"ok": False, "error": f"refus {r.status_code} {detail}".strip()}
    data = r.json() if r.content else {}
    return {"ok": True, "session_url": data.get("claude_code_session_url")}
