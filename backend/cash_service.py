"""Logique métier du module dépôts d'espèces : analyse des documents, rapprochement, alertes, synthèse."""
from __future__ import annotations

import json
import logging
import math
import os
import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional

from sqlalchemy import select, and_, or_
from sqlalchemy.orm import Session

from . import cash_bank, cash_notify, cash_routine
from .cash_config import rules as config_rules, stores as config_stores, store_by_id
from .cash_models import (
    CashAlert,
    CashBankTransaction,
    CashDeposit,
    CashDocument,
    CashExpense,
    CashReport,
    CashSetting,
    CashSheetSnapshot,
    CashSubmission,
)
from .cash_sheets import SheetsClient, parse_amount

log = logging.getLogger("cash.service")

REPORT_KINDS = ("releve_storemate", "releve_orika")
LOW_CONFIDENCE = 0.6


# ----------------------------------------------------------------------------- utilitaires

def norm(s: Any) -> str:
    s = "" if s is None else str(s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def parse_date_any(v: Any) -> Optional[date]:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    t = str(v).strip()
    t = t.split(" ")[0] if " " in t and "T" not in t else t.split("T")[0]
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


def to_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return parse_amount(v)


def r2(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(float(x) + 0.0, 2)


def iso(d: Optional[date]) -> Optional[str]:
    return d.isoformat() if d else None


def fr(d: Optional[date]) -> str:
    return d.strftime("%d/%m/%Y") if d else ""


WEEKDAYS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def frl(d: Optional[date]) -> str:
    """Date longue en français : jeudi 15/10/2026."""
    return f"{WEEKDAYS_FR[d.weekday()]} {fr(d)}" if d else ""


def eur(x: Optional[float]) -> str:
    if x is None:
        return "—"
    s = f"{abs(float(x)):,.2f}".replace(",", " ").replace(".", ",")
    return ("-" if float(x) < 0 else "") + s + " €"


def daterange(a: date, b: date) -> Iterable[date]:
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


# ----------------------------------------------------------------------------- réglages

def get_setting(db: Session, key: str, default: Any = None) -> Any:
    row = db.get(CashSetting, key)
    if not row:
        return default
    try:
        return json.loads(row.value)
    except Exception:
        return row.value


def set_setting(db: Session, key: str, value: Any) -> None:
    row = db.get(CashSetting, key)
    if row:
        row.value = json.dumps(value)
    else:
        db.add(CashSetting(key=key, value=json.dumps(value)))
    db.commit()


def effective_rules(db: Session) -> dict:
    r = dict(config_rules())
    override = get_setting(db, "rules", {}) or {}
    if isinstance(override, dict):
        r.update({k: v for k, v in override.items() if v is not None})
    return r


def is_closed_day(d: date, r: dict) -> bool:
    if d.weekday() in (r.get("closed_weekdays") or []):
        return True
    return d.isoformat() in (r.get("holidays") or [])


# ----------------------------------------------------------------------------- analyse d'un lot de documents

def _store_check(store: dict, kind: str, ex: dict) -> tuple[str, Optional[str]]:
    """Retourne (ok|mismatch|unknown, libellé du magasin détecté si différent)."""
    others = [s for s in config_stores() if s["id"] != store["id"]]
    if kind in REPORT_KINDS:
        num = str(ex.get("store_number") or "").strip()
        name = norm(ex.get("store_name"))
        if num and num in store["pos_store_numbers"]:
            return "ok", None
        for o in others:
            if num and num in o["pos_store_numbers"]:
                return "mismatch", o["label"]
        if name and norm(store["label"]) in name:
            return "ok", None
        for o in others:
            if name and norm(o["label"]) in name:
                return "mismatch", o["label"]
        return ("unknown", None) if not (num or name) else ("unknown", ex.get("store_name") or num)
    if kind == "bordereau":
        holder = norm(ex.get("account_holder"))
        if holder and holder == norm(store.get("entity")):
            return "ok", None
        for o in others:
            if holder and holder == norm(o.get("entity")):
                return "mismatch", f"{o['label']} ({o.get('entity')})"
        return "unknown", (ex.get("account_holder") or None)
    return "unknown", None


def _flatten(extracted: dict) -> list[dict]:
    """Une photo peut contenir plusieurs documents : on les sépare."""
    out = [dict(extracted)]
    for extra in extracted.get("extra_documents") or []:
        if isinstance(extra, dict) and extra.get("type"):
            e = dict(extra)
            e["_from_extra"] = True
            out.append(e)
    return out


def analyze_documents(db: Session, store: dict, docs: list[tuple[int, dict]], r: Optional[dict] = None) -> dict:
    """docs = [(document_id, extraction)] -> analyse structurée, sans rien écrire en base."""
    r = r or effective_rules(db)
    threshold = float(r.get("ecart_threshold_eur") or 10)
    issues: list[dict] = []
    documents: list[dict] = []
    reports: list[dict] = []
    slips: list[dict] = []
    receipts: list[dict] = []
    expenses: list[dict] = []

    for doc_id, extracted in docs:
        for ex in _flatten(extracted or {}):
            kind = str(ex.get("type") or "autre")
            conf = to_float(ex.get("confidence"))
            check, other = _store_check(store, kind, ex)
            d = {
                "document_id": doc_id,
                "kind": kind,
                "confidence": conf,
                "store_check": check,
                "detected_store": other,
                "warnings": ex.get("warnings") or [],
                "from_extra": bool(ex.get("_from_extra")),
            }
            documents.append(d)
            if check == "mismatch":
                issues.append(
                    {
                        "level": "block",
                        "code": "wrong_store",
                        "document_id": doc_id,
                        "message": f"Ce document semble appartenir à {other}, pas à {store['label']}. Vérifie le magasin choisi.",
                    }
                )
            if conf is not None and conf < LOW_CONFIDENCE and kind != "autre":
                issues.append(
                    {"level": "warning", "code": "low_confidence", "document_id": doc_id,
                     "message": f"Lecture incertaine d'un document ({kind}). Vérifie les montants avant de confirmer."}
                )
            if kind in REPORT_KINDS:
                ps, pe = parse_date_any(ex.get("period_start")), parse_date_any(ex.get("period_end"))
                cash = to_float(ex.get("cash_amount"))
                if cash is None:
                    for pm in ex.get("payment_methods") or []:
                        if "espece" in norm(pm.get("label")):
                            cash = to_float(pm.get("amount"))
                            break
                if cash is None and isinstance(ex.get("operators_total"), dict):
                    cash = to_float(ex["operators_total"].get("cash"))
                if ps and not pe:
                    pe = ps
                if pe and not ps:
                    ps = pe
                if not ps or cash is None:
                    issues.append({"level": "warning", "code": "report_incomplete", "document_id": doc_id,
                                   "message": "Relevé de caisse lu partiellement (période ou montant espèces manquant). Complète à la main."})
                reports.append(
                    {
                        "document_id": doc_id,
                        "pos_system": "orika" if kind == "releve_orika" else "storemate",
                        "period_start": iso(ps),
                        "period_end": iso(pe),
                        "cash_amount": r2(cash),
                        "total_amount": r2(to_float(ex.get("total_amount"))),
                        "operators": ex.get("operators") or [],
                        "operators_total": ex.get("operators_total"),
                        "handwritten_amount": r2(to_float(ex.get("handwritten_amount"))),
                        "handwritten_notes": ex.get("handwritten_notes"),
                        "store_name": ex.get("store_name"),
                        "duplicate_of": None,
                    }
                )
            elif kind == "bordereau":
                slips.append(
                    {
                        "document_id": doc_id,
                        "bag_number": (str(ex.get("bag_number") or "").strip() or None),
                        "date": iso(parse_date_any(ex.get("deposit_date"))),
                        "amount": r2(to_float(ex.get("deposit_amount"))),
                        "breakdown": ex.get("breakdown") or [],
                        "account_holder": ex.get("account_holder"),
                        "depositor": ex.get("depositor"),
                    }
                )
            elif kind == "recu_gab":
                receipts.append(
                    {
                        "document_id": doc_id,
                        "bag_number": (str(ex.get("bag_number") or "").strip() or None),
                        "date": iso(parse_date_any(ex.get("deposit_date"))),
                        "amount": r2(to_float(ex.get("deposit_amount"))),
                        "operation_number": ex.get("operation_number"),
                    }
                )
            elif kind == "facture":
                inv = ex.get("invoice") if isinstance(ex.get("invoice"), dict) else {}
                amt = to_float(inv.get("amount")) if inv else to_float(ex.get("deposit_amount"))
                expenses.append(
                    {
                        "document_id": doc_id,
                        "date": iso(parse_date_any((inv or {}).get("date") or ex.get("deposit_date"))),
                        "amount": r2(amt),
                        "label": ((inv or {}).get("supplier") or (inv or {}).get("label") or "Facture espèces"),
                    }
                )
            else:
                issues.append({"level": "info", "code": "unknown_document", "document_id": doc_id,
                               "message": "Un document n'a pas été reconnu (ni relevé, ni bordereau, ni ticket). Il est conservé mais ignoré dans le calcul."})

    # Dédoublonnage des relevés identiques (même période lue deux fois)
    uniq: list[dict] = []
    for rep in reports:
        if any(u["period_start"] == rep["period_start"] and u["period_end"] == rep["period_end"] and u["cash_amount"] == rep["cash_amount"] for u in uniq):
            continue
        uniq.append(rep)
    reports = uniq

    # Dépôts : association bordereau <-> ticket GAB par numéro de sac, sinon par montant, sinon par ordre
    deposits: list[dict] = []
    used_receipts: set[int] = set()
    for s in slips:
        match = None
        for i, rc in enumerate(receipts):
            if i in used_receipts:
                continue
            if s["bag_number"] and rc["bag_number"] and s["bag_number"] == rc["bag_number"]:
                match = i
                break
        if match is None:
            for i, rc in enumerate(receipts):
                if i in used_receipts:
                    continue
                if s["amount"] is not None and rc["amount"] is not None and abs(s["amount"] - rc["amount"]) < 0.005:
                    match = i
                    break
        if match is None and len(slips) == 1 and len(receipts) == 1 and 0 not in used_receipts:
            match = 0
        rc = receipts[match] if match is not None else None
        if match is not None:
            used_receipts.add(match)
        amount = rc["amount"] if rc and rc["amount"] is not None else s["amount"]
        ddate = rc["date"] if rc and rc["date"] else s["date"]
        mismatch = bool(rc and rc["amount"] is not None and s["amount"] is not None and abs(rc["amount"] - s["amount"]) >= 0.005)
        deposits.append(
            {
                "document_ids": [s["document_id"]] + ([rc["document_id"]] if rc else []),
                "bag_number": s["bag_number"] or (rc["bag_number"] if rc else None),
                "deposit_date": ddate,
                "amount": amount,
                "slip_amount": s["amount"],
                "slip_date": s["date"],
                "receipt_amount": rc["amount"] if rc else None,
                "receipt_date": rc["date"] if rc else None,
                "operation_number": rc.get("operation_number") if rc else None,
                "account_holder": s.get("account_holder"),
                "breakdown": s.get("breakdown") or [],
                "mismatch": mismatch,
                "has_receipt": rc is not None,
            }
        )
        if mismatch:
            issues.append({"level": "warning", "code": "slip_receipt_mismatch",
                           "message": f"Le bordereau indique {eur(s['amount'])} mais l'automate a compté {eur(rc['amount'])} (sac {s['bag_number'] or '?'})."})
        if not rc:
            issues.append({"level": "info", "code": "no_receipt",
                           "message": "Bordereau sans ticket de l'automate : le montant du bordereau est utilisé."})
    for i, rc in enumerate(receipts):
        if i in used_receipts:
            continue
        deposits.append(
            {
                "document_ids": [rc["document_id"]],
                "bag_number": rc["bag_number"],
                "deposit_date": rc["date"],
                "amount": rc["amount"],
                "slip_amount": None,
                "slip_date": None,
                "receipt_amount": rc["amount"],
                "receipt_date": rc["date"],
                "operation_number": rc.get("operation_number"),
                "account_holder": None,
                "breakdown": [],
                "mismatch": False,
                "has_receipt": True,
            }
        )
        issues.append({"level": "info", "code": "no_slip", "message": "Ticket de l'automate sans bordereau : le montant du ticket est utilisé."})

    # Doublons et trous par rapport à l'historique confirmé
    for rep in reports:
        ps, pe = parse_date_any(rep["period_start"]), parse_date_any(rep["period_end"])
        if not ps or not pe:
            continue
        dup = db.execute(
            select(CashReport).join(CashSubmission, CashSubmission.id == CashReport.submission_id).where(
                CashReport.store_id == store["id"],
                CashSubmission.status == "confirmed",
                CashReport.period_start <= pe,
                CashReport.period_end >= ps,
            )
        ).scalars().first()
        if dup:
            rep["duplicate_of"] = dup.submission_id
            issues.append({"level": "warning", "code": "duplicate_period",
                           "message": f"Un relevé couvrant déjà {fr(dup.period_start)} → {fr(dup.period_end)} a été envoyé (envoi n°{dup.submission_id}). Doublon probable."})
    for dep in deposits:
        if dep["bag_number"]:
            dup = db.execute(select(CashDeposit).where(CashDeposit.store_id == store["id"], CashDeposit.bag_number == dep["bag_number"])).scalars().first()
            if dup:
                issues.append({"level": "warning", "code": "duplicate_deposit",
                               "message": f"Le sac n°{dep['bag_number']} a déjà été enregistré le {fr(dup.deposit_date)} ({eur(dup.amount)})."})

    coverage = _coverage_info(db, store["id"], reports, r)
    if coverage.get("gap_days"):
        issues.append({"level": "info", "code": "gap_before",
                       "message": f"{len(coverage['gap_days'])} journée(s) sans relevé entre le dernier envoi et celui-ci : {', '.join(fr(parse_date_any(x)) for x in coverage['gap_days'][:6])}{'…' if len(coverage['gap_days']) > 6 else ''}."})

    if not reports:
        issues.append({"level": "warning", "code": "no_report", "message": "Aucun relevé de caisse reconnu dans cet envoi."})
    if not deposits:
        issues.append({"level": "warning", "code": "no_deposit", "message": "Aucun bordereau ni ticket de dépôt reconnu dans cet envoi."})

    analysis = {
        "store_id": store["id"],
        "store_label": store["label"],
        "documents": documents,
        "reports": reports,
        "deposits": deposits,
        "expenses": expenses,
        "issues": issues,
        "coverage": coverage,
        "threshold": threshold,
    }
    compute_totals(analysis)
    analysis["blocked"] = any(i["level"] == "block" for i in issues)
    return analysis


def compute_totals(analysis: dict) -> dict:
    threshold = float(analysis.get("threshold") or 10)
    cash = sum((to_float(rp.get("cash_amount")) or 0.0) for rp in analysis.get("reports") or [])
    exp = sum((to_float(e.get("amount")) or 0.0) for e in analysis.get("expenses") or [])
    dep = sum((to_float(d.get("amount")) or 0.0) for d in analysis.get("deposits") or [])
    expected = cash - exp
    has_dep = bool(analysis.get("deposits"))
    has_rep = bool(analysis.get("reports"))
    ecart = (expected - dep) if (has_dep and has_rep) else None
    analysis["cash_collected"] = r2(cash)
    analysis["expenses_total"] = r2(exp)
    analysis["expected_amount"] = r2(expected) if has_rep else None
    analysis["deposit_amount"] = r2(dep) if has_dep else None
    analysis["ecart"] = r2(ecart)
    analysis["ecart_alert"] = bool(ecart is not None and abs(ecart) >= threshold)
    analysis["coins_estimate"] = r2(expected - math.floor(expected / 5.0) * 5.0) if has_rep and expected > 0 else None
    return analysis


def _coverage_info(db: Session, store_id: str, reports: list[dict], r: dict) -> dict:
    days: set[date] = set()
    for rep in reports:
        ps, pe = parse_date_any(rep.get("period_start")), parse_date_any(rep.get("period_end"))
        if ps and pe and pe >= ps and (pe - ps).days < 62:
            days.update(daterange(ps, pe))
    last = db.execute(
        select(CashReport).join(CashSubmission, CashSubmission.id == CashReport.submission_id)
        .where(CashReport.store_id == store_id, CashSubmission.status == "confirmed")
        .order_by(CashReport.period_end.desc())
    ).scalars().first()
    gap: list[str] = []
    if last and days:
        first_new = min(days)
        if first_new > last.period_end + timedelta(days=1):
            for d in daterange(last.period_end + timedelta(days=1), first_new - timedelta(days=1)):
                if not is_closed_day(d, r) and d not in days:
                    gap.append(d.isoformat())
    return {
        "days": sorted(d.isoformat() for d in days),
        "last_covered_before": iso(last.period_end) if last else None,
        "gap_days": gap,
    }


# ----------------------------------------------------------------------------- alertes

def _store_label(store_id: str) -> str:
    s = store_by_id(store_id)
    return s["label"] if s else store_id


def upsert_alert(db: Session, store_id: str, kind: str, severity: str, title: str, message: str, dedupe_key: str,
                 submission_id: Optional[int] = None, ref_date: Optional[date] = None) -> tuple[CashAlert, bool]:
    row = db.execute(select(CashAlert).where(CashAlert.dedupe_key == dedupe_key)).scalars().first()
    if row:
        changed = False
        if row.status == "resolved":
            row.status = "open"
            row.notified_at = None
            changed = True
        if row.message != message or row.title != title or row.severity != severity:
            row.message, row.title, row.severity = message, title, severity
            changed = True
        if changed:
            row.updated_at = datetime.utcnow()
        return row, False
    row = CashAlert(store_id=store_id, kind=kind, severity=severity, title=title, message=message, dedupe_key=dedupe_key,
                    submission_id=submission_id, ref_date=ref_date)
    db.add(row)
    db.flush()
    return row, True


def resolve_alert(db: Session, dedupe_key: str) -> None:
    row = db.execute(select(CashAlert).where(CashAlert.dedupe_key == dedupe_key)).scalars().first()
    if row and row.status != "resolved":
        row.status = "resolved"
        row.updated_at = datetime.utcnow()


def alert_to_dict(a: CashAlert) -> dict:
    return {
        "id": a.id,
        "store_id": a.store_id,
        "store_label": _store_label(a.store_id),
        "kind": a.kind,
        "severity": a.severity,
        "title": a.title,
        "message": a.message,
        "submission_id": a.submission_id,
        "ref_date": iso(a.ref_date),
        "status": a.status,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "notified_at": a.notified_at.isoformat() if a.notified_at else None,
    }


def group_alert_dicts(alerts: list[dict]) -> list[dict]:
    """Regroupe les journées sans relevé d'un même magasin en une seule ligne (e-mails)."""
    out: list[dict] = []
    missing: dict[str, list[dict]] = {}
    for a in alerts:
        if a.get("kind") == "missing_day" and a.get("ref_date"):
            missing.setdefault(a["store_id"], []).append(a)
        else:
            out.append(a)
    for sid, items in missing.items():
        items.sort(key=lambda x: x["ref_date"])
        dates = [fr(parse_date_any(x["ref_date"])) for x in items]
        base = dict(items[0])
        base["title"] = f"{len(items)} journée(s) sans relevé de caisse" if len(items) > 1 else items[0]["title"]
        base["message"] = ("Journées non couvertes par un relevé : " + ", ".join(dates) + ".") if len(items) > 1 else items[0]["message"]
        out.append(base)
    return out


def notify_critical(db: Session, alerts: list[CashAlert]) -> int:
    crit = [a for a in alerts if a.severity == "critical" and a.notified_at is None and a.status == "open"]
    if not crit:
        return 0
    html_body, text_body = cash_notify.render_alerts_email([alert_to_dict(a) for a in crit], "Alerte dépôts d'espèces")
    subject = f"[LAB Assist] {len(crit)} alerte(s) critique(s) dépôts d'espèces"
    sent = cash_notify.send_email(subject, html_body, text_body)
    if sent:
        for a in crit:
            a.notified_at = datetime.utcnow()
        db.commit()
    return len(crit) if sent else 0


def send_digest(db: Session, force: bool = False) -> dict:
    """Récapitulatif quotidien des alertes ouvertes non encore envoyées (envoyé une fois par jour à digest_hour)."""
    r = effective_rules(db)
    today = date.today().isoformat()
    last = get_setting(db, "last_digest_date")
    now_hour = datetime.now().hour
    if not force and (last == today or now_hour < int(r.get("digest_hour") or 8)):
        return {"sent": False, "reason": "déjà envoyé aujourd'hui" if last == today else "pas encore l'heure"}
    pending = db.execute(select(CashAlert).where(CashAlert.status == "open", CashAlert.notified_at.is_(None)).order_by(CashAlert.severity, CashAlert.created_at)).scalars().all()
    set_setting(db, "last_digest_date", today)
    if not pending:
        return {"sent": False, "reason": "aucune nouvelle alerte"}
    html_body, text_body = cash_notify.render_alerts_email(group_alert_dicts([alert_to_dict(a) for a in pending]), f"Récap dépôts d'espèces du {date.today().strftime('%d/%m/%Y')}")
    sent = cash_notify.send_email(f"[LAB Assist] Récap dépôts d'espèces : {len(pending)} point(s) à voir", html_body, text_body)
    if sent:
        for a in pending:
            a.notified_at = datetime.utcnow()
        db.commit()
    return {"sent": sent, "count": len(pending)}


# ----------------------------------------------------------------------------- confirmation d'un envoi

def confirm_submission(db: Session, sub: CashSubmission, payload: dict, role: str, who: Optional[str] = None) -> dict:
    store = store_by_id(sub.store_id)
    if not store:
        raise ValueError("Magasin inconnu")
    if sub.status == "confirmed":
        raise ValueError("Cet envoi est déjà confirmé")
    r = effective_rules(db)
    base = json.loads(sub.analysis_json or "{}") if sub.analysis_json else {}
    analysis = dict(base)
    for key in ("reports", "deposits", "expenses"):
        if key in payload and isinstance(payload[key], list):
            analysis[key] = payload[key]
    analysis["threshold"] = float(r.get("ecart_threshold_eur") or 10)
    # Nettoyage / typage des valeurs éventuellement corrigées à la main
    for rep in analysis.get("reports") or []:
        rep["period_start"] = iso(parse_date_any(rep.get("period_start")))
        rep["period_end"] = iso(parse_date_any(rep.get("period_end"))) or rep["period_start"]
        rep["cash_amount"] = r2(to_float(rep.get("cash_amount")))
        if not rep["period_start"] or rep["cash_amount"] is None:
            raise ValueError("Chaque relevé doit avoir une date de début et un montant espèces")
    for dep in analysis.get("deposits") or []:
        dep["deposit_date"] = iso(parse_date_any(dep.get("deposit_date")))
        dep["amount"] = r2(to_float(dep.get("amount")))
        if not dep["deposit_date"] or dep["amount"] is None:
            raise ValueError("Chaque dépôt doit avoir une date et un montant")
    for e in analysis.get("expenses") or []:
        e["amount"] = r2(to_float(e.get("amount")))
        e["date"] = iso(parse_date_any(e.get("date")))
        if e["amount"] is None:
            raise ValueError("Chaque facture doit avoir un montant")
    blocking = [i for i in (base.get("issues") or []) if i.get("level") == "block"]
    if blocking and not (role == "owner" and payload.get("force")):
        raise ValueError(blocking[0]["message"])
    compute_totals(analysis)

    created_alerts: list[CashAlert] = []
    for rep in analysis.get("reports") or []:
        db.add(CashReport(
            submission_id=sub.id, document_id=rep.get("document_id"), store_id=sub.store_id,
            pos_system=rep.get("pos_system") or store["pos_system"],
            period_start=parse_date_any(rep["period_start"]), period_end=parse_date_any(rep["period_end"]),
            cash_amount=rep["cash_amount"], total_amount=r2(to_float(rep.get("total_amount"))),
            operators_json=json.dumps(rep.get("operators") or [], ensure_ascii=False),
        ))
    for dep in analysis.get("deposits") or []:
        db.add(CashDeposit(
            submission_id=sub.id, store_id=sub.store_id, deposit_date=parse_date_any(dep["deposit_date"]), amount=dep["amount"],
            bag_number=dep.get("bag_number"), slip_amount=r2(to_float(dep.get("slip_amount"))), slip_date=parse_date_any(dep.get("slip_date")),
            receipt_amount=r2(to_float(dep.get("receipt_amount"))), receipt_date=parse_date_any(dep.get("receipt_date")),
            operation_number=dep.get("operation_number"), account_holder=dep.get("account_holder"),
            breakdown_json=json.dumps(dep.get("breakdown") or [], ensure_ascii=False),
            bank_status="pending" if cash_bank.configured(store.get("entity")) else "unchecked",
        ))
    for e in analysis.get("expenses") or []:
        db.add(CashExpense(submission_id=sub.id, store_id=sub.store_id, expense_date=parse_date_any(e.get("date")), amount=e["amount"], label=e.get("label")))

    sub.status = "confirmed"
    sub.confirmed_at = datetime.utcnow()
    sub.submitted_by = who or role
    sub.note = (payload.get("note") or sub.note or None)
    sub.expected_amount = analysis.get("expected_amount")
    sub.deposit_amount = analysis.get("deposit_amount")
    sub.ecart = analysis.get("ecart")
    sub.analysis_json = json.dumps(analysis, ensure_ascii=False)
    db.flush()

    label = store["label"]
    period = _period_label(analysis)
    if analysis.get("ecart_alert"):
        e = analysis["ecart"]
        sens = "moins déposé qu'encaissé" if e > 0 else "plus déposé qu'encaissé"
        a, _ = upsert_alert(db, sub.store_id, "ecart_depot", "critical", f"Écart de dépôt de {eur(abs(e))}",
                            f"{label}, {period} : attendu {eur(analysis['expected_amount'])}, déposé {eur(analysis['deposit_amount'])} ({sens}).",
                            f"ecart:{sub.id}", submission_id=sub.id)
        created_alerts.append(a)
    for dep in analysis.get("deposits") or []:
        if dep.get("mismatch"):
            a, _ = upsert_alert(db, sub.store_id, "slip_receipt_mismatch", "warning", "Bordereau ≠ montant compté par l'automate",
                                f"{label} : bordereau {eur(dep.get('slip_amount'))}, automate {eur(dep.get('receipt_amount'))} (sac {dep.get('bag_number') or '?'}).",
                                f"mismatch:{sub.id}:{dep.get('bag_number') or dep.get('deposit_date')}", submission_id=sub.id)
            created_alerts.append(a)
    for i in base.get("issues") or []:
        if i.get("code") == "duplicate_period":
            a, _ = upsert_alert(db, sub.store_id, "duplicate_period", "warning", "Relevé en doublon probable", f"{label} : {i['message']}", f"dup_period:{sub.id}", submission_id=sub.id)
            created_alerts.append(a)
        elif i.get("code") == "duplicate_deposit":
            a, _ = upsert_alert(db, sub.store_id, "duplicate_deposit", "warning", "Dépôt déjà enregistré", f"{label} : {i['message']}", f"dup_deposit:{sub.id}", submission_id=sub.id)
            created_alerts.append(a)
        elif i.get("code") == "low_confidence":
            a, _ = upsert_alert(db, sub.store_id, "low_confidence", "info", "Lecture incertaine d'un document", f"{label}, {period} : {i['message']}", f"lowconf:{sub.id}:{i.get('document_id')}", submission_id=sub.id)
            created_alerts.append(a)
        elif i.get("code") == "wrong_store" and blocking:
            a, _ = upsert_alert(db, sub.store_id, "wrong_store", "critical", "Document d'un autre magasin",
                                f"{label} : {i['message']} Vérifie l'envoi et annule-le s'il est mal classé.", f"wrongstore:{sub.id}", submission_id=sub.id)
            created_alerts.append(a)
    # Les jours désormais couverts ne sont plus "non traités"
    for d in analysis.get("coverage", {}).get("days") or []:
        resolve_alert(db, f"missing_day:{sub.store_id}:{d}")
    # Un dépôt résout les "encaissé non déposé" antérieurs du magasin
    if analysis.get("deposits"):
        for al in db.execute(select(CashAlert).where(CashAlert.store_id == sub.store_id, CashAlert.kind == "undeposited", CashAlert.status == "open")).scalars().all():
            al.status = "resolved"
            al.updated_at = datetime.utcnow()
    db.commit()
    notify_critical(db, created_alerts)
    return {"submission": submission_to_dict(db, sub), "alerts": [alert_to_dict(a) for a in created_alerts]}


# ----------------------------------------------------------------------------- analyse différée (tâche Claude planifiée)

def analysis_mode() -> str:
    """deferred (défaut) : photos stockées puis analysées par une tâche Claude planifiée.
    immediate : photos lues à l'envoi via l'API (clé ANTHROPIC_API_KEY ou OPENAI_API_KEY)."""
    mode = (os.getenv("CASH_ANALYSIS_MODE") or "deferred").strip().lower()
    return mode if mode in ("deferred", "immediate") else "deferred"


def analysis_times() -> list[tuple[int, int]]:
    out = []
    for part in (os.getenv("CASH_ANALYSIS_TIMES") or "12:05,18:05").split(","):
        m = re.match(r"^\s*(\d{1,2})[:hH](\d{2})\s*$", part)
        if m:
            out.append((int(m.group(1)), int(m.group(2))))
    return sorted(out) or [(12, 5), (18, 5)]


def next_analysis_label(now: Optional[datetime] = None) -> str:
    """Texte affiché à la responsable : quand ses documents seront analysés (heure de Paris)."""
    try:
        from zoneinfo import ZoneInfo

        now = now or datetime.now(ZoneInfo("Europe/Paris"))
    except Exception:
        now = now or datetime.now()
    for h, m in analysis_times():
        if (now.hour, now.minute) < (h, m):
            return f"aujourd'hui vers {h}h{m:02d}"
    h, m = analysis_times()[0]
    return f"demain vers {h}h{m:02d}"


def _paris_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Europe/Paris")).replace(tzinfo=None)
    except Exception:
        return datetime.now()


def analysis_should_run(db: Session, now: Optional[datetime] = None) -> dict:
    """La tâche planifiée passe toutes les heures ; elle ne travaille que s'il y a des photos en attente
    et que c'est l'heure d'un créneau prévu (12h05, 18h05) ou que le gérant a demandé une analyse."""
    now = now or _paris_now()
    queued = db.execute(select(CashSubmission).where(CashSubmission.status == "queued")).scalars().all()
    requested = get_setting(db, "analysis_requested_at")
    if not queued:
        return {"run": False, "reason": "aucune photo en attente", "queued": 0, "requested_at": requested}
    if requested:
        return {"run": True, "reason": "demande manuelle", "queued": len(queued), "requested_at": requested}
    if now.hour in {h for h, _ in analysis_times()}:
        return {"run": True, "reason": "créneau prévu", "queued": len(queued), "requested_at": None}
    return {"run": False, "reason": "pas de créneau ni de demande", "queued": len(queued), "requested_at": None}


def request_analysis(db: Session, who: str) -> dict:
    queued = db.execute(select(CashSubmission).where(CashSubmission.status == "queued")).scalars().all()
    """Bouton « Analyser maintenant » : lance tout de suite la tâche Claude (relevé Pennylane, photos,
    contrôles), comme aux créneaux de 12h05 et 18h05."""
    set_setting(db, "analysis_requested_at", datetime.utcnow().isoformat())
    set_setting(db, "analysis_requested_by", who)
    last = get_setting(db, "routine_fired_at")
    try:
        recent = bool(last) and datetime.utcnow() - datetime.fromisoformat(last) < timedelta(minutes=3)
    except ValueError:
        recent = False
    if recent:
        return {"requested": True, "fired": False, "queued": len(queued), "session_url": get_setting(db, "routine_session_url"),
                "message": "Une analyse vient d'être lancée, elle est en cours."}
    if not cash_routine.configured():
        slots = " ou ".join(f"{h}h{m:02d}" for h, m in analysis_times())
        return {"requested": True, "fired": False, "queued": len(queued),
                "message": f"Le lancement immédiat n'est pas encore activé : l'analyse se fera au prochain créneau ({slots})."}
    res = cash_routine.fire("Analyse demandée depuis le tableau de bord.")
    if not res["ok"]:
        log.error("déclenchement de la tâche refusé : %s", res.get("error"))
        return {"requested": True, "fired": False, "queued": len(queued),
                "message": f"Lancement impossible ({res.get('error')}). L'analyse se fera au prochain créneau."}
    set_setting(db, "routine_fired_at", datetime.utcnow().isoformat())
    set_setting(db, "routine_session_url", res.get("session_url"))
    what = f"{len(queued)} envoi(s) en attente, relevé bancaire et contrôles" if queued else "relevé bancaire et contrôles"
    return {"requested": True, "fired": True, "queued": len(queued), "session_url": res.get("session_url"),
            "message": f"Analyse lancée ({what}). Les résultats arrivent dans quelques minutes."}


def queue_items(db: Session) -> list[dict]:
    subs = db.execute(select(CashSubmission).where(CashSubmission.status == "queued").order_by(CashSubmission.created_at)).scalars().all()
    items = []
    for sub in subs:
        store = store_by_id(sub.store_id) or {}
        docs = db.execute(select(CashDocument).where(CashDocument.submission_id == sub.id).order_by(CashDocument.id)).scalars().all()
        items.append({
            "submission_id": sub.id,
            "store_id": sub.store_id,
            "store_label": store.get("label"),
            "entity": store.get("entity"),
            "pos_system": store.get("pos_system"),
            "pos_store_numbers": store.get("pos_store_numbers"),
            "created_at": sub.created_at.isoformat() if sub.created_at else None,
            "submitted_by": sub.submitted_by,
            "note": sub.note,
            "documents": [{"id": d.id, "filename": d.filename, "mime": d.mime, "url": f"/cash/worker/documents/{d.id}"} for d in docs],
        })
    return items


def _incomplete(analysis: dict) -> list[str]:
    """Retire les éléments que l'analyse n'a pas pu lire en entier, pour que l'envoi soit quand même enregistré."""
    problems = []
    keep = []
    for rep in analysis.get("reports") or []:
        if parse_date_any(rep.get("period_start")) and to_float(rep.get("cash_amount")) is not None:
            keep.append(rep)
        else:
            problems.append("un relevé de caisse sans période ou sans montant espèces lisible")
    analysis["reports"] = keep
    keep = []
    for dep in analysis.get("deposits") or []:
        if parse_date_any(dep.get("deposit_date")) and to_float(dep.get("amount")) is not None:
            keep.append(dep)
        else:
            problems.append("un dépôt sans date ou sans montant lisible")
    analysis["deposits"] = keep
    keep = []
    for e in analysis.get("expenses") or []:
        if to_float(e.get("amount")) is not None:
            keep.append(e)
        else:
            problems.append("une facture sans montant lisible")
    analysis["expenses"] = keep
    return problems


def apply_worker_extractions(db: Session, sub: CashSubmission, items: list[dict], who: str = "Analyse Claude") -> dict:
    """Enregistre les lectures faites par la tâche planifiée, puis confirme l'envoi (calcul, alertes, e-mails)."""
    if sub.status != "queued":
        raise ValueError(f"Envoi n°{sub.id} déjà traité (statut {sub.status})")
    store = store_by_id(sub.store_id)
    if not store:
        raise ValueError("Magasin inconnu")
    docs = {d.id: d for d in db.execute(select(CashDocument).where(CashDocument.submission_id == sub.id)).scalars().all()}
    pairs: list[tuple[int, dict]] = []
    seen: set[int] = set()
    for it in items or []:
        try:
            did = int(it.get("document_id"))
        except (TypeError, ValueError):
            raise ValueError("document_id manquant ou invalide")
        if did not in docs:
            raise ValueError(f"Le document {did} n'appartient pas à l'envoi n°{sub.id}")
        ex = it.get("extraction") if isinstance(it.get("extraction"), dict) else {"type": "autre", "confidence": 0, "warnings": ["extraction absente"]}
        d = docs[did]
        d.kind = str(ex.get("type") or "autre")
        d.confidence = to_float(ex.get("confidence"))
        d.extracted_json = json.dumps(ex, ensure_ascii=False)
        pairs.append((did, ex))
        seen.add(did)
    for did in sorted(set(docs) - seen):
        ex = {"type": "autre", "confidence": 0, "warnings": ["document non analysé par la tâche planifiée"]}
        docs[did].extracted_json = json.dumps(ex, ensure_ascii=False)
        pairs.append((did, ex))
    analysis = analyze_documents(db, store, pairs)
    problems = _incomplete(analysis)
    compute_totals(analysis)
    sub.analysis_json = json.dumps(analysis, ensure_ascii=False)
    db.flush()
    result = confirm_submission(db, sub, {"force": True}, "owner", who=sub.submitted_by or who)
    resolve_alert(db, f"stale_queue:{sub.id}")
    if problems:
        a, _ = upsert_alert(db, sub.store_id, "analysis_incomplete", "warning", "Analyse incomplète d'un envoi",
                            f"{store['label']}, envoi n°{sub.id} : {', '.join(sorted(set(problems)))}. Ouvre l'envoi pour vérifier les photos.",
                            f"incomplete:{sub.id}", submission_id=sub.id)
        result["alerts"].append(alert_to_dict(a))
    db.commit()
    result["incomplete"] = problems
    return result


def check_stale_queue(db: Session, store_id: str, max_hours: float = 14.0) -> dict:
    """Alerte si des photos attendent l'analyse trop longtemps (tâche planifiée arrêtée ou en échec)."""
    limit = datetime.utcnow() - timedelta(hours=max_hours)
    subs = db.execute(select(CashSubmission).where(CashSubmission.store_id == store_id, CashSubmission.status == "queued")).scalars().all()
    stale = 0
    for sub in subs:
        if sub.created_at and sub.created_at < limit:
            stale += 1
            upsert_alert(db, store_id, "stale_queue", "warning", "Photos en attente d'analyse",
                         f"{_store_label(store_id)} : l'envoi n°{sub.id} du {fr(sub.created_at.date())} n'a pas encore été analysé. "
                         "Vérifie que la tâche planifiée « Analyse des dépôts d'espèces » tourne bien.",
                         f"stale_queue:{sub.id}", submission_id=sub.id, ref_date=sub.created_at.date())
    db.commit()
    return {"queued": len(subs), "stale": stale}


def _period_label(analysis: dict) -> str:
    days = analysis.get("coverage", {}).get("days") or []
    if not days:
        ds = [parse_date_any(rp.get("period_start")) for rp in analysis.get("reports") or []]
        de = [parse_date_any(rp.get("period_end")) for rp in analysis.get("reports") or []]
        ds, de = [d for d in ds if d], [d for d in de if d]
        if ds and de:
            return f"du {fr(min(ds))} au {fr(max(de))}"
        return "période inconnue"
    a, b = parse_date_any(days[0]), parse_date_any(days[-1])
    return f"du {fr(a)} au {fr(b)}" if a != b else f"le {fr(a)}"


# ----------------------------------------------------------------------------- sérialisation

def submission_to_dict(db: Session, sub: CashSubmission, with_docs: bool = True) -> dict:
    analysis = json.loads(sub.analysis_json) if sub.analysis_json else {}
    out = {
        "id": sub.id,
        "store_id": sub.store_id,
        "store_label": _store_label(sub.store_id),
        "status": sub.status,
        "created_at": sub.created_at.isoformat() if sub.created_at else None,
        "confirmed_at": sub.confirmed_at.isoformat() if sub.confirmed_at else None,
        "submitted_by": sub.submitted_by,
        "note": sub.note,
        "expected_amount": sub.expected_amount,
        "deposit_amount": sub.deposit_amount,
        "ecart": sub.ecart,
        "period": _period_label(analysis) if analysis else None,
        "analysis": analysis,
    }
    if with_docs:
        docs = db.execute(select(CashDocument).where(CashDocument.submission_id == sub.id).order_by(CashDocument.id)).scalars().all()
        out["documents"] = [{"id": d.id, "kind": d.kind, "filename": d.filename, "mime": d.mime, "confidence": d.confidence} for d in docs]
        deps = db.execute(select(CashDeposit).where(CashDeposit.submission_id == sub.id)).scalars().all()
        out["bank"] = [{"id": d.id, "bag_number": d.bag_number, "amount": d.amount, "deposit_date": iso(d.deposit_date),
                        "bank_status": d.bank_status, "bank_date": iso(d.bank_date), "bank_amount": d.bank_amount} for d in deps]
    return out


# ----------------------------------------------------------------------------- contrôles périodiques

def check_missing_days(db: Session, store_id: str, r: dict) -> dict:
    today = date.today()
    limit = today - timedelta(days=int(r.get("missing_days_after") or 5))
    reports = db.execute(
        select(CashReport).join(CashSubmission, CashSubmission.id == CashReport.submission_id)
        .where(CashReport.store_id == store_id, CashSubmission.status == "confirmed")
    ).scalars().all()
    if not reports:
        return {"missing": 0}
    covered: set[date] = set()
    for rep in reports:
        if (rep.period_end - rep.period_start).days < 62:
            covered.update(daterange(rep.period_start, rep.period_end))
    start = max(min(covered), today - timedelta(days=90))
    missing = []
    for d in daterange(start, limit):
        key = f"missing_day:{store_id}:{d.isoformat()}"
        if d in covered or is_closed_day(d, r):
            resolve_alert(db, key)
            continue
        missing.append(d)
        upsert_alert(db, store_id, "missing_day", "warning", f"Journée du {fr(d)} sans relevé de caisse",
                     f"{_store_label(store_id)} : aucun relevé reçu couvrant le {frl(d)} (plus de {r.get('missing_days_after')} jours).",
                     key, ref_date=d)
    db.commit()
    return {"missing": len(missing)}


def check_undeposited(db: Session, store_id: str, r: dict) -> dict:
    today = date.today()
    limit = today - timedelta(days=int(r.get("undeposited_after_days") or 7))
    subs = db.execute(select(CashSubmission).where(CashSubmission.store_id == store_id, CashSubmission.status == "confirmed")).scalars().all()
    count = 0
    for sub in subs:
        reps = db.execute(select(CashReport).where(CashReport.submission_id == sub.id)).scalars().all()
        deps = db.execute(select(CashDeposit).where(CashDeposit.submission_id == sub.id)).scalars().all()
        key = f"undeposited:{sub.id}"
        if not reps or deps:
            resolve_alert(db, key)
            continue
        end = max(rp.period_end for rp in reps)
        later = db.execute(select(CashDeposit).where(CashDeposit.store_id == store_id, CashDeposit.deposit_date >= end)).scalars().first()
        if later or end > limit:
            resolve_alert(db, key)
            continue
        total = sum(rp.cash_amount for rp in reps)
        count += 1
        upsert_alert(db, store_id, "undeposited", "warning", f"{eur(total)} encaissés sans dépôt depuis le {fr(end)}",
                     f"{_store_label(store_id)} : le relevé jusqu'au {fr(end)} n'a pas de dépôt associé après {r.get('undeposited_after_days')} jours.",
                     key, submission_id=sub.id, ref_date=end)
    db.commit()
    return {"undeposited": count}


BAG_RE = re.compile(r"\bESP\s+(\d{10})\b")
CASH_DEPOSIT_RE = re.compile(r"\b(DPT|DEPOT|VERST|VERSEMENT)\b.*\bESP", re.I)


def store_for_entity(entity: Optional[str]) -> Optional[dict]:
    for s in config_stores():
        if (s.get("entity") or "").upper() == (entity or "").upper():
            return s
    return None


def import_bank_transactions(db: Session, entity: str, transactions: list[dict], source: str = "pennylane") -> dict:
    """Enregistre les dépôts d'espèces d'un relevé bancaire (les autres lignes sont ignorées)."""
    store = store_for_entity(entity)
    if not store:
        raise ValueError(f"Société inconnue : {entity}")
    added = updated = ignored = 0
    for t in transactions or []:
        label = str(t.get("label") or t.get("description") or "")
        amount = to_float(t.get("amount"))
        tdate = parse_date_any(t.get("date"))
        tid = str(t.get("id") or "").strip()
        if not tid or amount is None or amount <= 0 or not tdate or not CASH_DEPOSIT_RE.search(label):
            ignored += 1
            continue
        m = BAG_RE.search(label)
        row = db.get(CashBankTransaction, tid)
        if row:
            row.amount, row.tx_date, row.label = amount, tdate, label
            row.bag_number = m.group(1) if m else row.bag_number
            updated += 1
        else:
            db.add(CashBankTransaction(id=tid, entity=entity.upper(), store_id=store["id"], tx_date=tdate, amount=amount,
                                       label=label, bag_number=m.group(1) if m else None, source=source))
            added += 1
    db.commit()
    set_setting(db, f"bank_import_at:{store['id']}", datetime.utcnow().isoformat())
    return {"store_id": store["id"], "added": added, "updated": updated, "ignored": ignored}


def _stored_bank_transactions(db: Session, store_id: str, since: date) -> list[dict]:
    rows = db.execute(select(CashBankTransaction).where(CashBankTransaction.store_id == store_id, CashBankTransaction.tx_date >= since)).scalars().all()
    return [{"id": r.id, "date": r.tx_date.isoformat(), "amount": r.amount, "label": r.label} for r in rows]


def bank_import_recent(db: Session, store_id: str, max_days: int = 3) -> bool:
    last = get_setting(db, f"bank_import_at:{store_id}")
    try:
        return bool(last) and datetime.utcnow() - datetime.fromisoformat(last) < timedelta(days=max_days)
    except ValueError:
        return False


def check_bank_without_documents(db: Session, store: dict, r: dict) -> dict:
    """Dépôts vus en banque sans bordereau envoyé sur le site (à partir du premier envoi du magasin)."""
    first_sub = db.execute(select(CashSubmission).where(CashSubmission.store_id == store["id"]).order_by(CashSubmission.created_at)).scalars().first()
    if not first_sub:
        return {"checked": 0}
    since = first_sub.created_at.date()
    grace = date.today() - timedelta(days=2)
    deposits = db.execute(select(CashDeposit).join(CashSubmission, CashSubmission.id == CashDeposit.submission_id)
                          .where(CashDeposit.store_id == store["id"], CashSubmission.status == "confirmed")).scalars().all()
    txs = db.execute(select(CashBankTransaction).where(CashBankTransaction.store_id == store["id"], CashBankTransaction.tx_date >= since)).scalars().all()
    missing = 0
    for t in txs:
        key = f"bank_without_docs:{t.id}"
        found = any((t.bag_number and d.bag_number == t.bag_number) or
                    (abs(d.amount - t.amount) < 0.01 and abs((d.deposit_date - t.tx_date).days) <= 6) for d in deposits)
        if found or t.tx_date > grace:
            resolve_alert(db, key)
            continue
        missing += 1
        upsert_alert(db, store["id"], "bank_without_docs", "warning", f"Dépôt de {eur(t.amount)} en banque sans justificatif",
                     f"{store['label']} : un dépôt d'espèces de {eur(t.amount)} est arrivé sur le compte {store.get('entity')} le {fr(t.tx_date)}"
                     f"{' (sac ' + t.bag_number + ')' if t.bag_number else ''}, mais aucun bordereau ni relevé correspondant n'a été envoyé sur le site.",
                     key, ref_date=t.tx_date)
    db.commit()
    return {"checked": len(txs), "without_docs": missing}


def check_bank(db: Session, store: dict, r: dict) -> dict:
    entity = store.get("entity")
    pending = db.execute(select(CashDeposit).where(CashDeposit.store_id == store["id"], CashDeposit.bank_status.in_(("pending", "missing", "unchecked")))).scalars().all()
    if not pending:
        return {"checked": 0}
    since = min(d.deposit_date for d in pending) - timedelta(days=3)
    if cash_bank.configured(entity):
        try:
            txs = cash_bank.list_incoming_transactions(entity, since)
        except Exception as e:
            log.error("Pennylane %s: %s", entity, e)
            return {"checked": 0, "error": str(e)}
    elif bank_import_recent(db, store["id"], max_days=7):
        txs = _stored_bank_transactions(db, store["id"], since)  # relevé importé par la synchronisation Pennylane
    else:
        for d in pending:
            d.bank_status = "unchecked"
        db.commit()
        return {"checked": 0, "reason": "relevé bancaire non disponible"}
    today = date.today()
    limit = today - timedelta(days=int(r.get("bank_credit_after_days") or 4))
    matched = missing = 0
    for d in pending:
        t = cash_bank.match_deposit({"bag_number": d.bag_number, "amount": d.amount, "deposit_date": d.deposit_date}, txs)
        key = f"bank_missing:{d.id}"
        if t:
            d.bank_status = "matched"
            d.bank_transaction_id = t["id"]
            d.bank_date = parse_date_any(t["date"])
            d.bank_amount = t["amount"]
            matched += 1
            resolve_alert(db, key)
        elif d.deposit_date <= limit:
            d.bank_status = "missing"
            missing += 1
            upsert_alert(db, store["id"], "bank_missing", "critical", f"Dépôt de {eur(d.amount)} non crédité en banque",
                         f"{store['label']} : le dépôt du {fr(d.deposit_date)} (sac {d.bag_number or '?'}) n'apparaît pas sur le compte {entity} après {r.get('bank_credit_after_days')} jours.",
                         key, submission_id=d.submission_id, ref_date=d.deposit_date)
    db.commit()
    return {"checked": len(pending), "matched": matched, "missing": missing}


def sync_sheet(db: Session, store: dict, force: bool = False, r: Optional[dict] = None) -> dict:
    r = r or effective_rules(db)
    if not store.get("sheet_id"):
        return {"ok": False, "reason": "pas de sheet configuré"}
    if not SheetsClient.configured():
        return {"ok": False, "reason": "GOOGLE_SERVICE_ACCOUNT_JSON non configuré"}
    last = latest_snapshot(db, store["id"])
    if last and not force and last.ok and (datetime.utcnow() - last.fetched_at) < timedelta(hours=float(r.get("sheet_sync_hours") or 6)):
        return {"ok": True, "cached": True, "fetched_at": last.fetched_at.isoformat()}
    try:
        data = SheetsClient().read_current_period(store["sheet_id"])
        snap = CashSheetSnapshot(store_id=store["id"], tab_title=data.get("tab_title"), data_json=json.dumps(data, ensure_ascii=False), ok=True)
        db.add(snap)
        db.commit()
        return {"ok": True, "tab": data.get("tab_title"), "deposits": len(data.get("deposits") or [])}
    except Exception as e:
        log.error("Sheet %s: %s", store["id"], e)
        db.add(CashSheetSnapshot(store_id=store["id"], data_json="{}", ok=False, error=str(e)))
        db.commit()
        return {"ok": False, "error": str(e)}


def latest_snapshot(db: Session, store_id: str, ok_only: bool = False) -> Optional[CashSheetSnapshot]:
    q = select(CashSheetSnapshot).where(CashSheetSnapshot.store_id == store_id)
    if ok_only:
        q = q.where(CashSheetSnapshot.ok.is_(True))
    return db.execute(q.order_by(CashSheetSnapshot.fetched_at.desc())).scalars().first()


def check_sheet_consistency(db: Session, store: dict, r: dict) -> dict:
    snap = latest_snapshot(db, store["id"], ok_only=True)
    if not snap:
        return {"checked": False}
    data = json.loads(snap.data_json or "{}")
    sheet_deps = [d for d in data.get("deposits") or [] if d.get("date")]
    today = date.today()
    grace = today - timedelta(days=3)
    first_sub = db.execute(select(CashSubmission).where(CashSubmission.store_id == store["id"], CashSubmission.status == "confirmed").order_by(CashSubmission.created_at)).scalars().first()
    app_deps = db.execute(select(CashDeposit).where(CashDeposit.store_id == store["id"])).scalars().all()

    def near(a: float, b: float) -> bool:
        return abs(a - b) < 0.01

    missing_in_sheet = 0
    for d in app_deps:
        key = f"sheet_missing:{d.id}"
        found = any(near(sd["amount"], d.amount) and abs((parse_date_any(sd["date"]) - d.deposit_date).days) <= 3 for sd in sheet_deps if parse_date_any(sd["date"]))
        if found or d.deposit_date > grace:
            resolve_alert(db, key)
            continue
        sheet_start = min((parse_date_any(sd["date"]) for sd in sheet_deps if parse_date_any(sd["date"])), default=None)
        if sheet_start and d.deposit_date < sheet_start:
            continue  # dépôt antérieur à l'onglet courant
        missing_in_sheet += 1
        upsert_alert(db, store["id"], "sheet_missing_deposit", "info", f"Dépôt de {eur(d.amount)} du {fr(d.deposit_date)} absent du sheet",
                     f"{store['label']} : ce dépôt confirmé sur le site n'est pas (encore) dans la liste des dépôts de l'onglet « {snap.tab_title} ».",
                     key, submission_id=d.submission_id, ref_date=d.deposit_date)
    without_doc = 0
    if first_sub:
        start = first_sub.created_at.date()
        for sd in sheet_deps:
            sdate = parse_date_any(sd["date"])
            if not sdate or sdate < start or sdate > grace:
                continue
            key = f"sheet_without_doc:{store['id']}:{sdate.isoformat()}:{sd['amount']}"
            found = any(near(sd["amount"], d.amount) and abs((sdate - d.deposit_date).days) <= 3 for d in app_deps)
            if found:
                resolve_alert(db, key)
                continue
            without_doc += 1
            upsert_alert(db, store["id"], "sheet_deposit_without_doc", "warning", f"Dépôt de {eur(sd['amount'])} ({fr(sdate)}) dans le sheet sans justificatif",
                         f"{store['label']} : ce dépôt est saisi dans le sheet mais aucun bordereau/ticket correspondant n'a été envoyé sur le site.",
                         key, ref_date=sdate)
    db.commit()
    return {"checked": True, "missing_in_sheet": missing_in_sheet, "without_doc": without_doc}


def run_checks(db: Session, force_sheets: bool = False) -> dict:
    r = effective_rules(db)
    out: dict[str, Any] = {"stores": {}, "ran_at": datetime.utcnow().isoformat()}
    new_critical: list[CashAlert] = []
    for store in config_stores():
        sid = store["id"]
        before = {a.id for a in db.execute(select(CashAlert).where(CashAlert.store_id == sid, CashAlert.status == "open")).scalars().all()}
        res = {
            "missing_days": check_missing_days(db, sid, r),
            "undeposited": check_undeposited(db, sid, r),
            "bank": check_bank(db, store, r),
            "bank_without_docs": check_bank_without_documents(db, store, r),
            "sheet": sync_sheet(db, store, force=force_sheets, r=r),
        }
        res["sheet_consistency"] = check_sheet_consistency(db, store, r)
        res["queue"] = check_stale_queue(db, sid)
        out["stores"][sid] = res
        after = db.execute(select(CashAlert).where(CashAlert.store_id == sid, CashAlert.status == "open")).scalars().all()
        new_critical.extend(a for a in after if a.id not in before and a.severity == "critical")
    out["notified_critical"] = notify_critical(db, new_critical)
    out["digest"] = send_digest(db)
    set_setting(db, "last_checks_at", out["ran_at"])
    return out


# ----------------------------------------------------------------------------- synthèse (dashboard)

def _bank_row(t: CashBankTransaction, deposits: list[CashDeposit]) -> dict:
    match = next((d for d in deposits if (t.bag_number and d.bag_number == t.bag_number) or d.bank_transaction_id == t.id), None)
    if match is None:
        match = next((d for d in deposits if abs(d.amount - t.amount) < 0.01 and abs((d.deposit_date - t.tx_date).days) <= 6), None)
    return {"id": t.id, "date": iso(t.tx_date), "amount": t.amount, "bag_number": t.bag_number,
            "submission_id": match.submission_id if match else None, "site_amount": match.amount if match else None}


def month_bounds(month: str) -> tuple[date, date]:
    y, m = int(month[:4]), int(month[5:7])
    start = date(y, m, 1)
    end = (date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)) - timedelta(days=1)
    return start, end


def dashboard(db: Session, store_id: Optional[str], month: str) -> dict:
    r = effective_rules(db)
    start, end = month_bounds(month)
    today = date.today()
    store_ids = [store_id] if store_id else [s["id"] for s in config_stores()]
    out: dict[str, Any] = {"month": month, "stores": [], "rules": r}

    for sid in store_ids:
        store = store_by_id(sid)
        if not store:
            continue
        subs = db.execute(select(CashSubmission).where(CashSubmission.store_id == sid, CashSubmission.status == "confirmed").order_by(CashSubmission.created_at.desc())).scalars().all()
        reports = db.execute(select(CashReport).join(CashSubmission, CashSubmission.id == CashReport.submission_id)
                             .where(CashReport.store_id == sid, CashSubmission.status == "confirmed")).scalars().all()
        deposits = db.execute(select(CashDeposit).where(CashDeposit.store_id == sid)).scalars().all()
        expenses = db.execute(select(CashExpense).where(CashExpense.store_id == sid)).scalars().all()
        alerts = db.execute(select(CashAlert).where(CashAlert.store_id == sid, CashAlert.status != "resolved").order_by(CashAlert.created_at.desc())).scalars().all()
        queued = db.execute(select(CashSubmission).where(CashSubmission.store_id == sid, CashSubmission.status == "queued").order_by(CashSubmission.created_at)).scalars().all()

        # Couverture des journées
        covered: dict[date, list[int]] = {}
        for rep in reports:
            if (rep.period_end - rep.period_start).days < 62:
                for d in daterange(rep.period_start, rep.period_end):
                    covered.setdefault(d, []).append(rep.submission_id)
        alert_days = {a.ref_date for a in alerts if a.ref_date}
        alert_subs = {a.submission_id for a in alerts if a.submission_id}
        first_covered = min(covered) if covered else None
        first_sub = db.execute(select(CashSubmission).where(CashSubmission.store_id == sid).order_by(CashSubmission.created_at)).scalars().first()
        go_live = first_sub.created_at.date() if first_sub and first_sub.created_at else None
        # Avant le premier envoi sur le site, les journées ne sont ni « traitées » ni « en retard »
        baseline = min([x for x in (first_covered, go_live) if x], default=None)
        calendar = []
        for d in daterange(start, end):
            if d > today:
                status = "future"
            elif is_closed_day(d, r):
                status = "closed"
            elif d in covered:
                status = "alert" if (d in alert_days or any(s in alert_subs for s in covered[d])) else "ok"
            elif baseline is None or d < baseline:
                status = "before"
            elif d > today - timedelta(days=int(r.get("missing_days_after") or 5)):
                status = "pending"
            else:
                status = "missing"
            calendar.append({"date": d.isoformat(), "status": status, "submissions": covered.get(d, [])})

        m_bank = db.execute(select(CashBankTransaction).where(CashBankTransaction.store_id == sid, CashBankTransaction.tx_date >= start,
                                                              CashBankTransaction.tx_date <= end)).scalars().all()
        m_reports = [rp for rp in reports if start <= rp.period_end <= end]
        m_deposits = [d for d in deposits if start <= d.deposit_date <= end]
        m_expenses = [e for e in expenses if e.expense_date and start <= e.expense_date <= end]
        m_subs = [s for s in subs if s.confirmed_at and start <= s.confirmed_at.date() <= end]
        cash = sum(rp.cash_amount for rp in m_reports)
        dep = sum(d.amount for d in m_deposits)
        exp = sum(e.amount for e in m_expenses)
        ecart_total = sum((s.ecart or 0) for s in m_subs if s.ecart is not None)
        operators_diff: dict[str, float] = {}
        for rp in m_reports:
            try:
                for op in json.loads(rp.operators_json or "[]"):
                    name = str(op.get("name") or "?")
                    diff = to_float(op.get("difference"))
                    if diff is not None:
                        operators_diff[name] = round(operators_diff.get(name, 0.0) + diff, 2)
            except Exception:
                pass

        snap = latest_snapshot(db, sid, ok_only=True)
        sheet = None
        if snap:
            data = json.loads(snap.data_json or "{}")
            diffs = data.get("register_diffs") or []
            m_diffs = [x for x in diffs if x.get("date") and start <= parse_date_any(x["date"]) <= end]
            by_person: dict[str, float] = {}
            for x in m_diffs:
                by_person[x.get("person") or "?"] = round(by_person.get(x.get("person") or "?", 0.0) + (x.get("amount") or 0), 2)
            sheet = {
                "fetched_at": snap.fetched_at.isoformat(),
                "tab_title": snap.tab_title,
                "totals": data.get("totals"),
                "count": data.get("count"),
                "deposits": data.get("deposits"),
                "register_diffs_month": round(sum(x.get("amount") or 0 for x in m_diffs), 2),
                "register_diffs_by_person": by_person,
                "expenses": data.get("expenses"),
                "coins_added": data.get("coins_added"),
            }
        last_snap_any = latest_snapshot(db, sid)
        out["stores"].append({
            "id": sid,
            "label": store["label"],
            "entity": store.get("entity"),
            "color": store.get("color"),
            "pos_system": store.get("pos_system"),
            "kpis": {
                "cash_collected": r2(cash),
                "deposited": r2(dep),
                "expenses": r2(exp),
                "ecart_total": r2(ecart_total),
                "submissions": len(m_subs),
                "deposits_count": len(m_deposits),
                "open_alerts": len([a for a in alerts if a.status == "open"]),
                "critical_alerts": len([a for a in alerts if a.status == "open" and a.severity == "critical"]),
                "missing_days": len([c for c in calendar if c["status"] == "missing"]),
                "bank_matched": len([d for d in m_deposits if d.bank_status == "matched"]),
                "bank_missing": len([d for d in m_deposits if d.bank_status == "missing"]),
                "bank_unchecked": len([d for d in m_deposits if d.bank_status in ("pending", "unchecked")]),
                "queued": len(queued),
                "bank_cash_total": r2(sum(t.amount for t in m_bank)),
                "bank_cash_count": len(m_bank),
            },
            "bank_deposits": [_bank_row(t, deposits) for t in sorted(m_bank, key=lambda x: x.tx_date, reverse=True)],
            "bank_import_at": get_setting(db, f"bank_import_at:{sid}"),
            "go_live": iso(go_live),
            "queued": [{"id": q.id, "created_at": q.created_at.isoformat() if q.created_at else None, "submitted_by": q.submitted_by, "note": q.note,
                        "documents_count": len(db.execute(select(CashDocument).where(CashDocument.submission_id == q.id)).scalars().all())}
                       for q in queued],
            "last_covered": iso(max(covered)) if covered else None,
            "last_submission_at": subs[0].created_at.isoformat() if subs else None,
            "calendar": calendar,
            "submissions": [submission_to_dict(db, s, with_docs=False) | {"documents_count": db.execute(select(CashDocument).where(CashDocument.submission_id == s.id)).scalars().all().__len__()} for s in m_subs],
            "deposits": [{"id": d.id, "submission_id": d.submission_id, "deposit_date": iso(d.deposit_date), "amount": d.amount, "bag_number": d.bag_number,
                          "slip_amount": d.slip_amount, "receipt_amount": d.receipt_amount, "bank_status": d.bank_status, "bank_date": iso(d.bank_date)}
                         for d in sorted(m_deposits, key=lambda x: x.deposit_date, reverse=True)],
            "alerts": [alert_to_dict(a) for a in alerts],
            "operators_diff": operators_diff,
            "sheet": sheet,
            "sheet_error": (last_snap_any.error if last_snap_any and not last_snap_any.ok else None),
        })
    out["generated_at"] = datetime.utcnow().isoformat()
    out["last_checks_at"] = get_setting(db, "last_checks_at")
    out["integrations"] = {
        "email": cash_notify.configured(),
        "sheets": SheetsClient.configured(),
        "pennylane": {s["id"]: cash_bank.configured(s.get("entity")) or bank_import_recent(db, s["id"]) for s in config_stores()},
        "store_labels": {s["id"]: s["label"] for s in config_stores()},
        "vision_mock": os.getenv("CASH_MOCK_EXTRACTION", "") in ("1", "true", "yes"),
        "vision_provider": (os.getenv("CASH_VISION_PROVIDER") or "anthropic").lower(),
        "vision_key": bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")) if (os.getenv("CASH_VISION_PROVIDER") or "anthropic").lower() == "anthropic" else bool(os.getenv("OPENAI_API_KEY")),
        "analysis_mode": analysis_mode(),
        "analysis_times": [f"{h}h{m:02d}" for h, m in analysis_times()],
        "last_worker_run_at": get_setting(db, "last_worker_run_at"),
        "analysis_requested_at": get_setting(db, "analysis_requested_at"),
        "routine_fired_at": get_setting(db, "routine_fired_at"),
        "routine_fire_ready": cash_routine.configured(),
        "queued_total": len(db.execute(select(CashSubmission).where(CashSubmission.status == "queued")).scalars().all()),
    }
    return out
