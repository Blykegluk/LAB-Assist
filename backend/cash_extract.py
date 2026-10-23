"""Extraction des documents de caisse (relevés, bordereaux, tickets GAB) par modèle vision.

Fournisseur (CASH_VISION_PROVIDER) :
  anthropic (défaut) : SDK Anthropic, modèle CASH_VISION_MODEL (défaut claude-opus-5-5).
                       Identifiants : ANTHROPIC_API_KEY, ou un profil `ant auth login` sur le serveur.
  openai             : même mécanisme que le module CNI existant (OPENAI_API_KEY, gpt-4o).

Mode simulé pour les tests : avec CASH_MOCK_EXTRACTION=1, un fichier JSON envoyé à la place
d'une photo est renvoyé tel quel comme résultat d'extraction.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import pathlib
import re
from typing import List, Optional

log = logging.getLogger("cash.extract")

PROMPT_PATH = pathlib.Path(__file__).resolve().parent.parent / "prompts" / "cash_document.prompt.md"
USER_TEXT = "Identifie le type de ce document de caisse et extrais ses champs. Réponds uniquement par le JSON demandé."
ANTHROPIC_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def _b64(data: bytes) -> str:
    return base64.standard_b64encode(data).decode("utf-8")


def _pdf_to_pngs(pdf_bytes: bytes, max_pages: int = 3) -> List[bytes]:
    import fitz  # PyMuPDF

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    out: List[bytes] = []
    try:
        for i in range(min(len(doc), max_pages)):
            pix = doc[i].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            out.append(pix.tobytes("png"))
    finally:
        doc.close()
    return out


def _normalize_image(data: bytes, mime: str, max_side: int = 2000) -> tuple[bytes, str]:
    """Réduit les photos de smartphone (souvent 4000px) et convertit les formats exotiques (HEIC…) en JPEG."""
    try:
        import fitz

        pix = fitz.Pixmap(data)
        w, h = pix.width, pix.height
        if max(w, h) <= max_side and mime in ANTHROPIC_IMAGE_TYPES:
            return data, mime
        doc = fitz.open(stream=data, filetype="image")
        page = doc[0]
        # La page est exprimée en points (dépend du DPI de l'image) : on calcule l'échelle sur ses dimensions réelles
        native = w / float(page.rect.width)  # échelle qui rend l'image à sa taille native en pixels
        scale = native * min(1.0, max_side / float(max(w, h)))
        new_pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        out = new_pix.tobytes("jpeg")
        doc.close()
        return out, "image/jpeg"
    except Exception as e:  # pragma: no cover - dépend du format
        log.debug("normalisation ignorée: %s", e)
        return data, (mime if mime in ANTHROPIC_IMAGE_TYPES else "image/jpeg")


def _parse_json(raw: str) -> dict:
    txt = (raw or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", txt, re.S)
    if m:
        txt = m.group(1)
    try:
        obj = json.loads(txt)
    except json.JSONDecodeError:
        start, end = txt.find("{"), txt.rfind("}")
        if start >= 0 and end > start:
            try:
                obj = json.loads(txt[start : end + 1])
            except json.JSONDecodeError:
                obj = None
        else:
            obj = None
    if not isinstance(obj, dict):
        return {"type": "autre", "confidence": 0, "warnings": ["réponse non JSON"], "raw": raw}
    return obj


class CashDocExtractor:
    def __init__(self, provider: Optional[str] = None, model: Optional[str] = None) -> None:
        self.provider = (provider or os.getenv("CASH_VISION_PROVIDER") or "anthropic").lower()
        default_model = "claude-opus-5-5" if self.provider == "anthropic" else "gpt-4o"
        self.model = model or os.getenv("CASH_VISION_MODEL") or default_model
        with open(PROMPT_PATH, "r", encoding="utf-8") as f:
            self.system_prompt = f.read()
        self._client = None

    @property
    def mock(self) -> bool:
        return os.getenv("CASH_MOCK_EXTRACTION", "") in ("1", "true", "yes")

    # ------------------------------------------------------------------ Anthropic
    def _anthropic_client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def _anthropic_blocks(self, data: bytes, mime: str) -> List[dict]:
        if mime == "application/pdf":
            return [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": _b64(data)}}]
        data, mime = _normalize_image(data, mime or "image/jpeg")
        return [{"type": "image", "source": {"type": "base64", "media_type": mime, "data": _b64(data)}}]

    def _extract_anthropic(self, data: bytes, mime: str) -> dict:
        import anthropic

        client = self._anthropic_client()
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=8000,
                system=[{"type": "text", "text": self.system_prompt, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": [*self._anthropic_blocks(data, mime), {"type": "text", "text": USER_TEXT}]}],
            )
        except anthropic.AuthenticationError:
            raise RuntimeError("Clé API Anthropic absente ou invalide (ANTHROPIC_API_KEY)")
        except anthropic.RateLimitError:
            raise RuntimeError("Limite de requêtes Anthropic atteinte, réessaie dans une minute")
        except anthropic.APIStatusError as e:
            raise RuntimeError(f"Erreur API Anthropic ({e.status_code}) : {e.message}")
        except anthropic.APIConnectionError:
            raise RuntimeError("Impossible de joindre l'API Anthropic")
        if response.stop_reason == "refusal":
            return {"type": "autre", "confidence": 0, "warnings": ["Le modèle a refusé d'analyser ce document."]}
        text = "".join(b.text for b in response.content if b.type == "text")
        obj = _parse_json(text)
        if response.stop_reason == "max_tokens":
            obj.setdefault("warnings", []).append("réponse tronquée")
        return obj

    # ------------------------------------------------------------------ OpenAI (compatibilité)
    def _openai_client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI()
        return self._client

    def _extract_openai(self, data: bytes, mime: str) -> dict:
        def data_url(b: bytes, m: str) -> str:
            return f"data:{m};base64," + _b64(b)

        if mime == "application/pdf":
            parts = [{"type": "image_url", "image_url": {"url": data_url(p, "image/png"), "detail": "high"}} for p in _pdf_to_pngs(data)]
        else:
            data, mime = _normalize_image(data, mime or "image/jpeg")
            parts = [{"type": "image_url", "image_url": {"url": data_url(data, mime), "detail": "high"}}]
        completion = self._openai_client().chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": self.system_prompt}, {"role": "user", "content": [{"type": "text", "text": USER_TEXT}, *parts]}],
            temperature=0.0,
        )
        return _parse_json(completion.choices[0].message.content or "{}")

    # ------------------------------------------------------------------ point d'entrée
    def extract(self, data: bytes, mime: str, filename: str = "") -> dict:
        if self.mock and (mime in ("application/json", "text/json") or filename.lower().endswith(".json")):
            try:
                return json.loads(data.decode("utf-8"))
            except Exception as e:
                return {"type": "autre", "confidence": 0, "warnings": [f"JSON simulé invalide: {e}"]}
        if self.mock:
            return {"type": "autre", "confidence": 0, "warnings": ["mode simulé: fichier non JSON"]}
        if self.provider == "openai":
            return self._extract_openai(data, mime)
        return self._extract_anthropic(data, mime)


_EXTRACTOR: Optional[CashDocExtractor] = None


def get_extractor() -> CashDocExtractor:
    global _EXTRACTOR
    if _EXTRACTOR is None:
        _EXTRACTOR = CashDocExtractor()
    return _EXTRACTOR
