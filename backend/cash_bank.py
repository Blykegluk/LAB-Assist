"""Rapprochement bancaire via l'API Pennylane (v2).

Chaque dépôt d'espèces apparaît sur le relevé Caisse d'Épargne sous la forme
"DPT VRAC ESP 0005179418  211026" : le numéro de sac permet un rapprochement exact.

Clés API (une par société) :
  PENNYLANE_API_KEY_AEJB, PENNYLANE_API_KEY_JAB  (ou PENNYLANE_API_KEY commune)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta
from typing import Optional

log = logging.getLogger("cash.bank")

API_BASE = os.getenv("PENNYLANE_API_BASE", "https://app.pennylane.com/api/external/v2")


def api_key_for(entity: Optional[str]) -> Optional[str]:
    if entity:
        k = os.getenv(f"PENNYLANE_API_KEY_{entity.upper()}")
        if k:
            return k
    return os.getenv("PENNYLANE_API_KEY") or None


def configured(entity: Optional[str]) -> bool:
    return bool(api_key_for(entity))


def list_incoming_transactions(entity: Optional[str], since: date, until: Optional[date] = None, max_pages: int = 10) -> list[dict]:
    """Transactions créditrices depuis `since`. Retourne [{id, date, amount, label}]."""
    import httpx

    key = api_key_for(entity)
    if not key:
        return []
    until = until or date.today()
    filters = [
        {"field": "date", "operator": "gteq", "value": since.isoformat()},
        {"field": "date", "operator": "lteq", "value": until.isoformat()},
    ]
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    out: list[dict] = []
    cursor: Optional[str] = None
    for _ in range(max_pages):
        params = {"filter": json.dumps(filters), "limit": 100}
        if cursor:
            params["cursor"] = cursor
        r = httpx.get(f"{API_BASE}/transactions", params=params, headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()
        items = data.get("items") or data.get("transactions") or []
        for t in items:
            try:
                amt = float(t.get("amount"))
            except (TypeError, ValueError):
                continue
            if amt <= 0:
                continue
            out.append(
                {
                    "id": str(t.get("id")),
                    "date": str(t.get("date"))[:10],
                    "amount": amt,
                    "label": t.get("label") or t.get("description") or "",
                }
            )
        if not data.get("has_more") or not data.get("next_cursor"):
            break
        cursor = data["next_cursor"]
    return out


def match_deposit(deposit: dict, transactions: list[dict], window_days: int = 6) -> Optional[dict]:
    """Trouve la transaction bancaire d'un dépôt : d'abord par numéro de sac, sinon montant + date proche."""
    bag = (deposit.get("bag_number") or "").strip()
    amount = float(deposit.get("amount") or 0)
    ddate = deposit.get("deposit_date")
    if isinstance(ddate, str):
        ddate = date.fromisoformat(ddate)
    if bag:
        for t in transactions:
            if bag in (t.get("label") or ""):
                return t
    if amount and ddate:
        best = None
        for t in transactions:
            if abs(t["amount"] - amount) > 0.005:
                continue
            try:
                tdate = date.fromisoformat(t["date"])
            except Exception:
                continue
            delta = abs((tdate - ddate).days)
            if delta <= window_days and (best is None or delta < best[0]):
                best = (delta, t)
        if best:
            return best[1]
    return None
