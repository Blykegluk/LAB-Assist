from __future__ import annotations

import json
from typing import List

import fitz  # PyMuPDF
from docx import Document
from openai import OpenAI


def _pdf_to_png_bytes_list(pdf_bytes: bytes, max_pages: int = 2) -> List[bytes]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    images: List[bytes] = []
    try:
        page_count = len(doc)
        for i in range(min(page_count, max_pages)):
            page = doc[i]
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            images.append(pix.tobytes("png"))
    finally:
        doc.close()
    return images


def _to_data_url(image_bytes: bytes, mime: str) -> str:
    import base64
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def _docx_to_text(docx_bytes: bytes) -> str:
    from io import BytesIO
    f = BytesIO(docx_bytes)
    doc = Document(f)
    parts = []
    for p in doc.paragraphs:
        txt = p.text.strip()
        if txt:
            parts.append(txt)
    return "\n".join(parts)


class CVAnalyzer:
    def __init__(self, system_prompt_path: str, model: str = "gpt-4o-mini") -> None:
        with open(system_prompt_path, "r", encoding="utf-8") as f:
            self.system_prompt = f.read()
        self.client = OpenAI()
        self.model = model

    def build_messages(self, role: str, criteria_payload: dict, files: List[dict]) -> list:
        # files: list of {filename, content: bytes, mime: str}
        user_content: List[dict] = [
            {"type": "text", "text": json.dumps({"role": role, "criteres": criteria_payload}, ensure_ascii=False)}
        ]
        for f in files:
            mime = (f.get("mime") or "").lower()
            name = f.get("filename") or "fichier"
            if mime == "application/pdf":
                for b in _pdf_to_png_bytes_list(f["content"], max_pages=2):
                    user_content.append({"type": "image_url", "image_url": {"url": _to_data_url(b, "image/png")}})
            elif mime.startswith("image/"):
                user_content.append({"type": "image_url", "image_url": {"url": _to_data_url(f["content"], mime)}})
            elif mime in ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/msword"):
                # Extract text
                try:
                    txt = _docx_to_text(f["content"]) or ""
                    if txt.strip():
                        user_content.append({"type": "text", "text": f"[DOCX:{name}]\n" + txt[:8000]})
                except Exception:
                    pass
            else:
                # Unknown -> try include as base64 text marker
                user_content.append({"type": "text", "text": f"[FICHIER:{name}]"})

        return [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_content},
        ]

    def analyze(self, role: str, criteria_payload: dict, files: List[dict]) -> dict:
        messages = self.build_messages(role, criteria_payload, files)
        completion = self.client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=messages,
            temperature=0.0,
        )
        content = completion.choices[0].message.content
        try:
            return json.loads(content)
        except Exception:
            return {"raw": content}



