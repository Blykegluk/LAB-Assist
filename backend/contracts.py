from __future__ import annotations

import json
from io import StringIO
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .database import get_db
from .models import Contract
from .schemas import ContractCreate, ContractRead, ContractsListResponse
from .pdf import generate_contract_pdf, ensure_generated_dir


# Création des tables déplacée dans l'événement startup de l'application


router = APIRouter(prefix="/contracts", tags=["contracts"])


@router.post("", response_model=ContractRead)
def create_contract(payload: ContractCreate, db: Session = Depends(get_db)):
    c = Contract(
        store=payload.store,
        prenom=payload.prenom,
        nom=payload.nom,
        date_naissance=payload.date_naissance,
        lieu_naissance=payload.lieu_naissance,
        adresse=payload.adresse,
        nationalite=payload.nationalite,
        numero_secu=payload.numero_secu,
        date_debut=payload.date_debut,
        status="created",
    )
    db.add(c)
    db.commit()
    db.refresh(c)

    # Generate PDF and update record
    data = {
        "id": c.id,
        "store": c.store,
        "prenom": c.prenom,
        "nom": c.nom,
        "date_naissance": c.date_naissance,
        "lieu_naissance": c.lieu_naissance,
        "adresse": c.adresse,
        "nationalite": c.nationalite,
        "numero_secu": c.numero_secu,
        "date_debut": c.date_debut,
    }
    try:
        pdf_path = generate_contract_pdf(data)
        c.generated_doc_path = pdf_path
        c.status = "generated"
        db.add(c)
        db.commit()
        db.refresh(c)
    except Exception as e:
        # Remonte une erreur claire au client (JSON) et n'expose pas de fallback
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=str(e))

    # Expose a URL path for the client
    from pathlib import Path
    gen_dir = ensure_generated_dir()
    try:
        rel = Path(pdf_path).resolve().relative_to(gen_dir.resolve())
        url = f"/files/{rel.as_posix()}"
    except Exception:
        url = None

    return ContractRead(
        id=c.id,
        store=c.store,
        prenom=c.prenom,
        nom=c.nom,
        date_naissance=c.date_naissance,
        lieu_naissance=c.lieu_naissance,
        adresse=c.adresse,
        nationalite=c.nationalite,
        numero_secu=c.numero_secu,
        date_debut=c.date_debut,
        status=c.status,
        generated_doc_url=url,
        created_at=c.created_at,
    )


@router.get("", response_model=ContractsListResponse)
def list_contracts(
    db: Session = Depends(get_db),
    store: Optional[str] = Query(None),
    q: Optional[str] = Query(None, description="Recherche texte simple"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    stmt = select(Contract)
    if store:
        stmt = stmt.where(Contract.store == store)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            (Contract.prenom.ilike(like))
            | (Contract.nom.ilike(like))
            | (Contract.numero_secu.ilike(like))
            | (Contract.adresse.ilike(like))
        )
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.execute(stmt.order_by(Contract.created_at.desc()).limit(limit).offset(offset)).scalars().all()

    items = []
    for c in rows:
        url = None
        if c.generated_doc_path:
            from pathlib import Path
            gen_dir = ensure_generated_dir()
            try:
                rel = Path(c.generated_doc_path).resolve().relative_to(gen_dir.resolve())
                url = f"/files/{rel.as_posix()}"
            except Exception:
                url = None
        items.append(
            ContractRead(
                id=c.id,
                store=c.store,
                prenom=c.prenom,
                nom=c.nom,
                date_naissance=c.date_naissance,
                lieu_naissance=c.lieu_naissance,
                adresse=c.adresse,
                nationalite=c.nationalite,
                numero_secu=c.numero_secu,
                date_debut=c.date_debut,
                status=c.status,
                generated_doc_url=url,
                created_at=c.created_at,
            )
        )
    return ContractsListResponse(items=items, total=total or 0)


@router.get("/export.csv")
def export_csv(db: Session = Depends(get_db)):
    rows = db.execute(select(Contract).order_by(Contract.created_at.desc())).scalars().all()
    out = StringIO()
    headers = [
        "id",
        "store",
        "prenom",
        "nom",
        "date_naissance",
        "lieu_naissance",
        "adresse",
        "nationalite",
        "numero_secu",
        "date_debut",
        "status",
        "generated_doc_path",
        "created_at",
    ]
    out.write(",".join(headers) + "\n")
    for c in rows:
        vals = [
            str(c.id),
            c.store or "",
            c.prenom or "",
            c.nom or "",
            c.date_naissance or "",
            c.lieu_naissance or "",
            (c.adresse or "").replace("\n", " "),
            c.nationalite or "",
            c.numero_secu or "",
            c.date_debut or "",
            c.status or "",
            c.generated_doc_path or "",
            c.created_at.isoformat() if c.created_at else "",
        ]
        out.write(",".join([f'"{v.replace("\"", "\"\"")}"' for v in vals]) + "\n")
    out.seek(0)
    return StreamingResponse(out, media_type="text/csv", headers={"Content-Disposition": "attachment; filename=contracts.csv"})


@router.get("/{contract_id}", response_model=ContractRead)
def get_contract(contract_id: int, db: Session = Depends(get_db)):
    c = db.get(Contract, contract_id)
    if not c:
        raise HTTPException(status_code=404, detail="Contrat introuvable")
    url = None
    if c.generated_doc_path:
        from pathlib import Path
        gen_dir = ensure_generated_dir()
        try:
            rel = Path(c.generated_doc_path).resolve().relative_to(gen_dir.resolve())
            url = f"/files/{rel.as_posix()}"
        except Exception:
            url = None
    return ContractRead(
        id=c.id,
        store=c.store,
        prenom=c.prenom,
        nom=c.nom,
        date_naissance=c.date_naissance,
        lieu_naissance=c.lieu_naissance,
        adresse=c.adresse,
        nationalite=c.nationalite,
        numero_secu=c.numero_secu,
        date_debut=c.date_debut,
        status=c.status,
        generated_doc_url=url,
        created_at=c.created_at,
    )


