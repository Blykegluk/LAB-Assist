import base64
import json
from typing import List

import fitz  # PyMuPDF
from openai import OpenAI


class IDCardExtractor:
    """
    Extracteur de champs Ã  partir d'un document (PDF/PNG/JPG) via un modÃ¨le vision.
    """

    def __init__(self, prompt_path: str, model: str = "gpt-4o-mini") -> None:
        with open(prompt_path, "r", encoding="utf-8") as f:
            self.system_prompt = f.read()
        # Le client lira OPENAI_API_KEY dans les variables d'environnement
        self.client = OpenAI()
        self.model = model

    @staticmethod
    def _to_data_url(image_bytes: bytes, mime: str) -> str:
        import base64
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        return f"data:{mime};base64,{b64}"

    @staticmethod
    def _pdf_to_png_bytes_list(pdf_bytes: bytes, max_pages: int = 2) -> List[bytes]:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        images: List[bytes] = []
        try:
            page_count = len(doc)
            for i in range(min(page_count, max_pages)):
                page = doc[i]
                # Rendu 2x pour une meilleure lisibilitÃ©
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                images.append(pix.tobytes("png"))
        finally:
            doc.close()
        return images

    def _file_to_image_contents(self, file_bytes: bytes, mime: str) -> List[dict]:
        if mime == "application/pdf":
            pngs = self._pdf_to_png_bytes_list(file_bytes)
            return [
                {"type": "image_url", "image_url": {"url": self._to_data_url(b, "image/png")}}
                for b in pngs
            ]
        # Si pas d'image reconnue, fallback JPEG
        if not mime or not mime.startswith("image/"):
            mime = "image/jpeg"
        return [{"type": "image_url", "image_url": {"url": self._to_data_url(file_bytes, mime)}}]

    def extract(self, file_bytes: bytes, mime: str, system_prompt: str | None = None) -> dict:
        user_content = [
            {"type": "text", "text": "Extrait les champs demandés et réponds en JSON strict."},
            *self._file_to_image_contents(file_bytes, mime),
        ]

        completion = self.client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt or self.system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=0.0,
        )

        content = completion.choices[0].message.content
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return {"raw": content}
