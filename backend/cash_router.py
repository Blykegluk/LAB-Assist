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
from fastapi.responses import FileResponse, PlainTextResponse, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import cash_service as svc
from .cash_auth import User, authenticate, make_token, require_admin, require_any, require_owner, require_worker
from .cash_extract import PROMPT_PATH
from .cash_config import docs_dir, store_by_id, stores as config_stores
from .cash_extract import get_extractor
from .cash_models import CashAlert, CashDocument, CashDocumentBlob, CashSubmission
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
    return {
        "stores": [{"id": s["id"], "label": s["label"], "entity": s["entity"], "color": s["color"], "pos_system": s["pos_system"]} for s in config_stores()],
        "analysis_mode": svc.analysis_mode(),
        "next_analysis": svc.next_analysis_label(),
    }


def _storage():
    """Supabase Storage si configuré (SUPABASE_URL, SUPABASE_SERVICE_ROLE, SUPABASE_BUCKET), sinon None."""
    try:
        from storage import SupabaseStorageClient

        client = SupabaseStorageClient()
        return client if client.is_configured() else None
    except Exception:
        return None


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

    deferred = svc.analysis_mode() == "deferred"
    sub = CashSubmission(store_id=store_id, status="queued" if deferred else "pending", submitted_by=f"{user.label} ({user.id})", note=note)
    db.add(sub)
    db.commit()
    db.refresh(sub)

    folder = docs_dir() / store_id / f"{date.today():%Y-%m}" / str(sub.id)
    folder.mkdir(parents=True, exist_ok=True)
    extractor = None if deferred else get_extractor()
    storage = _storage()
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
        remote_path = None
        if storage is not None:
            remote_path = f"cash/{store_id}/{date.today():%Y-%m}/{sub.id}/{fname}"
            try:
                storage.upload_bytes(remote_path, content, content_type=uf.content_type or "image/jpeg")
            except Exception as e:
                log.warning("copie Supabase Storage échouée pour %s : %s", fname, e)
                remote_path = None
        if deferred:
            doc = CashDocument(submission_id=sub.id, store_id=store_id, kind="en_attente", filename=uf.filename or fname,
                               path=str(path), remote_path=remote_path, mime=mime)
            db.add(doc)
            db.flush()
            db.add(CashDocumentBlob(document_id=doc.id, data=content))
            extracted_pairs.append((doc.id, {}))
            continue
        try:
            extracted = extractor.extract(content, mime, uf.filename or "")
        except Exception as e:
            log.exception("extraction échouée")
            extracted = {"type": "autre", "confidence": 0, "warnings": [f"Analyse impossible : {e}"]}
        doc = CashDocument(
            submission_id=sub.id, store_id=store_id, kind=str(extracted.get("type") or "autre"), filename=uf.filename or fname,
            path=str(path), remote_path=remote_path, mime=mime, confidence=svc.to_float(extracted.get("confidence")),
            extracted_json=json.dumps(extracted, ensure_ascii=False),
        )
        db.add(doc)
        db.flush()
        db.add(CashDocumentBlob(document_id=doc.id, data=content))
        extracted_pairs.append((doc.id, extracted))
    if not extracted_pairs:
        db.delete(sub)
        db.commit()
        raise HTTPException(status_code=400, detail="Fichiers vides")

    if deferred:
        db.commit()
        return {"submission_id": sub.id, "status": "queued", "documents": len(extracted_pairs), "next_analysis": svc.next_analysis_label()}

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
    return _serve_document(db, document_id)


def _serve_document(db: Session, document_id: int):
    doc = db.get(CashDocument, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document introuvable")
    if pathlib.Path(doc.path).exists():
        return FileResponse(doc.path, media_type=doc.mime, filename=doc.filename)
    blob = db.get(CashDocumentBlob, document_id)
    if blob is not None:
        return Response(content=blob.data, media_type=doc.mime, headers={"Content-Disposition": f'inline; filename="{doc.filename}"'})
    storage = _storage()
    if doc.remote_path and storage is not None:
        try:
            data = storage.download_bytes(doc.remote_path)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Document indisponible : {e}")
        return Response(content=data, media_type=doc.mime, headers={"Content-Disposition": f'inline; filename="{doc.filename}"'})
    raise HTTPException(status_code=404, detail="Document introuvable (fichier local perdu, pas de copie distante)")


@router.get("/manager/recent")
def manager_recent(store_id: str, user: User = Depends(require_any), db: Session = Depends(get_db)):
    """Les derniers envois du magasin, pour que la responsable voie ce qui est déjà parti."""
    rows = db.execute(select(CashSubmission).where(CashSubmission.store_id == store_id, CashSubmission.status.in_(("confirmed", "queued")))
                      .order_by(CashSubmission.created_at.desc()).limit(5)).scalars().all()
    items = []
    for s in rows:
        d = svc.submission_to_dict(db, s, with_docs=False)
        n_docs = len(db.execute(select(CashDocument).where(CashDocument.submission_id == s.id)).scalars().all())
        items.append({"id": d["id"], "status": d["status"], "period": d["period"], "deposit_amount": d["deposit_amount"],
                      "expected_amount": d["expected_amount"], "confirmed_at": d["confirmed_at"], "created_at": d["created_at"], "documents": n_docs})
    return {"items": items}


# ----------------------------------------------------------------------------- tâche d'analyse planifiée

@router.post("/analysis/request")
def analysis_request(user: User = Depends(require_owner), db: Session = Depends(get_db)):
    """Bouton « Analyser maintenant » du tableau de bord."""
    return svc.request_analysis(db, f"{user.label} ({user.id})")


@router.get("/worker/should-run")
def worker_should_run(who: str = Depends(require_worker), db: Session = Depends(get_db)):
    """Appelé à chaque passage horaire de la tâche : faut-il analyser maintenant ?"""
    return svc.analysis_should_run(db)


@router.get("/worker/queue")
def worker_queue(who: str = Depends(require_worker), db: Session = Depends(get_db)):
    """Envois en attente d'analyse, avec les liens vers leurs photos."""
    return {"items": svc.queue_items(db), "prompt_url": "/cash/worker/prompt",
            "result_url": "/cash/worker/submissions/{submission_id}/extractions"}


@router.get("/worker/prompt", response_class=PlainTextResponse)
def worker_prompt(who: str = Depends(require_worker)):
    """Consignes de lecture des documents (même prompt que l'analyse immédiate)."""
    return PROMPT_PATH.read_text(encoding="utf-8")


@router.get("/worker/documents/{document_id}")
def worker_document(document_id: int, who: str = Depends(require_worker), db: Session = Depends(get_db)):
    return _serve_document(db, document_id)


class WorkerDocIn(BaseModel):
    document_id: int
    extraction: dict


class WorkerResultIn(BaseModel):
    documents: list[WorkerDocIn]


@router.post("/worker/submissions/{submission_id}/extractions")
def worker_result(submission_id: int, payload: WorkerResultIn, who: str = Depends(require_worker), db: Session = Depends(get_db)):
    sub = db.get(CashSubmission, submission_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Envoi introuvable")
    try:
        out = svc.apply_worker_extractions(db, sub, [d.model_dump() for d in payload.documents])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    svc.set_setting(db, "last_worker_run_at", datetime.utcnow().isoformat())
    s = out["submission"]
    return {"submission_id": s["id"], "store": s["store_label"], "period": s["period"], "expected_amount": s["expected_amount"],
            "deposit_amount": s["deposit_amount"], "ecart": s["ecart"], "alerts": [a["title"] for a in out["alerts"]],
            "incomplete": out.get("incomplete") or []}


@router.get("/admin/diag")
def admin_diag(who: str = Depends(require_admin), db: Session = Depends(get_db)):
    """État technique sans aucun secret : base, variables présentes, comptes, file d'analyse."""
    import os

    from .cash_auth import users
    from .database import DB_DIAG, DATABASE_URL

    db_ok, db_error = True, None
    try:
        db.execute(select(CashSubmission).limit(1)).first()
    except Exception as e:  # noqa: BLE001
        db_ok, db_error = False, str(e).splitlines()[0][:300]
    names = ["DATABASE_URL", "CASH_USERS", "CASH_SECRET", "CASH_ANALYSIS_MODE", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
             "SUPABASE_URL", "SUPABASE_SERVICE_ROLE", "SUPABASE_BUCKET", "SMTP_HOST", "CASH_ALERT_EMAIL_TO",
             "GOOGLE_SERVICE_ACCOUNT_JSON", "PENNYLANE_API_KEY_AEJB", "PENNYLANE_API_KEY_JAB", "CASH_PUBLIC_URL", "RENDER_GIT_COMMIT"]
    try:
        accounts = [{"id": u.id, "role": u.role, "label": u.label} for u in users().values()]
    except Exception as e:  # noqa: BLE001
        accounts = [{"error": str(e)[:200]}]
    return {
        "db": {**DB_DIAG, "dialect": DATABASE_URL.split(":", 1)[0], "ok": db_ok, "error": db_error},
        "env_present": {n: bool(os.getenv(n)) for n in names},
        "commit": (os.getenv("RENDER_GIT_COMMIT") or "")[:12] or None,
        "accounts": accounts,
        "analysis_mode": svc.analysis_mode(),
    }


class AdminSealIn(BaseModel):
    value: str
    probe_database: bool = False  # tester la valeur comme adresse de base (et le pooler Supabase) avant de sceller


@router.post("/admin/seal")
def admin_seal(payload: AdminSealIn, who: str = Depends(require_admin)):
    """Chiffre une valeur avec la clé du serveur. Le résultat peut être versionné sans risque."""
    from sqlalchemy.engine import make_url

    from .database import _can_connect, _normalize, _supabase_pooler_candidates
    from .sealed import available, seal

    if not available():
        raise HTTPException(status_code=400, detail="Aucune clé de scellement sur le serveur")
    value = payload.value.strip()
    out: dict = {}
    if payload.probe_database:
        url = make_url(_normalize(value))
        attempts = []
        working = None
        for cand in [url] + _supabase_pooler_candidates(url):
            err = _can_connect(cand, timeout=5)
            attempts.append({"host": cand.host, "user": cand.username, "error": err})
            if err is None:
                working = cand
                break
            if err and "password authentication failed" in err:
                break
        out["attempts"] = attempts
        if working is None:
            return out
        value = working.render_as_string(hide_password=False)
        out["working_host"] = working.host
    out["sealed"] = seal(value)
    return out


class AdminRoutineIn(BaseModel):
    url: str
    token: str


@router.put("/admin/routine")
def admin_routine(payload: AdminRoutineIn, who: str = Depends(require_admin), db: Session = Depends(get_db)):
    """Déclencheur API de la tâche d'analyse (créé dans claude.ai/code/routines), gardé scellé en base."""
    from . import cash_routine

    try:
        cash_routine.store(db, payload.url, payload.token)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "configured": cash_routine.configured()}


class AdminUsersIn(BaseModel):
    users: str  # identifiant:code:role[:libellé],...


@router.put("/admin/users")
def admin_users(payload: AdminUsersIn, who: str = Depends(require_admin)):
    """Définit les comptes en base (codes hachés). Sans effet si CASH_USERS est défini sur le serveur."""
    from .cash_auth import store_users

    try:
        stored = store_users(payload.users)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    import os

    return {"accounts": stored, "overridden_by_env": bool(os.getenv("CASH_USERS"))}


class BankImportIn(BaseModel):
    entity: str  # AEJB, JAB
    transactions: list[dict]  # [{id, date, amount, label}] tels que renvoyés par Pennylane


@router.post("/worker/bank-transactions")
def worker_bank_transactions(payload: BankImportIn, who: str = Depends(require_worker), db: Session = Depends(get_db)):
    """Import du relevé bancaire (dépôts d'espèces) depuis Pennylane, puis rapprochement."""
    try:
        res = svc.import_bank_transactions(db, payload.entity, payload.transactions)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    store = store_by_id(res["store_id"])
    r = svc.effective_rules(db)
    res["bank_check"] = svc.check_bank(db, store, r)
    res["without_docs"] = svc.check_bank_without_documents(db, store, r)
    return res


@router.post("/worker/checks")
def worker_checks(who: str = Depends(require_worker), db: Session = Depends(get_db)):
    """Lance les contrôles et le récap e-mail (utile si le serveur a dormi)."""
    svc.set_setting(db, "last_worker_run_at", datetime.utcnow().isoformat())
    if not db.execute(select(CashSubmission).where(CashSubmission.status == "queued")).scalars().first():
        svc.set_setting(db, "analysis_requested_at", None)  # demande manuelle satisfaite
    res = svc.run_checks(db)
    return {"ran_at": res["ran_at"], "notified_critical": res.get("notified_critical"), "digest": res.get("digest")}


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
