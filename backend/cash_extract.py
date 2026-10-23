"""Extraction des documents de caisse (relevés, bordereaux, tickets GAB) par modèle vision.

Mode simulé pour les tests : avec CASH_MOCK_EXTRACTION=1, un fichier JSON envoyé à la place
d'une photo est renvoyé tel quel comme résultat d'extraction.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
from typing import List, Optional

log = logging.getLogger("cash.extract")

PROMPT_PATH = pathlib.Path(__file__).resolve().parent.parent / "prompts" / "cash_document.prompt.md"


def _b64_data_url(data: bytes, mime: str) -> str:
    import base64

    return f"data:{mime};base64," + base64.b64encode(data).decode("utf-8")


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


def _downscale_image(data: bytes, mime: str, max_side: int = 2000) -> tuple[bytes, str]:
    """Réduit les photos de smartphone (souvent 4000px) pour limiter le coût sans perdre la lisibilité."""
    try:
        import fitz

        pix = fitz.Pixmap(data)
        w, h = pix.width, pix.height
        if max(w, h) <= max_side:
            return data, mime
        scale = max_side / float(max(w, h))
        # Conversion via un document image PyMuPDF pour redimensionner proprement
        doc = fitz.open(stream=data, filetype="image")
        page = doc[0]
        new_pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        out = new_pix.tobytes("jpeg")
        doc.close()
        return out, "image/jpeg"
    except Exception as e:  # pragma: no cover - dépend du format
        log.debug("downscale ignoré: %s", e)
        return data, mime


class CashDocExtractor:
    def __init__(self, model: Optional[str] = None) -> None:
        self.model = model or os.getenv("CASH_VISION_MODEL") or "gpt-4o"
        with open(PROMPT_PATH, "r", encoding="utf-8") as f:
            self.system_prompt = f.read()
        self._client = None

    @property
    def mock(self) -> bool:
        return os.getenv("CASH_MOCK_EXTRACTION", "") in ("1", "true", "yes")

    def _client_or_raise(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI()
        return self._client

    def _image_parts(self, data: bytes, mime: str) -> List[dict]:
        if mime == "application/pdf":
            return [
                {"type": "image_url", "image_url": {"url": _b64_data_url(p, "image/png"), "detail": "high"}}
                for p in _pdf_to_pngs(data)
            ]
        if not mime or not mime.startswith("image/"):
            mime = "image/jpeg"
        data, mime = _downscale_image(data, mime)
        return [{"type": "image_url", "image_url": {"url": _b64_data_url(data, mime), "detail": "high"}}]

    def extract(self, data: bytes, mime: str, filename: str = "") -> dict:
        if self.mock and (mime in ("application/json", "text/json") or filename.lower().endswith(".json")):
            try:
                return json.loads(data.decode("utf-8"))
            except Exception as e:
                return {"type": "autre", "confidence": 0, "warnings": [f"JSON simulé invalide: {e}"]}
        if self.mock:
            return {"type": "autre", "confidence": 0, "warnings": ["mode simulé: fichier non JSON"]}

        client = self._client_or_raise()
        content = [
            {
                "type": "text",
                "text": "Identifie le type de ce document de caisse et extrais ses champs. Réponds en JSON strict.",
            },
            *self._image_parts(data, mime),
        ]
        completion = client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": content},
            ],
            temperature=0.0,
        )
        raw = completion.choices[0].message.content or "{}"
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            obj = {"type": "autre", "confidence": 0, "warnings": ["réponse non JSON"], "raw": raw}
        if not isinstance(obj, dict):
            obj = {"type": "autre", "confidence": 0, "warnings": ["réponse inattendue"]}
        return obj


_EXTRACTOR: Optional[CashDocExtractor] = None


def get_extractor() -> CashDocExtractor:
    global _EXTRACTOR
    if _EXTRACTOR is None:
        _EXTRACTOR = CashDocExtractor()
    return _EXTRACTOR
