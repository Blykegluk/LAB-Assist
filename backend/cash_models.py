"""Tables SQL du module dépôts d'espèces."""
from __future__ import annotations

from datetime import datetime, date
from typing import Optional

from sqlalchemy import String, Integer, Float, DateTime, Date, Text, Boolean, ForeignKey, LargeBinary
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class CashDocumentBlob(Base):
    """Contenu des photos gardé en base : le disque des hébergeurs gratuits (Render) s'efface au redémarrage."""

    __tablename__ = "cash_document_blobs"

    document_id: Mapped[int] = mapped_column(Integer, ForeignKey("cash_documents.id"), primary_key=True)
    data: Mapped[bytes] = mapped_column(LargeBinary)


class CashSubmission(Base):
    """Un envoi de la responsable : un lot de photos pour un magasin, analysé puis confirmé."""

    __tablename__ = "cash_submissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[str] = mapped_column(String(40), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending | confirmed | cancelled
    submitted_by: Mapped[str] = mapped_column(String(40), default="manager")
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Résultat du rapprochement (figé à la confirmation)
    expected_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    deposit_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ecart: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    analysis_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class CashDocument(Base):
    __tablename__ = "cash_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    submission_id: Mapped[int] = mapped_column(Integer, ForeignKey("cash_submissions.id"), index=True)
    store_id: Mapped[str] = mapped_column(String(40), index=True)
    kind: Mapped[str] = mapped_column(String(30), default="autre", index=True)
    filename: Mapped[str] = mapped_column(String(300))
    path: Mapped[str] = mapped_column(String(600))
    remote_path: Mapped[Optional[str]] = mapped_column(String(600), nullable=True)  # objet Supabase Storage (copie durable)
    mime: Mapped[str] = mapped_column(String(80), default="image/jpeg")
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    extracted_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CashReport(Base):
    """Un relevé de caisse (storeMate ou Orika) couvrant une période."""

    __tablename__ = "cash_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    submission_id: Mapped[int] = mapped_column(Integer, ForeignKey("cash_submissions.id"), index=True)
    document_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    store_id: Mapped[str] = mapped_column(String(40), index=True)
    pos_system: Mapped[str] = mapped_column(String(20), default="storemate")
    period_start: Mapped[date] = mapped_column(Date, index=True)
    period_end: Mapped[date] = mapped_column(Date, index=True)
    cash_amount: Mapped[float] = mapped_column(Float, default=0.0)
    total_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    operators_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CashDeposit(Base):
    """Un dépôt en banque (bordereau + ticket GAB)."""

    __tablename__ = "cash_deposits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    submission_id: Mapped[int] = mapped_column(Integer, ForeignKey("cash_submissions.id"), index=True)
    store_id: Mapped[str] = mapped_column(String(40), index=True)
    deposit_date: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[float] = mapped_column(Float)
    bag_number: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, index=True)
    slip_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    slip_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    receipt_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    receipt_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    operation_number: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    account_holder: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    breakdown_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bank_status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending | matched | missing | unchecked
    bank_transaction_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    bank_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    bank_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CashExpense(Base):
    """Facture ou dépense payée en espèces (sort du coffre)."""

    __tablename__ = "cash_expenses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    submission_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    store_id: Mapped[str] = mapped_column(String(40), index=True)
    expense_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    amount: Mapped[float] = mapped_column(Float)
    label: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CashAlert(Base):
    __tablename__ = "cash_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[str] = mapped_column(String(40), index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    severity: Mapped[str] = mapped_column(String(10), default="warning")  # critical | warning | info
    title: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    submission_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ref_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="open", index=True)  # open | acked | resolved
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class CashSheetSnapshot(Base):
    """Dernière lecture du Google Sheet 'Comptage coffre' d'un magasin."""

    __tablename__ = "cash_sheet_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[str] = mapped_column(String(40), index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    tab_title: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    data_json: Mapped[str] = mapped_column(Text)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class CashSetting(Base):
    __tablename__ = "cash_settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
