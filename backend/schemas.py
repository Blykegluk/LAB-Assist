from datetime import datetime
from typing import Optional, Literal

from pydantic import BaseModel, Field


class ContractBase(BaseModel):
    store: Literal["AEJB", "JAB"] = Field(description="Magasin concerné")
    prenom: str
    nom: str
    date_naissance: str
    lieu_naissance: str
    adresse: str
    nationalite: str
    numero_secu: str
    date_debut: str


class ContractCreate(ContractBase):
    pass


class ContractRead(ContractBase):
    id: int
    status: str
    generated_doc_url: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class ContractsListResponse(BaseModel):
    items: list[ContractRead]
    total: int


# --- Recrutement ---

class CriteriaInput(BaseModel):
    key: str
    label: str
    coefficient: int  # 0..3


class FreeCriteriaInput(BaseModel):
    label: str
    coefficient: int  # 0..3


class AnalyzeCVRequest(BaseModel):
    role: str
    criteria: list[CriteriaInput]
    free_criteria: list[FreeCriteriaInput]


class CandidateOutput(BaseModel):
    nom: Optional[str] = None
    prenom: Optional[str] = None
    telephone: Optional[str] = None
    poste: Optional[str] = None
    score: float
    date_cv: Optional[str] = None
    commentaire: Optional[str] = None


class AnalyzeCVResponse(BaseModel):
    role: str
    candidates: list[CandidateOutput]
