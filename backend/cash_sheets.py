"""Lecture (seule) des Google Sheets "Comptage coffre" de chaque magasin.

Authentification par compte de service Google :
  GOOGLE_SERVICE_ACCOUNT_JSON  = contenu JSON du compte de service, ou chemin vers le fichier.
Le sheet doit être partagé (lecteur) avec l'adresse e-mail du compte de service.

Structure attendue d'un onglet de période ("Du 15/06 au ...") :
  ligne 1 : en-têtes "Liste depots" / "Date" / "Ajout monnaie" / "Date" / "Factures" / "Date"
  colonnes A-D : comptage des rouleaux de pièces, "Solde précédent", "Total théorique", "Ecart"
  plus bas : bloc "Ecarts" / "Personne" / "Date" (écarts de caisse par caissier et par jour)
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime
from typing import Any, Optional

log = logging.getLogger("cash.sheets")

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

MONTHS_FR = {
    "janv": 1, "jan": 1, "fev": 2, "fév": 2, "feb": 2, "mars": 3, "mar": 3, "avr": 4, "apr": 4, "mai": 5,
    "juin": 6, "jun": 6, "juil": 7, "jul": 7, "aout": 8, "août": 8, "aug": 8, "sept": 9, "sep": 9,
    "oct": 10, "nov": 11, "dec": 12, "déc": 12,
}


def parse_amount(s: Any) -> Optional[float]:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).strip().replace("€", "").replace(" ", "").replace("\xa0", "").replace(" ", "")
    if not t or t in ("-", "–"):
        return None
    neg = t.startswith("-") or t.startswith("(")
    t = t.strip("()-+")
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".")
    else:
        t = t.replace(",", ".")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def parse_fr_date(s: Any, default_year: Optional[int] = None) -> Optional[date]:
    """Dates telles qu'écrites à la main dans le sheet : 17/6/26, 02/07, 13/08/2026, 19-juin, 01/09."""
    if s is None:
        return None
    if isinstance(s, (date, datetime)):
        return s if isinstance(s, date) else s.date()
    t = str(s).strip().lower()
    if not t:
        return None
    year = default_year or date.today().year
    m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{2,4}))?$", t)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        if y:
            yy = int(y)
            yy = yy + 2000 if yy < 100 else yy
        else:
            yy = year
        try:
            return date(yy, mo, d)
        except ValueError:
            return None
    m = re.match(r"^(\d{1,2})[\s\-]*([a-zéû]+)\.?(?:[\s\-]*(\d{2,4}))?$", t)
    if m:
        d, mon, y = int(m.group(1)), m.group(2), m.group(3)
        mo = None
        for k, v in MONTHS_FR.items():
            if mon.startswith(k):
                mo = v
                break
        if mo:
            yy = int(y) if y else year
            yy = yy + 2000 if yy < 100 else yy
            try:
                return date(yy, mo, d)
            except ValueError:
                return None
    return None


def _find_header(values: list[list[str]], label: str) -> Optional[tuple[int, int]]:
    lab = label.lower()
    for r, row in enumerate(values):
        for c, cell in enumerate(row):
            if str(cell).strip().lower() == lab:
                return r, c
    return None


def _column_pairs(values: list[list[str]], r: int, c: int, default_year: int, max_rows: int = 400) -> list[dict]:
    """Lit les couples (montant, date) sous un en-tête, jusqu'à la ligne 'Total' ou un long vide."""
    out = []
    blank = 0
    for i in range(r + 1, min(len(values), r + 1 + max_rows)):
        row = values[i]
        a = row[c] if c < len(row) else ""
        d = row[c + 1] if c + 1 < len(row) else ""
        if str(a).strip().lower() in ("total", "totaux"):
            break
        if c > 0 and c - 1 < len(row) and str(row[c - 1]).strip().lower() == "total":
            break
        amt = parse_amount(a)
        if amt is None:
            blank += 1
            if blank > 8:
                break
            continue
        blank = 0
        out.append({"amount": amt, "date": (parse_fr_date(d, default_year) or None), "date_raw": str(d).strip(), "row": i + 1})
    for e in out:
        e["date"] = e["date"].isoformat() if e["date"] else None
    return out


def _year_from_title(title: str) -> Optional[int]:
    m = re.search(r"/(\d{2,4})\b", title or "")
    if m:
        y = int(m.group(1))
        return y + 2000 if y < 100 else y
    m = re.match(r"^(20\d{2})", title or "")
    return int(m.group(1)) if m else None


def parse_coffre_tab(values: list[list[str]], tab_title: str = "", default_year: Optional[int] = None) -> dict:
    """Transforme les cellules d'un onglet de période en structure exploitable."""
    year = default_year or _year_from_title(tab_title) or date.today().year
    out: dict[str, Any] = {"tab_title": tab_title, "deposits": [], "coins_added": [], "expenses": [], "register_diffs": [], "count": {}}
    h = _find_header(values, "liste depots") or _find_header(values, "liste dépôts")
    if h:
        out["deposits"] = _column_pairs(values, h[0], h[1], year)
    h = _find_header(values, "ajout monnaie")
    if h:
        out["coins_added"] = _column_pairs(values, h[0], h[1], year)
    h = _find_header(values, "factures")
    if h:
        out["expenses"] = _column_pairs(values, h[0], h[1], year)
    h = _find_header(values, "ecarts") or _find_header(values, "écarts")
    if h:
        r, c = h
        for i in range(r + 1, len(values)):
            row = values[i]
            a = row[c] if c < len(row) else ""
            amt = parse_amount(a)
            if amt is None:
                if not any(str(x).strip() for x in row[c : c + 3]):
                    # ligne vide : on continue un peu au cas où
                    if i - r > 3 and not any(str(x).strip() for x in row):
                        continue
                continue
            person = (row[c + 1] if c + 1 < len(row) else "").strip()
            d = row[c + 2] if c + 2 < len(row) else ""
            extra_amt = parse_amount(row[c + 3]) if c + 3 < len(row) else None
            extra_lab = (row[c + 4] if c + 4 < len(row) else "").strip()
            dt = parse_fr_date(d, year)
            out["register_diffs"].append(
                {"amount": amt, "person": person, "date": dt.isoformat() if dt else None, "date_raw": str(d).strip(),
                 "extra_amount": extra_amt, "extra_label": extra_lab or None, "row": i + 1}
            )
    # Bloc de comptage (colonnes A-D)
    for key, label in (("solde_precedent", "solde précédent"), ("total_theorique", "total théorique"), ("ecart", "ecart"),
                       ("espece_encaisse", "espece encaissé"), ("date_comptage", "date comptage"), ("pieces_en_plus", "pieces en plus"),
                       ("total_compte_coffre", "total compté coffre")):
        pos = _find_header(values, label)
        if pos:
            r, c = pos
            row = values[r]
            val = row[c + 1] if c + 1 < len(row) else ""
            out["count"][key] = parse_amount(val) if key != "date_comptage" else (str(val).strip() or None)
            if key == "espece_encaisse" and c + 2 < len(row):
                out["count"]["espece_encaisse_depuis"] = str(row[c + 2]).strip() or None
    out["totals"] = {
        "deposits": round(sum(e["amount"] for e in out["deposits"]), 2),
        "coins_added": round(sum(e["amount"] for e in out["coins_added"]), 2),
        "expenses": round(sum(e["amount"] for e in out["expenses"]), 2),
        "register_diffs": round(sum(e["amount"] for e in out["register_diffs"]), 2),
    }
    return out


def pick_current_tab(titles: list[str]) -> Optional[str]:
    """Le premier onglet 'Du ... au ...' est la période en cours (les onglets sont rangés du plus récent au plus ancien)."""
    for t in titles:
        if re.match(r"^\s*du\s", t, re.I):
            return t
    for t in titles:
        if re.match(r"^\d{8}$", t.strip()):
            return t
    return None


class SheetsClient:
    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._token_exp: float = 0

    @staticmethod
    def configured() -> bool:
        return bool(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"))

    def _access_token(self) -> str:
        import time

        if self._token and time.time() < self._token_exp - 60:
            return self._token
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request

        raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        if raw.strip().startswith("{"):
            info = json.loads(raw)
            creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        else:
            creds = service_account.Credentials.from_service_account_file(raw, scopes=SCOPES)
        creds.refresh(Request())
        self._token = creds.token
        self._token_exp = creds.expiry.timestamp() if creds.expiry else time.time() + 3000
        return self._token

    def _get(self, url: str, params: Optional[dict] = None) -> dict:
        import httpx

        headers = {"Authorization": f"Bearer {self._access_token()}"}
        r = httpx.get(url, params=params, headers=headers, timeout=30)
        r.raise_for_status()
        return r.json()

    def tab_titles(self, spreadsheet_id: str) -> list[str]:
        data = self._get(f"{SHEETS_API}/{spreadsheet_id}", {"fields": "sheets.properties.title"})
        return [s["properties"]["title"] for s in data.get("sheets", [])]

    def values(self, spreadsheet_id: str, a1_range: str) -> list[list[str]]:
        from urllib.parse import quote

        data = self._get(f"{SHEETS_API}/{spreadsheet_id}/values/{quote(a1_range, safe='')}")
        return data.get("values", [])

    def read_current_period(self, spreadsheet_id: str) -> dict:
        titles = self.tab_titles(spreadsheet_id)
        tab = pick_current_tab(titles)
        if not tab:
            raise RuntimeError("Aucun onglet de période ('Du ... au ...') trouvé")
        vals = self.values(spreadsheet_id, f"'{tab}'!A1:Z600")
        return parse_coffre_tab(vals, tab)
