from __future__ import annotations

import json
from typing import List, Optional

from fastapi import APIRouter, UploadFile, File, Form, HTTPException

from .cv import CVAnalyzer


router = APIRouter(prefix="/recruitment", tags=["recruitment"])


def _parse_criteria(criteria_json: Optional[str]) -> dict:
    try:
        obj = json.loads(criteria_json or "{}")
        # expect: { key: { label, coefficient }, ... }
        # flatten to { key: coefficient }
        out = {}
        for k, v in (obj.items() if isinstance(obj, dict) else []):
            try:
                coef = int(v.get("coefficient", 0))
            except Exception:
                coef = 0
            if coef < 0:
                coef = 0
            if coef > 3:
                coef = 3
            out[k] = coef
        return out
    except Exception:
        return {}


@router.post("/analyze")
async def analyze(
    role: str = Form(...),
    criteria: str = Form("{}"),
    files: List[UploadFile] = File(...),
):
    analyzer = CVAnalyzer(system_prompt_path="prompts/cv_analyzer.prompt.md")

    # Build criteria payload
    criteria_payload = _parse_criteria(criteria)

    # Read files
    file_entries = []
    for uf in files:
        try:
            content = await uf.read()
            if not content:
                continue
            file_entries.append({
                "filename": uf.filename,
                "content": content,
                "mime": uf.content_type or "",
            })
        except Exception:
            continue
    if not file_entries:
        raise HTTPException(status_code=400, detail="Aucun fichier valide reçu")

    try:
        result = analyzer.analyze(role=role, criteria_payload=criteria_payload, files=file_entries)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return result



