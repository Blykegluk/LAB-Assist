from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm, cm
from reportlab.pdfgen import canvas
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.colors import black
import re

MONTHS_IN_YEAR = 12

def _add_months(dt, months):
    year = dt.year + (dt.month - 1 + months) // MONTHS_IN_YEAR
    month = (dt.month - 1 + months) % MONTHS_IN_YEAR + 1
    day = min(dt.day, [31, 29 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31, 31][month-1])
    from datetime import datetime as _dt
    return _dt(year, month, day)


def _load_config() -> dict:
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"pdf": {"title": "Contrat", "footer_text": ""}, "generated_dir": "generated"}


def ensure_generated_dir() -> Path:
    cfg = _load_config()
    gen = Path(cfg.get("generated_dir", "generated"))
    gen.mkdir(parents=True, exist_ok=True)
    return gen


def _load_template_path(store: str | None) -> Path | None:
    cfg = _load_config()
    templates = cfg.get("templates", {}) or {}
    if store and store in templates:
        p = Path(templates[store])
        if p.exists():
            return p
    # fallback: no template
    return None


def _load_pdf_template_path(store: str | None) -> Path | None:
    # Supprimé: non utilisé (pipeline 100% templates .txt)
    return None


def _load_docx_template_path(store: str | None) -> Path | None:
    # Supprimé: non utilisé (pipeline 100% templates .txt)
    return None


def _load_txt_template_path(store: str | None) -> Path | None:
    """Charge le chemin du template texte (.txt) - UNIQUEMENT les fichiers .txt"""
    if not store:
        return None
    
    # Chemin du dossier templates depuis la racine du projet
    base_dir = Path(__file__).parent.parent
    templates_dir = base_dir / "templates"
    
    # Chercher directement le fichier {STORE}_CDI_VENDEUR.txt
    template_file = templates_dir / f"{store}_CDI_VENDEUR.txt"
    
    if template_file.exists() and template_file.is_file():
        return template_file
    
    # Fallback: chercher dans config.json
    cfg = _load_config()
    templates = cfg.get("templates", {}) or {}
    if store in templates:
        template_path = templates[store]
        p = Path(template_path)
        if not p.is_absolute():
            p = base_dir / p
        if p.exists() and p.suffix.lower() == '.txt':
            return p
    
    return None


def _generate_pdf_from_text_template(template_path: Path, variables: dict, out_path: Path) -> str:
    """Génère un PDF à partir d'un template texte avec formatage professionnel"""
    # Lire le template
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            template_text = f.read()
    except Exception:
        with open(template_path, "r", encoding="latin-1") as f:
            template_text = f.read()
    
    # Remplacer les balises
    for token, value in variables.items():
        template_text = template_text.replace(token, str(value))
    
    # Créer les styles ReportLab
    styles = getSampleStyleSheet()
    
    # Style pour le titre principal (centré, gras)
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Normal'],
        fontSize=16,
        textColor=black,
        spaceAfter=36,
        alignment=TA_CENTER,
        fontName='Helvetica-Bold',
        leading=20
    )
    
    # Style pour les titres d'articles (gras et souligné)
    article_title_style = ParagraphStyle(
        'ArticleTitle',
        parent=styles['Normal'],
        fontSize=11,
        textColor=black,
        spaceBefore=16,
        spaceAfter=12,
        fontName='Helvetica-Bold',
        leading=16
    )
    
    # Style pour le texte normal
    normal_style = ParagraphStyle(
        'Normal',
        parent=styles['Normal'],
        fontSize=10,
        textColor=black,
        spaceAfter=0,
        alignment=TA_JUSTIFY,
        leading=16
    )
    # Variante sans espace après (avant "d'une part,")
    normal_tight_style = ParagraphStyle(
        'NormalTight',
        parent=normal_style,
        spaceAfter=0
    )
    # Style pour marqueurs d'intro (pas d'espace avant, espace après)
    marker_style = ParagraphStyle(
        'Marker',
        parent=normal_style,
        spaceBefore=0,
        spaceAfter=0
    )
    
    # Créer le document PDF
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        rightMargin=2.5*cm,
        leftMargin=2.5*cm,
        topMargin=2.5*cm,
        bottomMargin=2.5*cm
    )
    
    # Contenu du document
    story = []
    
    # Parser le texte ligne par ligne
    lines = template_text.split('\n')
    title_done = False
    
    for i, line in enumerate(lines):
        line_stripped = line.strip()
        
        # Calculer la prochaine ligne non vide
        j = i + 1
        next_non_empty = ""
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j < len(lines):
            next_non_empty = lines[j].strip().lower()
        
        # Calculer la précédente ligne non vide
        k = i - 1
        prev_non_empty = ""
        while k >= 0 and not lines[k].strip():
            k -= 1
        if k >= 0:
            prev_non_empty = lines[k].strip().lower()
        
        # Normalisation flags
        next_is_dune_part = ("d'une part" in next_non_empty) or ("d’une part" in next_non_empty)
        this_is_dune_part = (line_stripped.lower() == "d'une part,") or (line_stripped.lower() == "d’une part,")
        
        # Lignes vides -> saut de ligne (sauf si la prochaine ligne est "d'une part,")
        if not line_stripped:
            if next_is_dune_part:
                continue
            story.append(Spacer(1, 10))
            continue
        
        # Détecter le titre principal (première ligne non vide en majuscules, longue)
        if not title_done and line_stripped.isupper() and len(line_stripped) > 30:
            # Échapper HTML et mettre en gras
            escaped = line_stripped.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            story.append(Paragraph(f"<b>{escaped}</b>", title_style))
            story.append(Spacer(1, 28))
            title_done = True
            continue
        
        # Détecter les titres d'articles (ARTICLE X – ... ou ARTICLE X - ...)
        article_match = re.match(r'^ARTICLE\s+(\d+)\s*[–\-—]\s*(.+)$', line_stripped, re.IGNORECASE)
        if article_match:
            article_num = article_match.group(1)
            article_title = article_match.group(2).strip()
            # Échapper HTML
            article_title_escaped = article_title.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            # Gras et souligné
            story.append(Paragraph(f"<b><u>ARTICLE {article_num} – {article_title_escaped}</u></b>", article_title_style))
            continue
        
        # Marqueurs d'intro spécifiques
        low = line_stripped.lower()
        if this_is_dune_part:
            # Supprimer tout Spacer résiduel juste avant le marqueur
            if story and isinstance(story[-1], Spacer):
                story.pop()
            escaped = line_stripped.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            story.append(Paragraph(escaped, marker_style))
            story.append(Spacer(1, normal_style.leading))
            story.append(Spacer(1, normal_style.leading))
            continue
        if low == 'et,' or low.startswith('et,'):
            if story and isinstance(story[-1], Spacer):
                story.pop()
            escaped = line_stripped.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            story.append(Paragraph(escaped, marker_style))
            story.append(Spacer(1, normal_style.leading))
            story.append(Spacer(1, normal_style.leading))
            continue
        if low.startswith("d'autre part") or low.startswith("d’autre part"):
            if story and isinstance(story[-1], Spacer):
                story.pop()
            escaped = line_stripped.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            story.append(Paragraph(escaped, marker_style))
            story.append(Spacer(1, normal_style.leading))
            story.append(Spacer(1, normal_style.leading))
            continue
        
        # Détecter les puces (commencent par · ou -)
        if line_stripped.startswith('·') or (line_stripped.startswith('-') and len(line_stripped) > 2):
            escaped = line_stripped.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            story.append(Paragraph(f"&nbsp;&nbsp;&nbsp;&nbsp;{escaped}", normal_style))
            continue
        
        # Texte normal (avec style resserré si la prochaine ligne est "d'une part,")
        escaped = line_stripped.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        use_tight = next_is_dune_part
        story.append(Paragraph(escaped, normal_tight_style if use_tight else normal_style))
        
        # Ajout d'un petit espace supplémentaire après les lignes de type en-tête terminées par ':'
        if line_stripped.endswith(':'):
            story.append(Spacer(1, 6))
        
        # Saut de 2 lignes après la mention de signature du président (fin de document uniquement)
        if 'Monsieur Anthony BOUSKILA, Président' in line_stripped:
            if prev_non_empty == 'pour la société aejb,':
                story.append(Spacer(1, 16))
                story.append(Spacer(1, 16))
        
        # Règles spécifiques d'intro (supprimées car gérées par marker_style)
        # if "d'une part" in low:
        #     story.append(Spacer(1, 12))
        # if low == 'et,' or low.startswith('et,'):
        #     story.append(Spacer(1, 12))
        # if "d'autre part" in low:
        #     story.append(Spacer(1, 12))
    
    # Générer le PDF
    doc.build(story)
    return str(out_path)


def _format_fr_date(s: str) -> str:
    if not s:
        return s
    from datetime import datetime as _dt
    s = str(s).strip()
    fmts = ["%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%m/%d/%Y"]
    for f in fmts:
        try:
            dt = _dt.strptime(s, f)
            return dt.strftime("%d/%m/%Y")
        except Exception:
            pass
    # try loose parse
    try:
        from dateutil import parser  # optional
        return parser.parse(s, dayfirst=True).strftime("%d/%m/%Y")
    except Exception:
        return s


def generate_contract_pdf(contract: dict) -> str:
    cfg = _load_config()

    out_dir = ensure_generated_dir()
    filename = f"contrat_{contract['id']}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.pdf"
    out_path = out_dir / filename

    # Build variables mapping in template style
    date_debut_raw = contract.get("date_debut") or ""
    dd_str = _format_fr_date(date_debut_raw)
    try:
        from datetime import datetime as _dt
        base_dt = _dt.strptime(dd_str, "%d/%m/%Y")
        fin_dt = _add_months(base_dt, 2)
        fin_str = fin_dt.strftime("%d/%m/%Y")
    except Exception:
        fin_str = dd_str

    variables = {
        "{{Prénom}}": contract.get("prenom", ""),
        "{{Nom}}": contract.get("nom", ""),
        "{{Date_de_naissance}}": _format_fr_date(contract.get("date_naissance", "")),
        "{{Lieu de naissance}}": contract.get("lieu_naissance", ""),
        "{{Adresse}}": contract.get("adresse", ""),
        "{{Nationalité}}": contract.get("nationalite", ""),
        "{{Numéro de secu}}": contract.get("numero_secu", ""),
        "{{Date_debut}}": dd_str,
        "{{Date_fin_periode_essai}}": fin_str,
    }

    # UNIQUEMENT templates texte - AUCUN fallback PDF/DOCX
    store = contract.get("store")
    if not store:
        raise RuntimeError("Magasin non spécifié dans le contrat")
    
    txt_template = _load_txt_template_path(store)
    if not txt_template:
        raise RuntimeError(
            f"Template texte introuvable pour le magasin '{store}'. "
            f"Vérifiez que le fichier 'templates/{store}_CDI_VENDEUR.txt' existe."
        )
    
    try:
        return _generate_pdf_from_text_template(txt_template, variables, out_path)
    except Exception as e:
        raise RuntimeError(f"Erreur lors de la génération du PDF: {str(e)}") from e

    # Code obsolète supprimé (anciens fallbacks non utilisés)

