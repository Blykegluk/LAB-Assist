import os
import pathlib
from dotenv import load_dotenv

from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from backend.extractor import IDCardExtractor
from backend.contracts import router as contracts_router
from backend.pdf import ensure_generated_dir
from backend.database import Base, engine
from backend.recruitment import router as recruitment_router


app = FastAPI(title="ID Card Extractor", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

load_dotenv()
EXTRACTOR = IDCardExtractor(prompt_path=os.path.join("prompts", "id_card.prompt.md"))

def _load_prompt_for(doc_type: str) -> str:
    name = {
        "cni": "id_card.prompt.md",
        "domicile": "domicile.prompt.md",
        "secu": "secu.prompt.md",
    }.get(doc_type, "id_card.prompt.md")
    p = os.path.join("prompts", name)
    try:
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        with open(os.path.join("prompts", "id_card.prompt.md"), "r", encoding="utf-8") as f:
            return f.read()


def _normalize_fields(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return payload
    from datetime import datetime

    def to_dd_mm_yyyy(value: str):
        if not isinstance(value, str):
            return value
        raw = value.strip()
        if not raw:
            return raw
        date_part = raw.split("T")[0].split(" ")[0]
        fmts = [
            "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d",
            "%d.%m.%Y", "%Y.%m.%d", "%m/%d/%Y", "%d %m %Y",
            "%d %b %Y", "%d %B %Y", "%Y%m%d",
        ]
        for fmt in fmts:
            try:
                dt = datetime.strptime(date_part, fmt)
                return dt.strftime("%d/%m/%Y")
            except Exception:
                pass
        import re
        m = re.match(r"^\D*(\d{1,4})\D+(\d{1,2})\D+(\d{1,4})\D*$", date_part)
        if m:
            a, b, c = m.groups()
            if len(a) == 4:
                yyyy, mm, dd = a, b.zfill(2), c.zfill(2)
            elif len(c) == 4:
                dd, mm, yyyy = a.zfill(2), b.zfill(2), c
            else:
                return raw
            try:
                dt = datetime.strptime(f"{yyyy}-{mm}-{dd}", "%Y-%m-%d")
                return dt.strftime("%d/%m/%Y")
            except Exception:
                return raw
        return raw
    for key in ("nationalite", "nationalité", "nationality"):
        if key in payload and isinstance(payload[key], str):
            val_norm = payload[key].strip().upper()
            if val_norm in ("FRA", "FR"):
                payload[key] = "Française"
    # Dates → DD/MM/YYYY
    date_keys_groups = [
        ("date_naissance", "date de naissance", "birthdate", "dob"),
        ("date_debut", "date debut", "start_date"),
        ("date_expiration", "date expiration", "expiry", "expiration_date"),
    ]
    lower_to_key = {k.lower(): k for k in payload.keys()}
    for group in date_keys_groups:
        for k in group:
            lk = k.lower()
            if lk in lower_to_key:
                real_key = lower_to_key[lk]
                payload[real_key] = to_dd_mm_yyyy(payload[real_key])
                break
    return payload


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/extract")
async def extract(file: UploadFile = File(...), doc_type: str = Form("cni")):
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Fichier vide")

    mime = file.content_type or ""

    try:
        system_prompt = _load_prompt_for(doc_type)
        data = EXTRACTOR.extract(content, mime, system_prompt=system_prompt)
        data = _normalize_fields(data)
        return JSONResponse(content={"success": True, "data": data})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Mount des fichiers statiques en dernier pour ne pas intercepter les routes API
BASE_DIR = pathlib.Path(__file__).parent.parent
frontend_dir = BASE_DIR / "frontend"

@app.get("/")
async def read_root():
    """Serve index.html à la racine"""
    return FileResponse(str(frontend_dir / "index.html"))

# API Contracts
app.include_router(contracts_router)
app.include_router(recruitment_router)

# Serve generated files
generated_dir = ensure_generated_dir()
app.mount("/files", StaticFiles(directory=str(generated_dir)), name="files")

# Serve frontend static assets (images, CSS, JS)
assets_dir = frontend_dir / "assets"
app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")


@app.on_event("startup")
def on_startup() -> None:
    # Crée les tables si elles n'existent pas (au démarrage de l'app)
    Base.metadata.create_all(bind=engine)
