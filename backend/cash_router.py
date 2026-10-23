"""Routes HTTP du module dépôts d'espèces (préfixe /cash)."""
from __future__ import annotations

import json
import logging
import pathlib
import re
import uuid
from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import cash_service as svc
from .cash_auth import User, authenticate, make_token, require_any, require_owner
from .cash_config import docs_dir, store_by_id, stores as config_stores
from .cash_extract import get_extractor
from .cash_models import CashAlert, CashDocument, CashSubmission
from .database import get_db

log = logging.getLogger("cash.router")
router = APIRouter(prefix="/cash", tags=["cash"])

MAX_FILES = 12
MAX_FILE_BYTES = 25 * 1024 * 1024


class AuthIn(BaseModel):
    user_id: str
    code: str


@router.post("/auth")
def auth(payload: AuthIn):
    user = authenticate(payload.user_id, payload.code)
    if not user:
        raise HTTPException(status_code=401, detail="Identifiant ou code incorrect")
    return {"role": user.role, "user_id": user.id, "label": user.label, "token": make_token(user)}


@router.get("/me")
def me(user: User = Depends(require_any)):
    return {"role": user.role, "user_id": user.id, "label": user.label}


@router.get("/stores")
def list_stores(user: User = Depends(require_any)):
    return {"stores": [{"id": s["id"], "label": s["label"], "entity": s["entity"], "color": s["color"], "pos_system": s["pos_system"]} for s in config_stores()]}


def _safe_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "photo")
    return name[:120] or "photo"


@router.post("/submissions")
async def create_submission(
    store_id: str = Form(...),
    files: List[UploadFile] = File(...),
    note: Optional[str] = Form(None),
    user: User = Depends(require_any),
    db: Session = Depends(get_db),
):
    store = store_by_id(store_id)
    if not store:
        raise HTTPException(status_code=400, detail="Magasin inconnu")
    if not files:
        raise HTTPException(status_code=400, detail="Aucun fichier")
    if len(files) > MAX_FILES:
        raise HTTPException(status_code=400, detail=f"{MAX_FILES} fichiers maximum par envoi")

    sub = CashSubmission(store_id=store_id, status="pending", submitted_by=f"{user.label} ({user.id})", note=note)
    db.add(sub)
    db.commit()
    db.refresh(sub)

    folder = docs_dir() / store_id / f"{date.today():%Y-%m}" / str(sub.id)
    folder.mkdir(parents=True, exist_ok=True)
    extractor = get_extractor()
    extracted_pairs: list[tuple[int, dict]] = []
    for uf in files:
        content = await uf.read()
        if not content:
            continue
        if len(content) > MAX_FILE_BYTES:
            raise HTTPException(status_code=413, detail=f"Fichier trop volumineux : {uf.filename}")
        mime = uf.content_type or "image/jpeg"
        fname = f"{uuid.uuid4().hex[:8]}_{_safe_name(uf.filename or 'photo.jpg')}"
        path = folder / fname
        path.write_bytes(content)
        try:
            extracted = extractor.extract(content, mime, uf.filename or "")
        except Exception as e:
            log.exception("extraction échouée")
            extracted = {"type": "autre", "confidence": 0, "warnings": [f"Analyse impossible : {e}"]}
        doc = CashDocument(
            submission_id=sub.id, store_id=store_id, kind=str(extracted.get("type") or "autre"), filename=uf.filename or fname,
            path=str(path), mime=mime, confidence=svc.to_float(extracted.get("confidence")), extracted_json=json.dumps(extracted, ensure_ascii=False),
        )
        db.add(doc)
        db.flush()
        extracted_pairs.append((doc.id, extracted))
    if not extracted_pairs:
        db.delete(sub)
        db.commit()
        raise HTTPException(status_code=400, detail="Fichiers vides")

    analysis = svc.analyze_documents(db, store, extracted_pairs)
    sub.analysis_json = json.dumps(analysis, ensure_ascii=False)
    sub.expected_amount = analysis.get("expected_amount")
    sub.deposit_amount = analysis.get("deposit_amount")
    sub.ecart = analysis.get("ecart")
    db.commit()
    return {"submission_id": sub.id, "analysis": analysis}


class ConfirmIn(BaseModel):
    reports: Optional[list] = None
    deposits: Optional[list] = None
    expenses: Optional[list] = None
    note: Optional[str] = None
    force: bool = False


@router.post("/submissions/{submission_id}/confirm")
def confirm(submission_id: int, payload: ConfirmIn, user: User = Depends(require_any), db: Session = Depends(get_db)):
    sub = db.get(CashSubmission, submission_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Envoi introuvable")
    try:
        return svc.confirm_submission(db, sub, payload.model_dump(), user.role, f"{user.label} ({user.id})")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/submissions/{submission_id}/cancel")
def cancel(submission_id: int, user: User = Depends(require_any), db: Session = Depends(get_db)):
    sub = db.get(CashSubmission, submission_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Envoi introuvable")
    if sub.status == "confirmed" and user.role != "owner":
        raise HTTPException(status_code=403, detail="Un envoi confirmé ne peut être annulé que par le gérant")
    sub.status = "cancelled"
    db.commit()
    return {"ok": True}


@router.get("/submissions")
def list_submissions(store_id: Optional[str] = None, month: Optional[str] = None, status: Optional[str] = None, limit: int = 100,
                     user: User = Depends(require_owner), db: Session = Depends(get_db)):
    q = select(CashSubmission)
    if store_id:
        q = q.where(CashSubmission.store_id == store_id)
    if status:
        q = q.where(CashSubmission.status == status)
    if month:
        a, b = svc.month_bounds(month)
        q = q.where(CashSubmission.created_at >= datetime.combine(a, datetime.min.time()), CashSubmission.created_at <= datetime.combine(b, datetime.max.time()))
    rows = db.execute(q.order_by(CashSubmission.created_at.desc()).limit(limit)).scalars().all()
    return {"items": [svc.submission_to_dict(db, s) for s in rows]}


@router.get("/submissions/{submission_id}")
def get_submission(submission_id: int, user: User = Depends(require_any), db: Session = Depends(get_db)):
    sub = db.get(CashSubmission, submission_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Envoi introuvable")
    return svc.submission_to_dict(db, sub)


@router.get("/documents/{document_id}")
def get_document(document_id: int, user: User = Depends(require_any), db: Session = Depends(get_db)):
    doc = db.get(CashDocument, document_id)
    if not doc or not pathlib.Path(doc.path).exists():
        raise HTTPException(status_code=404, detail="Document introuvable")
    return FileResponse(doc.path, media_type=doc.mime, filename=doc.filename)


@router.get("/manager/recent")
def manager_recent(store_id: str, user: User = Depends(require_any), db: Session = Depends(get_db)):
    """Les derniers envois du magasin, pour que la responsable voie ce qui est déjà parti."""
    rows = db.execute(select(CashSubmission).where(CashSubmission.store_id == store_id, CashSubmission.status == "confirmed")
                      .order_by(CashSubmission.created_at.desc()).limit(5)).scalars().all()
    items = []
    for s in rows:
        d = svc.submission_to_dict(db, s, with_docs=False)
        items.append({"id": d["id"], "period": d["period"], "deposit_amount": d["deposit_amount"], "expected_amount": d["expected_amount"], "confirmed_at": d["confirmed_at"]})
    return {"items": items}


@router.get("/dashboard")
def get_dashboard(store_id: Optional[str] = None, month: Optional[str] = None, user: User = Depends(require_owner), db: Session = Depends(get_db)):
    month = month or date.today().strftime("%Y-%m")
    if not re.match(r"^\d{4}-\d{2}$", month):
        raise HTTPException(status_code=400, detail="month doit être au format AAAA-MM")
    return svc.dashboard(db, store_id, month)


@router.get("/alerts")
def list_alerts(status: Optional[str] = "open", store_id: Optional[str] = None, limit: int = 200, user: User = Depends(require_owner), db: Session = Depends(get_db)):
    q = select(CashAlert)
    if status and status != "all":
        q = q.where(CashAlert.status == status)
    if store_id:
        q = q.where(CashAlert.store_id == store_id)
    rows = db.execute(q.order_by(CashAlert.created_at.desc()).limit(limit)).scalars().all()
    return {"items": [svc.alert_to_dict(a) for a in rows]}


@router.post("/alerts/{alert_id}/ack")
def ack_alert(alert_id: int, user: User = Depends(require_owner), db: Session = Depends(get_db)):
    a = db.get(CashAlert, alert_id)
    if not a:
        raise HTTPException(status_code=404, detail="Alerte introuvable")
    a.status = "acked"
    a.updated_at = datetime.utcnow()
    db.commit()
    return svc.alert_to_dict(a)


@router.post("/alerts/{alert_id}/reopen")
def reopen_alert(alert_id: int, user: User = Depends(require_owner), db: Session = Depends(get_db)):
    a = db.get(CashAlert, alert_id)
    if not a:
        raise HTTPException(status_code=404, detail="Alerte introuvable")
    a.status = "open"
    a.updated_at = datetime.utcnow()
    db.commit()
    return svc.alert_to_dict(a)


@router.post("/checks/run")
def run_checks(force_sheets: bool = False, user: User = Depends(require_owner), db: Session = Depends(get_db)):
    return svc.run_checks(db, force_sheets=force_sheets)


@router.post("/sheets/sync")
def sheets_sync(store_id: Optional[str] = None, user: User = Depends(require_owner), db: Session = Depends(get_db)):
    out = {}
    for s in config_stores():
        if store_id and s["id"] != store_id:
            continue
        out[s["id"]] = svc.sync_sheet(db, s, force=True)
        out[s["id"]]["consistency"] = svc.check_sheet_consistency(db, s, svc.effective_rules(db))
    return out


@router.post("/digest/send")
def digest_send(user: User = Depends(require_owner), db: Session = Depends(get_db)):
    return svc.send_digest(db, force=True)


class RulesIn(BaseModel):
    ecart_threshold_eur: Optional[float] = None
    missing_days_after: Optional[int] = None
    undeposited_after_days: Optional[int] = None
    bank_credit_after_days: Optional[int] = None
    sheet_sync_hours: Optional[float] = None
    digest_hour: Optional[int] = None
    closed_weekdays: Optional[list[int]] = None
    holidays: Optional[list[str]] = None


@router.get("/settings")
def get_settings(user: User = Depends(require_owner), db: Session = Depends(get_db)):
    return {"rules": svc.effective_rules(db)}


@router.put("/settings")
def put_settings(payload: RulesIn, user: User = Depends(require_owner), db: Session = Depends(get_db)):
    current = svc.get_setting(db, "rules", {}) or {}
    current.update({k: v for k, v in payload.model_dump().items() if v is not None})
    svc.set_setting(db, "rules", current)
    return {"rules": svc.effective_rules(db)}
