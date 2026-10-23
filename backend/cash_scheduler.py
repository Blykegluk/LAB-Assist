"""Contrôles périodiques en tâche de fond (thread) : jours non traités, dépôts non crédités, sheets, récap e-mail.

Désactivable avec CASH_SCHEDULER=0 (par exemple si un cron externe appelle POST /cash/checks/run).
"""
from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger("cash.scheduler")

INTERVAL_SECONDS = int(os.getenv("CASH_CHECKS_INTERVAL_SECONDS") or 15 * 60)
_started = False


def _loop() -> None:
    from .database import SessionLocal
    from .cash_service import run_checks

    time.sleep(20)  # laisse l'application démarrer
    while True:
        db = SessionLocal()
        try:
            res = run_checks(db)
            log.info("contrôles exécutés: %s", {k: v for k, v in res.items() if k != "stores"})
        except Exception:
            log.exception("contrôles périodiques en erreur")
        finally:
            db.close()
        time.sleep(INTERVAL_SECONDS)


def start() -> None:
    global _started
    # Désactivé par défaut : les contrôles se font aux créneaux de la tâche Claude (12h05, 18h05)
    # et à la demande (« Analyser maintenant »). CASH_SCHEDULER=1 réactive le contrôle toutes les 15 min.
    if _started or os.getenv("CASH_SCHEDULER", "0") in ("0", "false", "no"):
        return
    _started = True
    t = threading.Thread(target=_loop, name="cash-checks", daemon=True)
    t.start()
