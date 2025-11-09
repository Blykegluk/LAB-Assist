from datetime import datetime
from sqlalchemy import String, Integer, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class Contract(Base):
    __tablename__ = "contracts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    store: Mapped[str] = mapped_column(String(20), index=True)

    prenom: Mapped[str] = mapped_column(String(120))
    nom: Mapped[str] = mapped_column(String(120))
    date_naissance: Mapped[str] = mapped_column(String(20))
    lieu_naissance: Mapped[str] = mapped_column(String(200))
    adresse: Mapped[str] = mapped_column(String(300))
    nationalite: Mapped[str] = mapped_column(String(80))
    numero_secu: Mapped[str] = mapped_column(String(32))
    date_debut: Mapped[str] = mapped_column(String(20))

    status: Mapped[str] = mapped_column(String(40), default="created", index=True)
    generated_doc_path: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

