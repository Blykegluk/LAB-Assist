"""Configuration du module dépôts d'espèces (lecture de config.json + variables d'env)."""
from __future__ import annotations

import json
import os
import pathlib
from functools import lru_cache
from typing import Any, Optional

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.json"

DEFAULT_RULES = {
    "ecart_threshold_eur": 10.0,
    "missing_days_after": 5,
    "undeposited_after_days": 7,
    "bank_credit_after_days": 4,
    "sheet_sync_hours": 6,
    "digest_hour": 8,
    "closed_weekdays": [],
    "holidays": [],
}


def _load_raw() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {}


@lru_cache(maxsize=1)
def cash_config() -> dict:
    raw = _load_raw().get("cash", {}) or {}
    rules = dict(DEFAULT_RULES)
    rules.update(raw.get("rules", {}) or {})
    stores = []
    for s in raw.get("stores", []) or []:
        stores.append(
            {
                "id": s.get("id"),
                "label": s.get("label") or s.get("id"),
                "entity": s.get("entity"),
                "color": s.get("color") or "#2563eb",
                "pos_system": s.get("pos_system") or "storemate",
                "pos_store_numbers": [str(x) for x in (s.get("pos_store_numbers") or [])],
                "sheet_id": s.get("sheet_id"),
                "pennylane_company_id": s.get("pennylane_company_id"),
            }
        )
    return {
        "stores": stores,
        "rules": rules,
        "docs_dir": str(BASE_DIR / (raw.get("docs_dir") or "data/cash_docs")),
    }


def stores() -> list[dict]:
    return cash_config()["stores"]


def store_by_id(store_id: str) -> Optional[dict]:
    for s in stores():
        if s["id"] == store_id:
            return s
    return None


def rules() -> dict:
    return cash_config()["rules"]


def docs_dir() -> pathlib.Path:
    p = pathlib.Path(cash_config()["docs_dir"])
    p.mkdir(parents=True, exist_ok=True)
    return p


def env(name: str, default: Any = None) -> Any:
    v = os.getenv(name)
    return v if v not in (None, "") else default
