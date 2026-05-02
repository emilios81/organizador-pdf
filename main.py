"""
PDF Scholar — Renombrador de PDFs sin API key
Pipeline: metadata PDF → DOI/CrossRef → CrossRef(título) → OpenAlex →
          Semantic Scholar → ISBN/Open Library → OCR → heurísticas
"""

import os
import re
import json
import shutil
import threading
import urllib.request
import urllib.parse
import tkinter as tk
from tkinter import filedialog, messagebox

import PyPDF2
import customtkinter as ctk

# ── Miniaturas y OCR opcionales ────────────────────────────────────────────────
# Instalar con: pip install pymupdf pytesseract Pillow
# También requiere Tesseract OCR: https://github.com/UB-Mannheim/tesseract/wiki
try:
    import fitz                          # PyMuPDF
    from PIL import Image, ImageTk
    THUMB_AVAILABLE = True
except ImportError:
    fitz = Image = ImageTk = None
    THUMB_AVAILABLE = False

try:
    import pytesseract
    OCR_AVAILABLE = THUMB_AVAILABLE
except ImportError:
    OCR_AVAILABLE = False

# ── Configuración ──────────────────────────────────────────────────────────────
MAX_PDFS   = 50
PAGES_SCAN = 8       # máximo de páginas a analizar por archivo
UA         = "PDFScholar/3.0"

# Paleta
BG_ROOT   = "#0A0B0D"
BG_PANEL  = "#12141A"
BG_CARD   = "#1A1D25"
BORDER    = "#252830"
ACCENT    = "#00C9A7"
ACCENT_HV = "#00A88B"
TEXT_PRI  = "#DCE0EA"
TEXT_SEC  = "#5A6075"
TEXT_OK   = "#00C9A7"
TEXT_ERR  = "#FF5F6D"
TEXT_WARN = "#F0A500"

FONT_TITLE  = ("Segoe UI",  20, "bold")
FONT_LABEL  = ("Segoe UI",  12)
FONT_SMALL  = ("Segoe UI",  10)
FONT_MONO   = ("Consolas",  11)
FONT_MONO_S = ("Consolas",  10)
FONT_BADGE  = ("Consolas",   9, "bold")


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  MOTOR DE RENOMBRADO                                                        ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def _sanitize(name: str) -> str:
    """Elimina caracteres inválidos para nombres de archivo en Windows."""
    name = name.replace("\n", " ").replace("\r", "").replace("\t", " ")
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "")
    return " ".join(name.split()).strip()


# ── 1. Metadata embebida del PDF ───────────────────────────────────────────────

def _clean_metadata_title(title: str) -> str:
    """Limpia prefijos comunes que meten los programas al título."""
    # "Microsoft Word - Paper final.doc" → "Paper final.doc"
    title = re.sub(r'^Microsoft Word\s*-\s*', '', title, flags=re.IGNORECASE)
    title = re.sub(r'^Microsoft PowerPoint\s*-\s*', '', title, flags=re.IGNORECASE)
    # Rutas completas: "C:\Users\...\paper.pdf" → "paper.pdf"
    if re.match(r'^[A-Z]:[\\/]', title) or title.startswith("/"):
        title = os.path.basename(title.replace("\\", "/"))
    return title.strip()


def _metadata_looks_valid(title: str, author: str) -> bool:
    """
    Rechaza metadata basura que dejan editores, escáneres y plantillas.
    """
    tl = title.lower().strip()
    al = author.lower().strip()

    # ── Título: extensiones de archivo fuente ────────────────────────────────
    bad_exts = (".pmd", ".indd", ".doc", ".docx", ".qxd", ".tex",
                ".odt", ".rtf", ".pdf", ".pub", ".pages", ".dvi")
    if tl.endswith(bad_exts):
        return False

    # ── Título: nombres tipo "03 bonomo", "12_paper" ─────────────────────────
    if re.match(r'^\d{1,3}[\s_\-\.]', title):
        return False

    # ── Título: placeholders genéricos ───────────────────────────────────────
    placeholder_titles = {
        "untitled", "sin título", "sin titulo", "documento1", "document1",
        "documento", "document", "template", "plantilla", "new document",
        "nuevo documento", "scanned document", "escaneado", "scan",
        "pdf document", "acrobat document", "presentación1", "presentation1"
    }
    if tl in placeholder_titles:
        return False

    # ── Título: rutas de archivo ─────────────────────────────────────────────
    if re.match(r'^[A-Z]:[\\/]', title) or "/users/" in tl or "\\users\\" in tl:
        return False

    # ── Título: longitud mínima razonable ────────────────────────────────────
    if len(title) < 10:
        return False

    # ── Autor: demasiado corto (iniciales "ME", "JP") ────────────────────────
    if len(author) < 4:
        return False

    # ── Autor: nombres genéricos del sistema operativo ───────────────────────
    bad_authors = {
        "user", "usuario", "admin", "administrator", "administrador",
        "unknown", "windows", "owner", "dueño", "propietario", "computer",
        "computadora", "pc", "mac", "default", "guest", "invitado",
        "acrobat", "adobe", "microsoft", "word", "author", "autor"
    }
    if al in bad_authors:
        return False

    # ── Autor: marcas de escáneres e impresoras ──────────────────────────────
    scanner_brands = ("hp ", "epson", "canon", "brother", "xerox",
                      "kyocera", "ricoh", "lexmark", "samsung", "scansnap",
                      "scanner", "scanjet", "workforce", "officejet")
    if any(b in al for b in scanner_brands):
        return False

    # ── Autor: palabras de software ──────────────────────────────────────────
    if any(w in al for w in ("pdfcreator", "nitro", "foxit", "acrobat")):
        return False

    return True


def _from_pdf_metadata(reader: PyPDF2.PdfReader, log) -> str | None:
    """
    Muchos PDFs (especialmente libros digitales bien hechos) tienen
    /Title y /Author guardados en sus metadatos. Es la fuente más limpia,
    PERO también donde los editores suelen dejar basura como nombres de
    archivo fuente. Validamos antes de confiar.
    """
    meta = reader.metadata
    if not meta:
        return None

    title  = (meta.get("/Title")  or "").strip()
    author = (meta.get("/Author") or "").strip()

    if not title or not author:
        return None

    # Primero intentamos limpiar prefijos comunes ("Microsoft Word - ", rutas…)
    title = _clean_metadata_title(title)

    if not _metadata_looks_valid(title, author):
        log(f"Metadata descartada (parece basura): {author} / {title[:40]}", TEXT_WARN)
        return None

    # Año desde /CreationDate (formato: D:20230615...)
    year = "s.f."
    raw = meta.get("/CreationDate") or meta.get("/ModDate") or ""
    m = re.search(r'D:(\d{4})', raw) or re.search(r'\b(19|20)\d{2}\b', raw)
    if m:
        year = m.group(1)

    log(f"Metadata: autor={author[:40]}, título={title[:40]}", TEXT_SEC)
    return _sanitize(f"{author} ({year}) - {title}")


# ── 2. Extracción de texto (con OCR si la página es imagen) ───────────────────

def _extract_text(reader: PyPDF2.PdfReader, ruta: str, log) -> str:
    """
    Lee hasta PAGES_SCAN páginas. Si una página está en blanco
    (tapa como imagen) y OCR está disponible, la reconoce con Tesseract.
    """
    pages = min(PAGES_SCAN, len(reader.pages))
    full  = ""

    doc_fitz = None
    if OCR_AVAILABLE:
        try:
            doc_fitz = fitz.open(ruta)
        except Exception:
            pass

    for i in range(pages):
        page_text = reader.pages[i].extract_text() or ""

        if len(page_text.strip()) < 30:
            if OCR_AVAILABLE and doc_fitz:
                log(f"Pág. {i+1}: imagen detectada, aplicando OCR…", TEXT_WARN)
                try:
                    pix = doc_fitz[i].get_pixmap(dpi=200)
                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    page_text = pytesseract.image_to_string(img, lang="spa+eng")
                    log(f"Pág. {i+1}: OCR completado", TEXT_OK)
                except Exception as e:
                    log(f"OCR falló en pág. {i+1}: {e}", TEXT_WARN)
            else:
                if i == 0:
                    log("Pág. 1 parece una imagen. Instalar PyMuPDF+pytesseract para OCR.", TEXT_WARN)

        full += page_text + "\n"

        # Parar temprano si ya hay suficiente texto y revisamos al menos 3 páginas
        if i >= 2 and len(full.strip()) > 2500:
            break

    if doc_fitz:
        doc_fitz.close()

    return full


# ── 3. DOI → CrossRef ─────────────────────────────────────────────────────────

def _find_doi(text: str) -> str | None:
    m = re.search(r'10\.\d{4,}/[^\s\]\[\'"<>]+', text)
    return m.group(0).rstrip(".,;)") if m else None


def _format_authors_crossref(authors: list) -> str:
    if not authors:
        return "Anónimo"
    fams = [a.get("family", a.get("name", "?")) for a in authors]
    if len(fams) == 1: return fams[0]
    if len(fams) == 2: return f"{fams[0]} y {fams[1]}"
    return f"{fams[0]} et al"


def _query_crossref(doi: str) -> str | None:
    url = f"https://api.crossref.org/works/{urllib.parse.quote(doi, safe='')}"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": UA}), timeout=12
        ) as r:
            meta = json.loads(r.read()).get("message", {})
    except Exception:
        return None

    author_str = _format_authors_crossref(meta.get("author", []))
    year = "s.f."
    for field in ("published-print", "published-online", "issued", "created"):
        parts = meta.get(field, {}).get("date-parts", [[None]])[0]
        if parts and parts[0]:
            year = str(parts[0]); break

    titles = meta.get("title", [])
    title  = titles[0].strip() if titles else "Sin título"
    return _sanitize(f"{author_str} ({year}) - {title}")


# Regex que parsea un resultado "Autor (Año) - Título" — usado en varios lados.
_RESULT_RE = re.compile(r'^(.*?)\s*\((\d{4}|s\.f\.)\)\s*-\s*(.+)$', re.DOTALL)


# ── Utilidades generales de extracción ────────────────────────────────────────
#
# Estas funciones son señales UNIVERSALES que funcionan en cualquier paper,
# no importa de qué revista sea. Se usan tanto en búsqueda por APIs como en
# las heurísticas finales.

# Palabras comunes en dominios de institución que NO son apellidos.
_EMAIL_NON_NAMES = {
    "mail", "email", "correo", "gmail", "hotmail", "yahoo", "outlook", "live",
    "conicet", "uba", "unc", "unam", "ucm", "csic", "cnrs", "cnr", "mpg",
    "info", "admin", "contact", "webmaster", "noreply", "author", "editor",
    "arqueologia", "antropologia", "historia", "cultura", "ciencias",
}

def _surnames_from_emails(text: str) -> list:
    """
    Extrae apellidos de emails académicos del texto.
    Emails suelen tener forma `nombre.apellido@institucion`,
    `inicial.apellido@...` o `nombre.apellido-compuesto@...`.
    Preservamos guiones en apellidos compuestos (Criado-Boado).
    Señal universal: virtualmente cualquier paper moderno tiene emails.
    """
    emails = re.findall(
        r'\b([A-Za-z][\w\.\-]{1,40})@[\w\.\-]+\.[a-z]{2,10}\b', text
    )
    out, seen = [], set()
    for local in emails:
        # Si no hay separador interno (. o _), no podemos distinguir nombre
        # de apellido. Ej: "rolandojsilla" — descartamos para no ensuciar.
        if "." not in local and "_" not in local:
            continue
        # Separar SOLO por punto/guión-bajo: conservamos '-' para apellidos
        # compuestos tipo "criado-boado".
        parts = re.split(r'[\._]', local)
        for cand in reversed(parts):
            if not cand:
                continue
            # Un apellido compuesto puede traer guión: "criado-boado"
            core = cand.lower()
            plain = core.replace("-", "")
            if len(plain) < 3 or plain in _EMAIL_NON_NAMES:
                continue
            # Debe ser alfabético (permitiendo guiones internos)
            if not re.fullmatch(r'[a-zñáéíóúü]+(?:-[a-zñáéíóúü]+)*', core):
                continue
            # Capitalizar cada segmento entre guiones: criado-boado → Criado-Boado
            nice = "-".join(s.capitalize() for s in core.split("-"))
            if nice.lower() not in seen:
                seen.add(nice.lower())
                out.append(nice)
            break
    return out


def _split_glued_words(s: str) -> str:
    """
    Separa palabras pegadas por extracción PDF defectuosa:
      'SurandinasEl patrimonio' → 'Surandinas El patrimonio'
    Heurística segura: minúscula seguida de Mayúscula (no acrónimo).
    """
    return re.sub(r'([a-záéíóúüñ])([A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ])', r'\1 \2', s)


def _smart_title_case(s: str) -> str:
    """
    Convierte un título ALL-CAPS o Mezclado a forma legible:
      "TEMPRANA COMPLEJIDAD FUNERARIA EN LA COSTA" →
      "Temprana complejidad funeraria en la costa"
    No toca si ya tiene una proporción razonable de minúsculas (>30%).
    """
    if not s:
        return s
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return s
    lowers = sum(1 for c in letters if c.islower())
    if lowers / len(letters) > 0.30:
        return s  # Ya tiene mezcla natural, no tocar

    # Todo o casi todo mayúsculas: capitalizar primera letra de oración,
    # dejar el resto en minúscula. Preservar acrónimos de 2-4 letras
    # consecutivas si los hubiera (raro en títulos ALL-CAPS, pero por las dudas).
    result = s.lower()
    # Primera letra mayúscula
    result = result[:1].upper() + result[1:]
    # Después de ". " o ": " también
    result = re.sub(r'([\.\:]\s+)([a-záéíóúüñ])',
                    lambda m: m.group(1) + m.group(2).upper(), result)
    return result


def _is_review(text: str) -> bool:
    """Detecta si el documento es una reseña de libro.
    Tolera mojibake (ñ/ó corrompidas por encoding en el texto extraído)."""
    head = text[:2000].lower()
    # `.` en lugar de `[ñn]` tolera mojibake (ej: 'rese?a', 'rese\ufffda').
    # Sin `\b` porque PDFs extraen texto pegado sin espacios ('brRese?a').
    return bool(re.search(
        r'rese.a\s+de\s+libro|rese.a\s+bibliogr.fica|'
        r'book\s+review|review\s+of\s+', head
    ))


# Palabras que, si aparecen en el título devuelto por una API, indican
# que el registro está mal indexado (título contaminado con header/fecha
# de recepción). Universal: ningún título real de paper tiene estas palabras.
_API_TITLE_NOISE_RE = re.compile(
    # Sin `\b` porque los PDFs mal maquetados pegan palabras: '39Recibido'.
    r'(Recibido|Aceptado|Received|Accepted|ISSN|Copyright|'
    r'E-?mail|DOI\s*:|https?://)',
    re.IGNORECASE
)

def _api_title_is_clean(api_title: str) -> bool:
    """Rechaza títulos API con ruido obvio (headers mal indexados)."""
    if len(api_title) > 280:
        return False
    if _API_TITLE_NOISE_RE.search(api_title):
        return False
    return True


# ── 3b. Búsqueda por título en CrossRef (fuzzy) ───────────────────────────────
#
# Esta es la RED DE SEGURIDAD real para papers sin DOI embebido.
# CrossRef tiene 150M+ de obras y su endpoint /works?query.title=...
# devuelve matches aproximados con un score. Le mandamos candidatos de título
# sacados del texto y, si hay un match con score alto y suficiente solapamiento
# de palabras, usamos el registro canónico (autores, año, título reales).

def _title_candidates_for_search(text: str) -> list:
    """
    Saca candidatos a título de las primeras líneas del texto.
    No intenta ser perfecto: manda varias opciones a CrossRef y que el
    matching fuzzy haga su trabajo.
    """
    # Reparar palabras pegadas (defecto común de extracción PDF)
    text = _split_glued_words(text)
    lines = [l.strip() for l in text.splitlines() if len(l.strip()) >= 15]
    clean = []
    for l in lines[:15]:
        ll = l.lower()
        # Descartar ruido obvio (igual que en heurísticas, pero light)
        if any(k in ll for k in ("http", "www.", "issn", "doi:",
                                 "copyright", "©", "all rights reserved")):
            continue
        if re.match(r'^[\d\W]+$', l):
            continue
        # Cita de revista
        if re.search(r'\b\d{1,3}\s*[\(:]\s*\d', l) and \
           re.search(r'\d+\s*[-–]\s*\d+', l):
            continue
        # Quitar email-pegado-a-título si aparece
        m = re.search(r'@[\w\.\-]+\.[a-z]{2,4}([A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ].+)$', l)
        if m:
            l = m.group(1).strip()
        clean.append(l)

    # Generar queries: una línea, dos consecutivas, tres consecutivas
    queries = []
    seen = set()
    for n in (1, 2, 3):
        for i in range(max(0, len(clean) - n + 1)):
            q = " ".join(clean[i:i + n]).strip()
            if 20 <= len(q) <= 300 and q not in seen:
                queries.append(q)
                seen.add(q)
    return queries[:6]   # máximo 6 intentos


def _title_similarity(q: str, api_title: str) -> tuple:
    """
    Devuelve (fracción, count_overlap): fracción de palabras ≥4 chars del
    api_title que aparecen en q, y cantidad absoluta de palabras en común.
    Usar ambas en validación: fracción alta + count mínimo evita matches
    espurios con títulos API cortos (donde 1/2 palabras da ratio 0.5).
    """
    q_words = set(re.findall(r'[a-záéíóúüñ]{4,}', q.lower()))
    t_words = set(re.findall(r'[a-záéíóúüñ]{4,}', api_title.lower()))
    if not t_words:
        return (0.0, 0)
    common = q_words & t_words
    return (len(common) / len(t_words), len(common))


def _query_crossref_by_title(text: str, log) -> str | None:
    """
    Búsqueda fuzzy en CrossRef. Sólo acepta el match si pasa TRES validaciones:
      a) score ≥ 70 y similitud de título ≥ 0.75
      b) al menos un apellido de los autores del match aparece en el texto
      c) el año del match coincide con algún año del texto (± 1)

    Si algún match falla, prueba otro query. Si todos fallan → None
    (se cae a la etapa siguiente del pipeline).
    """
    queries = _title_candidates_for_search(text)
    if not queries:
        return None

    text_lower = text.lower()
    text_years = set(re.findall(r'\b(?:19|20)\d{2}\b', text))
    email_surnames = _surnames_from_emails(text)
    author_qs = "&query.author=" + urllib.parse.quote(email_surnames[0]) if email_surnames else ""

    for q in queries:
        url = (
            "https://api.crossref.org/works?"
            f"query.title={urllib.parse.quote(q)}"
            f"{author_qs}"
            "&rows=3&select=title,author,issued,score"
        )
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": UA}), timeout=10
            ) as r:
                items = json.loads(r.read()).get("message", {}).get("items", [])
        except Exception:
            continue

        for item in items:
            score = item.get("score", 0)
            api_title = (item.get("title", [""]) or [""])[0]
            if not api_title or not _api_title_is_clean(api_title):
                continue

            sim, common = _title_similarity(q, api_title)
            if score < 70 or sim < 0.75 or common < 3:
                continue

            # Año del match
            year = None
            parts = item.get("issued", {}).get("date-parts", [[None]])[0]
            if parts and parts[0]:
                year = str(parts[0])

            # Validación C: año coincide con algún año del texto ±1
            if year and text_years:
                y = int(year)
                if not any(abs(y - int(ty)) <= 1 for ty in text_years):
                    continue

            # Validación B: al menos un apellido del match aparece en el texto
            authors = item.get("author", [])
            fams = [a.get("family", "") for a in authors if a.get("family")]
            if fams:
                if not any(f.lower() in text_lower for f in fams):
                    continue

            # Pasa todas las validaciones
            author_str = _format_authors_crossref(authors)
            log(f"CrossRef match (score={score:.0f}, sim={sim:.2f})", TEXT_OK)
            return _sanitize(f"{author_str} ({year or 's.f.'}) - {api_title}")

    return None


# ── 3c. OpenAlex (búsqueda por título) ────────────────────────────────────────
#
# OpenAlex indexa ~250M de obras, incluye muchas revistas latinoamericanas
# y regionales que CrossRef no tiene. API pública sin key.

def _openalex_format_authors(authorships: list) -> str:
    fams = []
    for a in authorships:
        name = (a.get("author", {}) or {}).get("display_name", "")
        if not name:
            continue
        # Último token como apellido (OpenAlex no separa family/given)
        parts = name.strip().split()
        if parts:
            fams.append(parts[-1])
    if not fams:
        return "Anónimo"
    if len(fams) == 1: return fams[0]
    if len(fams) == 2: return f"{fams[0]} y {fams[1]}"
    return f"{fams[0]} et al"


def _query_openalex_by_title(text: str, log) -> str | None:
    queries = _title_candidates_for_search(text)
    if not queries:
        return None

    text_lower = text.lower()
    text_years = set(re.findall(r'\b(?:19|20)\d{2}\b', text))
    email_surnames = _surnames_from_emails(text)

    # Probar con "title + surname" PRIMERO (más precisión), luego solo título.
    search_pairs = []  # (url_query, similarity_query)
    if email_surnames:
        for q in queries[:3]:
            search_pairs.append((f"{q} {email_surnames[0]}", q))
    for q in queries:
        search_pairs.append((q, q))

    for search_q, sim_q in search_pairs:
        url = (
            "https://api.openalex.org/works?"
            f"search={urllib.parse.quote(search_q)}"
            "&per-page=3"
        )
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": UA}), timeout=10
            ) as r:
                items = json.loads(r.read()).get("results", [])
        except Exception:
            continue

        for item in items:
            api_title = (item.get("title") or "").strip()
            if not api_title or not _api_title_is_clean(api_title):
                continue
            sim, common = _title_similarity(sim_q, api_title)
            if sim < 0.75 or common < 3:
                continue

            year = item.get("publication_year")
            if year and text_years:
                if not any(abs(int(year) - int(ty)) <= 1 for ty in text_years):
                    continue

            authorships = item.get("authorships", [])
            fams_full = []
            for a in authorships:
                name = (a.get("author", {}) or {}).get("display_name", "")
                if name:
                    fams_full.append(name.split()[-1])
            if fams_full:
                if not any(f.lower() in text_lower for f in fams_full):
                    continue

            author_str = _openalex_format_authors(authorships)
            log(f"OpenAlex match (sim={sim:.2f})", TEXT_OK)
            return _sanitize(f"{author_str} ({year or 's.f.'}) - {api_title}")

    return None


# ── 3d. Semantic Scholar (búsqueda por título) ────────────────────────────────
#
# Cobertura fuerte en ciencias. API pública sin key (rate-limited pero
# suficiente para uso interactivo).

def _s2_format_authors(authors: list) -> str:
    fams = []
    for a in authors:
        name = a.get("name", "")
        if not name:
            continue
        fams.append(name.strip().split()[-1])
    if not fams:
        return "Anónimo"
    if len(fams) == 1: return fams[0]
    if len(fams) == 2: return f"{fams[0]} y {fams[1]}"
    return f"{fams[0]} et al"


def _query_semantic_scholar_by_title(text: str, log) -> str | None:
    queries = _title_candidates_for_search(text)
    if not queries:
        return None

    text_lower = text.lower()
    text_years = set(re.findall(r'\b(?:19|20)\d{2}\b', text))
    email_surnames = _surnames_from_emails(text)

    search_pairs = []
    if email_surnames:
        for q in queries[:3]:
            search_pairs.append((f"{q} {email_surnames[0]}", q))
    for q in queries:
        search_pairs.append((q, q))

    for search_q, sim_q in search_pairs:
        url = (
            "https://api.semanticscholar.org/graph/v1/paper/search?"
            f"query={urllib.parse.quote(search_q)}"
            "&limit=3&fields=title,authors,year"
        )
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": UA}), timeout=10
            ) as r:
                items = json.loads(r.read()).get("data", [])
        except Exception:
            continue

        for item in items:
            api_title = (item.get("title") or "").strip()
            if not api_title or not _api_title_is_clean(api_title):
                continue
            sim, common = _title_similarity(sim_q, api_title)
            if sim < 0.75 or common < 3:
                continue

            year = item.get("year")
            if year and text_years:
                if not any(abs(int(year) - int(ty)) <= 1 for ty in text_years):
                    continue

            authors = item.get("authors", [])
            fams_full = [a.get("name", "").split()[-1] for a in authors if a.get("name")]
            if fams_full:
                if not any(f.lower() in text_lower for f in fams_full):
                    continue

            author_str = _s2_format_authors(authors)
            log(f"Semantic Scholar match (sim={sim:.2f})", TEXT_OK)
            return _sanitize(f"{author_str} ({year or 's.f.'}) - {api_title}")

    return None


# ── 4. ISBN → Open Library ────────────────────────────────────────────────────

def _find_isbn(text: str) -> str | None:
    """Busca ISBN-13 (978/979) o ISBN-10 en el texto."""
    # Con prefijo ISBN
    m = re.search(r'ISBN[-:\s]*(97[89][\d\-\s]{10,16}\d)', text, re.IGNORECASE)
    if m:
        isbn = re.sub(r'[-\s]', '', m.group(1))
        if len(isbn) == 13:
            return isbn

    # Bare ISBN-13
    m = re.search(r'\b(97[89]\d{10})\b', text)
    if m:
        return m.group(1)

    # ISBN-10 con prefijo
    m = re.search(r'ISBN[-:\s]*([\dX]{10})\b', text, re.IGNORECASE)
    if m:
        return m.group(1)

    return None


def _query_open_library(isbn: str) -> str | None:
    url = f"https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&format=json&jscmd=data"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": UA}), timeout=12
        ) as r:
            data = json.loads(r.read())
    except Exception:
        return None

    meta = data.get(f"ISBN:{isbn}")
    if not meta:
        return None

    # Autores
    authors = meta.get("authors", [])
    if not authors:
        author_str = "Anónimo"
    else:
        names = [a.get("name", "?") for a in authors]
        fams  = [n.split()[-1] for n in names]
        if len(fams) == 1:   author_str = fams[0]
        elif len(fams) == 2: author_str = f"{fams[0]} y {fams[1]}"
        else:                author_str = f"{fams[0]} et al"

    # Año
    raw  = meta.get("publish_date", "")
    ym   = re.search(r'\b(19|20)\d{2}\b', raw)
    year = ym.group(0) if ym else (raw or "s.f.")

    # Título
    title    = meta.get("title", "Sin título")
    subtitle = meta.get("subtitle", "")
    if subtitle:
        title = f"{title}: {subtitle}"

    return _sanitize(f"{author_str} ({year}) - {title}")


# ── 5. Heurísticas (papers + libros) ──────────────────────────────────────────
#
# Idea central: los papers académicos tienen una estructura estable en las
# primeras páginas:
#
#     [encabezado de revista: 'Journal X 9: 25-41. 2008']   ← ruido
#     [copyright / ISSN / DOI / URL]                         ← ruido
#     [afiliaciones / direcciones / emails]                  ← ruido
#     TÍTULO (1-4 líneas)
#     AUTORES (línea con nombres propios)
#     [más afiliaciones posiblemente]
#     Recibido … / Resumen / Abstract   ← corta aquí
#
# Entonces la estrategia es:
#   1. Cortar al primer marcador de fin de cabecera (Resumen/Abstract/…).
#   2. Dentro de la cabecera, descartar líneas obviamente no-título
#      (citas de revista, copyright, emails, afiliaciones).
#   3. Ubicar la línea de autores (tiene nombres propios y es corta/media).
#   4. Todo lo de antes de esa línea → título.
#
# Para libros (sin esa estructura) hay un atajo: `by Autor` o `© YYYY Autor`.

END_OF_HEADER_RE = re.compile(
    r'^\s*(resumen|abstract|recibido|received|introducción|introduccion|'
    r'introduction|palabras\s+clave|keywords?|sumario|summary)\b',
    re.IGNORECASE
)

# Patrones fuertes de afiliación institucional / direcciones.
AFFILIATION_RE = re.compile(
    r'\bfacultad\s+de\b|'
    r'\buniversidad\s+(?:de\s+|nacional|autónoma|autonoma|del)\b|'
    r'\bdepartamento\s+de\b|'
    r'\binstituto\s+(?:de\s+|nacional|superior)\b|'
    r'\bconicet\b|'
    r'\bescuela\s+de\b|'
    r'\bcentro\s+de\s+investigaci|'
    r'\bmuseo\s+(?:de\s+|nacional)\b|'
    r'\bavda\.\s|\bavenida\s|\bcalle\s|'
    r'\bfaculty\s+of\b|\buniversity\s+of\b|'
    r'\bdepartment\s+of\b|\binstitute\s+of\b|'
    r'\bschool\s+of\b',
    re.IGNORECASE
)

NOISE_WORDS = ('issn', 'doi:', 'http://', 'https://', 'www.',
               'copyright', '©', 'all rights reserved',
               'derechos reservados')

# 'Nombre [Inicial.]* Apellido [SegundoApellido]'
NAME_RE = re.compile(
    r'[A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]+'
    r'(?:\s+[A-ZÁÉÍÓÚÜÑ]\.)*'
    r'\s+[A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]+'
    r'(?:\s+[A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]+)?'
)


def _clean_email_glued(line: str) -> str:
    """
    En PDFs escaneados a veces el email y el título quedan pegados:
        'E-mail: autor@dominio.arTítulo del paper'
    Devuelve sólo la parte del título cuando el patrón calza.
    """
    m = re.search(
        r'@[\w\.\-]+\.[a-z]{2,4}([A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ].+)$',
        line
    )
    return m.group(1).strip() if m else line


def _is_header_noise(line: str) -> bool:
    """True si la línea no es título ni autores (ruido a descartar)."""
    ll = line.lower()
    if len(line.strip()) < 3:
        return True
    if re.match(r'^[\d\W]+$', line):                       # sólo números/símbolos
        return True
    if any(w in ll for w in NOISE_WORDS):
        return True
    # Cita de revista: '... 9: 25-41'
    if re.search(r'\b\d{1,3}\s*[\(:]\s*\d', line) and \
       re.search(r'\d+\s*[-–]\s*\d+', line):
        return True
    if '@' in line:                                        # email no limpiado
        return True
    if AFFILIATION_RE.search(line):                        # afiliación
        return True
    if re.match(r'^(pag|pág|page|p)\.?\s*\d+\s*$', ll):    # nº de página
        return True
    return False


def _looks_like_author_line(line: str) -> bool:
    """
    Línea de autores: contiene nombres propios, no es afiliación, corta-media.
    Caso especial: un solo nombre aislado ('Nélida Pal') también cuenta si
    la línea entera tiene ≤6 palabras.
    """
    if len(line) > 250 or len(line) < 5:
        return False
    if '@' in line or AFFILIATION_RE.search(line):
        return False
    names = NAME_RE.findall(line)
    if not names:
        return False
    if len(names) >= 2:
        return True
    if len(names) == 1 and len(line.split()) <= 6:
        return True
    return False


# ── Heurísticas en modo STREAM (robusto a PDFs sin saltos de línea) ──────────

# Marcadores de fin de cabecera. Se buscan EN CUALQUIER POSICIÓN del stream.
STREAM_END_RE = re.compile(
    r'\b(Resumen|Abstract|Palabras\s+claves?|Keywords?|Key\s+words|'
    r'Summary|Recibido|Received|Aceptado|Accepted|'
    r'ISSN|Copyright|E-?mail|Correo\s+electr[oó]nico)\b',
    re.IGNORECASE
)

# Autor con marca de afiliación (superíndice numérico, asterisco, †, ‡).
# Señal fuerte: 'Francisco Rothhammer1,2,3' / 'John A. Smith*'
# Restricción de contexto: debe estar precedido por
#   (a) inicio del texto,
#   (b) un punto + espacio (fin de oración anterior),
#   (c) una palabra ALL-CAPS de 3+ letras (típicamente el final del título,
#       que suele estar en mayúsculas en revistas académicas).
# Esto evita falsos positivos como 'Laguna La Barrancosa 1' (sitio con número)
# o 'del Valle 5737' (dirección).
NAME_WITH_MARKER_RE = re.compile(
    r'(?:^|[\.\n]\s+|\b[A-ZÁÉÍÓÚÜÑ]{3,}\s+)'
    r'([A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]+(?:\s+[A-ZÁÉÍÓÚÜÑ]\.)*\s+'
    r'[A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]+)'
    r'\s*(?:\d+(?:\s*,\s*\d+)*|\*|†|‡)'
)

# Lista de autores: "Name Surname, Name Surname, … y Name Surname"
AUTHOR_LIST_RE = re.compile(
    r'(?:[A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]+(?:\s+[A-ZÁÉÍÓÚÜÑ]\.)?\s+'
    r'[A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]+\s*[,;]\s+){1,5}'
    r'(?:y\s+|and\s+|e\s+)?'
    r'[A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]+(?:\s+[A-ZÁÉÍÓÚÜÑ]\.)?\s+'
    r'[A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]+'
)

# Extraer nombres sueltos de un bloque ya validado como lista de autores.
NAME_ONLY_RE = re.compile(
    r'[A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]+(?:\s+[A-ZÁÉÍÓÚÜÑ]\.)?\s+'
    r'[A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]+'
)

# Prefijo típico de revista: "Vol 46, Nº 1, 2014. Páginas 145-151"
JOURNAL_PREFIX_RE = re.compile(
    r'^\s*[Vv]ol(?:umen|\.)?\s*\d+\S*[,\s]+'
    r'(?:N[ºo°]?\s*\d+\S*[,\s]+)?'
    r'(?:(?:19|20)\d{2}[\s.,]+)?'
    r'(?:[Pp][áa]g(?:inas?|s)?\.?\s*[\d\-–\s]+)?'
)


def _surnames_to_str(fams: list) -> str:
    if not fams:       return "Anónimo"
    if len(fams) == 1: return fams[0]
    if len(fams) == 2: return f"{fams[0]} y {fams[1]}"
    return f"{fams[0]} et al"


def _dedupe(seq: list) -> list:
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x); out.append(x)
    return out


def _extract_year_stream(s: str) -> str:
    """'Vol X Nº Y YYYY' → 'Aceptado YYYY' → primer año en 1500 chars."""
    m = re.search(
        r'[Vv]ol(?:umen|\.)?\s*\d+\S*[,\s]+'
        r'(?:N[ºo°]?\s*\d+\S*[,\s]+)?'
        r'((?:19|20)\d{2})',
        s[:800]
    )
    if m:
        return m.group(1)
    m = re.search(
        r'(?:Aceptado|Accepted|Publicado|Published)[^.]{0,50}?'
        r'((?:19|20)\d{2})', s, re.IGNORECASE
    )
    if m:
        return m.group(1)
    m = re.search(r'\b((?:19|20)\d{2})\b', s[:1500])
    return m.group(1) if m else "s.f."


def _clean_title_stream(title: str) -> str:
    """Quita prefijo de revista, nombre de revista, normaliza ALL-CAPS."""
    # 1. Quitar prefijo "Vol X, Nº Y, YYYY. Páginas A-B"
    title = JOURNAL_PREFIX_RE.sub('', title).strip()

    # 2. Si empieza con algo tipo "Nombre, Revista de..." antes de un bloque
    # en MAYÚSCULAS (≥3 palabras capitalizadas), recortar hasta ese bloque.
    caps = re.search(
        r'[A-ZÁÉÍÓÚÜÑ]{3,}(?:\s+[A-ZÁÉÍÓÚÜÑ]{2,}){2,}',
        title
    )
    if caps and 0 < caps.start() < 200:
        title = title[caps.start():]

    # 3. Si todo el título está en MAYÚSCULAS, convertir a Title Case.
    if title and len(title) > 20 and title.upper() == title:
        title = title.title()

    title = title.strip(" .,:;-")
    return title


def _fix_pdf_kerning(s: str) -> str:
    """
    Repara artefactos de extracción de PDF tipo 'V olumen', 'F acultad'
    (una mayúscula aislada seguida de minúsculas). Estos aparecen en PDFs
    con kerning agresivo. Sólo V/W/Y/F (las problemáticas más comunes)
    para minimizar falsos positivos.
    """
    return re.sub(r'\b([VWYF])\s+([a-záéíóúñ]{2,})\b', r'\1\2', s)


def _heuristic_stream(text: str):
    """
    Busca autor+título+año trabajando el texto como stream (sin depender
    de saltos de línea). Sólo se activa cuando encuentra la señal FUERTE
    de autor con afiliación por superíndice (ej: 'Francisco Rothhammer1,2,3').
    Sin esa señal, devuelve None para que corra el modo líneas.
    """
    s = re.sub(r'\s+', ' ', text).strip()
    s = _fix_pdf_kerning(s)
    if len(s) < 40:
        return None

    end_m = STREAM_END_RE.search(s)
    header = s[:end_m.start()] if end_m else s[:3000]

    # Sólo autor-con-superíndice. La lista separada por comas es demasiado
    # ambigua en stream porque matchea direcciones ('La Plata, Buenos Aires').
    first_marker = NAME_WITH_MARKER_RE.search(header)
    if not first_marker:
        return None

    all_names = NAME_WITH_MARKER_RE.findall(header)
    fams = _dedupe([n.split()[-1] for n in all_names])
    author_str = _surnames_to_str(fams)
    author_pos = first_marker.start()

    year = _extract_year_stream(s)

    # Título = header antes del autor, limpiado
    title = _clean_title_stream(header[:author_pos])
    if len(title) < 5:
        title = "Sin título"

    return _sanitize(f"{author_str} ({year}) - {title}")


def _heuristic_review(text: str) -> str | None:
    """
    Caso especial general: reseñas de libros. Tienen estructura distinta de
    un paper normal (el autor es el reseñador, el título suele ser 'Reseña
    de ...'). Devuelve un resultado armado o None si no es reseña.
    """
    if not _is_review(text):
        return None
    # Año: 'Aceptado YYYY' o primer año suelto
    year = "s.f."
    m = re.search(r'\b(19|20)\d{2}\b', text)
    if m:
        year = m.group(0)
    # Autor: email-derivado o línea tipo "Nombre Apellido" justo antes/después
    # del marcador 'Reseña de libro'.
    email_fams = _surnames_from_emails(text)
    if email_fams:
        author_str = _surnames_to_str(email_fams)
    else:
        # Fallback: buscar nombre "Palabra Palabra" único cerca del marcador
        around = text[:3000]
        m2 = re.search(
            r'([A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]+(?:\s+[A-ZÁÉÍÓÚÜÑ]\.)*\s+'
            r'[A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]+)\s*\n?\s*'
            r'(?:Un\s|El\s|La\s|Este\s|Esta\s)',
            around
        )
        author_str = m2.group(1).split()[-1] if m2 else "Anónimo"
    # Título = el del libro reseñado. Está después del marcador "Reseña de libro".
    m3 = re.search(
        r'Rese[ñn]a\s+(?:de\s+libro|bibliogr[aá]fica)?\s*\n?\s*(.+?)'
        r'(?:\n\n|\.\s*\d{4}\.|\.\s+[A-ZÁÉÍÓÚÜÑ]\w+\s*:)',
        text, re.DOTALL | re.IGNORECASE
    )
    titulo = m3.group(1).strip() if m3 else "Reseña"
    titulo = re.sub(r'\s+', ' ', titulo)[:200]
    return f"{author_str} ({year}) - Reseña de {titulo}"


def _heuristic_name(text: str) -> str:
    """
    Dos pasadas:
      0. Reseña de libro (tipo especial, señal universal)
      1. STREAM: patrones fuertes (nombre+superíndice, lista con comas).
         Robusto cuando el PDF extrae todo como un bloque único.
      2. LÍNEAS (fallback): lógica estructural por línea, útil para PDFs
         con saltos preservados (papers tipo Intersecciones).

    Post-proceso UNIVERSAL: si hay emails académicos en el texto, los
    apellidos derivados de ellos PISAN el autor extraído por regex.
    Los emails son señal mucho más confiable que cualquier heurística
    sobre nombres, porque vienen del propio autor del paper.
    """
    review = _heuristic_review(text)
    if review:
        return review
    result = _heuristic_stream(text) or _heuristic_lines(text)
    if not result:
        return result

    m = _RESULT_RE.match(result)
    if not m:
        return result
    autor, year, titulo = m.group(1), m.group(2), m.group(3)

    # 1) Override del autor con apellidos desde emails (señal fuerte, universal)
    email_fams = _surnames_from_emails(text)
    if email_fams:
        autor = _surnames_to_str(email_fams)

    # 2) Cleanup de título si está contaminado. Ancla = primer apellido del
    #    autor ya resuelto (venga de email o de heurística).
    titulo_es_basura = (
        len(titulo) < 15 or
        len(titulo) > 260 or
        bool(re.match(r'^\d+\s*[Nn][º°o]', titulo)) or
        bool(re.match(r'^\d+\W*$', titulo[:20])) or
        "pp." in titulo[:30].lower() or
        titulo.lower().startswith(("vol", "volumen", "issn")) or
        bool(_API_TITLE_NOISE_RE.search(titulo))
    )
    if titulo_es_basura:
        anchor = (email_fams[0] if email_fams
                  else autor.split(" y ")[0].split(" et ")[0].split()[-1])
        better = _title_from_author_anchor(text, anchor)
        if better:
            titulo = better

    return f"{autor} ({year}) - {titulo}"


def _title_from_author_anchor(text: str, surname: str) -> str | None:
    """
    Busca el apellido del autor en el texto y devuelve la línea/fragmento
    inmediatamente anterior limpio como candidato a título. Mecanismo
    general: en casi todos los papers el título precede a la lista de autores.
    """
    text = _split_glued_words(text)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    # Encontrar primera línea que mencione el apellido
    idx = None
    for i, l in enumerate(lines):
        if re.search(rf'\b{re.escape(surname)}\b', l, re.IGNORECASE):
            idx = i; break
    if idx is None:
        return None

    # Recorrer hacia atrás hasta encontrar líneas "título-candidatas":
    # no son header/pie de revista ni afiliación. Sin `\b` porque los PDFs
    # extraen palabras pegadas ('39Recibido', 'ArgentinaRMAAgricultura').
    noise_re = re.compile(
        r'(ISSN|Copyright|Recibido|Aceptado|Received|Accepted|'
        r'\bvol(?:umen)?\b|n[º°o]\s*\d|pp\.|p[aá]g\.|'
        r'Revista|Departamento|Facultad|Universidad|E-?mail|correo)',
        re.IGNORECASE
    )
    title_lines = []
    for i in range(idx - 1, max(-1, idx - 8), -1):
        l = lines[i]
        if len(l) < 8 or len(l) > 200:
            continue
        if noise_re.search(l):
            continue
        if re.match(r'^[\d\W]+$', l):
            continue
        # Línea plausible
        title_lines.insert(0, l)
        if sum(len(x) for x in title_lines) > 200:
            break

    if not title_lines:
        return None
    out = " ".join(title_lines).strip()
    # Doble red: nunca devolver algo con ruido obvio
    if _API_TITLE_NOISE_RE.search(out):
        return None
    return out if 15 <= len(out) <= 240 else None


def _heuristic_lines(text: str) -> str:
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # ── Año ──────────────────────────────────────────────────────────────────
    # Preferir 'Aceptado/Accepted YYYY' (año real del paper) frente al primer
    # año suelto del texto (que suele ser el del encabezado de revista).
    year = "s.f."
    pref = re.search(
        r'(?:Aceptado|Accepted|Publicado|Published)'
        r'[^.\n]{0,40}?\b((?:19|20)\d{2})\b',
        text, re.IGNORECASE
    )
    if pref:
        year = pref.group(1)
    else:
        year_m = re.search(r'\b(19|20)\d{2}\b', text)
        if year_m:
            year = year_m.group(0)

    # ── Atajo libro: 'by Autor' / '© YYYY Autor' ─────────────────────────────
    book_author = None
    m = re.search(
        r'(?:^|\s)[Bb]y\s+('
        r'[A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]+(?:\s[A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]+){1,3})',
        text
    )
    if m:
        book_author = m.group(1).split()[-1]
    else:
        m = re.search(
            r'©\s*(?:19|20)\d{2}\s+(?:by\s+)?'
            r'([A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]+(?:\s[A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]+)+)',
            text
        )
        if m:
            book_author = m.group(1).split()[-1]

    # ── Zona header: hasta 'Resumen/Abstract/Recibido/…' ─────────────────────
    end_idx = len(lines)
    for i, line in enumerate(lines[:40]):
        if END_OF_HEADER_RE.match(line):
            end_idx = i
            break

    # ── Limpiar email-pegado-a-título y filtrar ruido ────────────────────────
    content = []
    for line in lines[:end_idx]:
        cleaned = _clean_email_glued(line)
        if _is_header_noise(cleaned):
            continue
        content.append(cleaned)

    # ── Encontrar línea de autores ───────────────────────────────────────────
    # Tomamos la ÚLTIMA línea que parezca autores dentro del contenido.
    # Motivo: el título a veces contiene falsos positivos (nombres geográficos
    # tipo 'Tres Arroyos y Buenos Aires'). La verdadera línea de autores
    # aparece pegada al final de la cabecera, justo antes de Recibido/Resumen.
    author_idx = None
    for i, line in enumerate(content):
        if _looks_like_author_line(line):
            author_idx = i

    # ── Título = todo lo anterior a la línea de autores ──────────────────────
    if author_idx is not None and author_idx > 0:
        title = " ".join(content[:author_idx]).strip()
    elif content:
        title = content[0]
        for extra in content[1:3]:
            if title.rstrip().endswith((".", "?", "!")):
                break
            if len(extra) < 5:
                break
            title = f"{title} {extra}"
    else:
        title = "Sin título"

    # ── Autor ────────────────────────────────────────────────────────────────
    if book_author:
        author_str = book_author
    elif author_idx is not None:
        found = NAME_RE.findall(content[author_idx])
        fams = []
        for n in found:
            parts = [p for p in n.split() if not re.match(r'^[A-ZÁÉÍÓÚÜÑ]\.$', p)]
            if parts:
                fams.append(parts[-1])
        fams = list(dict.fromkeys(fams))
        if   len(fams) == 1: author_str = fams[0]
        elif len(fams) == 2: author_str = f"{fams[0]} y {fams[1]}"
        elif len(fams) >= 3: author_str = f"{fams[0]} et al"
        else:                author_str = "Anónimo"
    else:
        author_str = "Anónimo"

    return _sanitize(f"{author_str} ({year}) - {title}")


# ── Orquestador principal ──────────────────────────────────────────────────────

def _finalize(result: str) -> str:
    """
    Post-procesador GENERAL que corre sobre CUALQUIER resultado del pipeline.
    Normaliza: aplica title-case a títulos ALL-CAPS, colapsa espacios, trunca
    si es absurdamente largo, y resanitiza.
    """
    if not result:
        return result
    m = _RESULT_RE.match(result.strip())
    if not m:
        return _sanitize(result)
    autor, year, titulo = m.group(1), m.group(2), m.group(3)
    titulo = _smart_title_case(titulo).strip()
    # Si el "título" es absurdamente largo (>240 chars) cortarlo en la primera
    # oración o signo fuerte, para no generar nombres de archivo monstruosos.
    if len(titulo) > 240:
        cut = re.search(r'[\.;:]\s+', titulo[:240])
        if cut:
            titulo = titulo[:cut.start()].strip()
        else:
            titulo = titulo[:240].rstrip() + "…"
    return _sanitize(f"{autor} ({year}) - {titulo}")


def get_new_name(reader: PyPDF2.PdfReader, ruta: str, log) -> str:
    """
    Pipeline completo:
      1. Metadata embebida del PDF       → más limpio, instantáneo
      2. DOI → CrossRef                  → papers con DOI embebido
      3a. CrossRef búsqueda por título   → papers sin DOI
      3b. OpenAlex búsqueda por título   → cobertura de revistas regionales
      3c. Semantic Scholar por título    → cobertura de ciencias
      4. ISBN → Open Library             → libros
      5. Heurísticas de texto            → último recurso
    Todos los resultados pasan por _finalize() para normalización final.
    """
    # 1. Metadata PDF
    result = _from_pdf_metadata(reader, log)
    if result:
        log("Fuente: metadata del PDF", TEXT_OK)
        return _finalize(result)

    # Extraer texto (los demás pasos lo usan)
    text = _extract_text(reader, ruta, log)
    if len(text.strip()) < 10:
        log("No se pudo extraer texto de ninguna página.", TEXT_ERR)
        return "Documento sin texto"

    # 2. DOI exacto → CrossRef
    doi = _find_doi(text)
    if doi:
        log(f"DOI encontrado: {doi}", TEXT_SEC)
        result = _query_crossref(doi)
        if result:
            log("Fuente: CrossRef (DOI)", TEXT_OK)
            return _finalize(result)
        log("CrossRef no respondió al DOI", TEXT_WARN)

    # 3a. Búsqueda por título en CrossRef (fuzzy, 150M+ obras)
    log("Buscando por título en CrossRef…", TEXT_SEC)
    result = _query_crossref_by_title(text, log)
    if result:
        log("Fuente: CrossRef (búsqueda por título)", TEXT_OK)
        return _finalize(result)

    # 3b. OpenAlex (buena cobertura de revistas regionales/latinoamericanas)
    log("Buscando en OpenAlex…", TEXT_SEC)
    result = _query_openalex_by_title(text, log)
    if result:
        log("Fuente: OpenAlex", TEXT_OK)
        return _finalize(result)

    # 3c. Semantic Scholar
    log("Buscando en Semantic Scholar…", TEXT_SEC)
    result = _query_semantic_scholar_by_title(text, log)
    if result:
        log("Fuente: Semantic Scholar", TEXT_OK)
        return _finalize(result)

    # 4. ISBN → Open Library
    isbn = _find_isbn(text)
    if isbn:
        log(f"ISBN encontrado: {isbn}", TEXT_SEC)
        result = _query_open_library(isbn)
        if result:
            log("Fuente: Open Library (libros)", TEXT_OK)
            return _finalize(result)
        log("Open Library no respondió", TEXT_WARN)

    # 5. Heurísticas (último recurso)
    log("Sin match en APIs — usando heurísticas", TEXT_WARN)
    return _finalize(_heuristic_name(text))


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  INTERFAZ GRÁFICA                                                           ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

PREV_W, PREV_H   = 286, 405   # px — panel de vista previa (A4, +10 %)
THUMB_RENDER_W   = PREV_W * 3  # resolución alta para hacer zoom sin pixelar


def _generate_thumbnail(ruta: str) -> 'tuple[Image.Image | None, str]':
    """Renderiza pág. 1 y extrae su texto (hilo secundario)."""
    if not THUMB_AVAILABLE:
        return None, ""
    try:
        doc       = fitz.open(ruta)
        if not doc.page_count:
            doc.close()
            return None, ""
        page      = doc[0]
        page_text = page.get_text("text")
        zoom      = THUMB_RENDER_W / page.rect.width
        pix       = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        img       = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        doc.close()
        return img, page_text
    except Exception:
        return None, ""


class QueueItem:
    """
    Una fila en la cola de procesamiento.
    Tiene: ícono de estado, nombre editable y botón de guardar.
    """

    def __init__(self, parent, ruta_original: str, app):
        self.ruta_original = ruta_original
        self.app           = app
        self.saved         = False
        self.analyzed      = False
        self._pil_img      = None
        self._page_text    = ""

        self.frame = ctk.CTkFrame(
            parent, fg_color=BG_CARD, corner_radius=8,
            border_width=1, border_color=BORDER
        )
        self.frame.pack(fill="x", pady=3, padx=2)
        self.frame.bind("<Button-1>", lambda e: self.app.show_preview(self))

        # Ícono de estado
        self.lbl_status = ctk.CTkLabel(
            self.frame, text="⟳", font=("Consolas", 14, "bold"),
            text_color=TEXT_SEC, width=22
        )
        self.lbl_status.pack(side="left", padx=(10, 2), pady=8)

        # Nombre original (pequeño, encima del entry)
        center = ctk.CTkFrame(self.frame, fg_color="transparent")
        center.pack(side="left", fill="x", expand=True, padx=4, pady=6)

        self.lbl_orig = ctk.CTkLabel(
            center, text=os.path.basename(ruta_original),
            font=("Consolas", 8), text_color=TEXT_SEC, anchor="w"
        )
        self.lbl_orig.pack(anchor="w", fill="x")
        self.lbl_orig.bind("<Button-1>", lambda e: self.app.show_preview(self))

        # Entry editable con el nombre propuesto
        self.entry = ctk.CTkEntry(
            center, font=FONT_MONO_S,
            fg_color=BG_ROOT, border_color=BORDER, text_color=TEXT_PRI,
            placeholder_text="Analizando…", height=28
        )
        self.entry.pack(fill="x", pady=(2, 0))

        # Botones
        btns = ctk.CTkFrame(self.frame, fg_color="transparent")
        btns.pack(side="right", padx=8, pady=6)

        self.btn_save = ctk.CTkButton(
            btns, text="Guardar", width=72, height=28,
            font=("Segoe UI", 11, "bold"),
            fg_color=ACCENT, hover_color=ACCENT_HV, text_color=BG_ROOT,
            command=self.save, state="disabled"
        )
        self.btn_save.pack(side="left", padx=(0, 4))

        self.btn_del = ctk.CTkButton(
            btns, text="✕", width=28, height=28,
            font=("Segoe UI", 12),
            fg_color="transparent", hover_color="#2A1515",
            text_color=TEXT_SEC, border_width=1, border_color=BORDER,
            command=self.remove
        )
        self.btn_del.pack(side="left")

    def set_analyzed(self, nombre_propuesto: str):
        """Llamar desde el hilo principal cuando termina el análisis."""
        self.analyzed = True
        self.entry.delete(0, "end")
        self.entry.insert(0, nombre_propuesto)
        self.lbl_status.configure(text="·", text_color=TEXT_OK)
        self.btn_save.configure(state="normal")

    def set_error(self, msg: str):
        self.analyzed = True
        self.entry.delete(0, "end")
        self.entry.insert(0, f"⚠ {msg}")
        self.entry.configure(text_color=TEXT_ERR)
        self.lbl_status.configure(text="✗", text_color=TEXT_ERR)

    def set_thumbnail(self, pil_img, page_text: str = ""):
        """Llamar desde el hilo principal con imagen PIL y texto extraído."""
        self._pil_img   = pil_img
        self._page_text = page_text
        if pil_img is not None:
            self.app.show_preview(self)

    def save(self):
        if self.saved:
            return
        nombre = self.entry.get().strip()
        if not nombre:
            return
        if not nombre.lower().endswith(".pdf"):
            nombre += ".pdf"

        dest = filedialog.asksaveasfilename(
            title="Guardar como…",
            initialdir=os.path.dirname(self.ruta_original),
            initialfile=nombre,
            defaultextension=".pdf",
            filetypes=[("Archivos PDF", "*.pdf")]
        )
        if not dest:
            return
        try:
            shutil.copy2(self.ruta_original, dest)
            self.saved = True
            self.entry.configure(state="disabled", text_color=TEXT_SEC)
            self.btn_save.configure(state="disabled", text="✓ Guardado")
            self.lbl_status.configure(text="✓", text_color=TEXT_OK)
            self.app._update_stats()
        except Exception as e:
            messagebox.showerror("Error al guardar", str(e))

    def remove(self):
        self.frame.destroy()
        self.app._remove_item(self)


class PDFScholar(ctk.CTk):
    def __init__(self):
        super().__init__()

        ctk.set_appearance_mode("Dark")
        ctk.set_default_color_theme("blue")

        self.title("PDF Scholar")
        self.geometry("1100x760")
        self.minsize(940, 660)
        self.configure(fg_color=BG_ROOT)

        self._processing  = False
        self._items: list[QueueItem] = []
        self._full_img    = None
        self._zoom        = 1.0
        self._pan_x       = 0.0
        self._pan_y       = 0.0
        self._drag_start  = None
        self._build_ui()

    # ── Layout ─────────────────────────────────────────────────────────────────
    def _build_ui(self):
        self._build_header()
        content = ctk.CTkFrame(self, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=20, pady=(0, 16))
        self._build_main_view(content)

    def _build_header(self):
        header = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=62)
        header.pack(fill="x")
        header.pack_propagate(False)

        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.pack(side="left", padx=22, pady=10)

        ctk.CTkLabel(title_box, text="PDF Scholar",
                     font=FONT_TITLE, text_color=TEXT_PRI).pack(anchor="w")
        ctk.CTkLabel(title_box, text="Papers · Libros · Documentos",
                     font=FONT_MONO_S, text_color=TEXT_SEC).pack(anchor="w")

        ocr_color = "#0D2B24" if OCR_AVAILABLE else "#2B1A0D"
        ocr_text  = "● OCR activo" if OCR_AVAILABLE else "○ OCR inactivo"
        ocr_tc    = ACCENT if OCR_AVAILABLE else TEXT_WARN
        badge = ctk.CTkFrame(header, fg_color=ocr_color, corner_radius=6)
        badge.pack(side="right", padx=16)
        ctk.CTkLabel(badge, text=ocr_text,
                     font=FONT_BADGE, text_color=ocr_tc).pack(padx=10, pady=6)

    def _build_main_view(self, parent):
        # ── Divisor horizontal arrastrable ─────────────────────────────────────
        hpane = tk.PanedWindow(
            parent, orient="horizontal",
            bg=BORDER, sashwidth=5, sashpad=0,
            sashrelief="flat", bd=0
        )
        hpane.pack(fill="both", expand=True)

        # ── Columna izquierda: cola + log ───────────────────────────────────────
        left_col = ctk.CTkFrame(hpane, fg_color="transparent")
        hpane.add(left_col, minsize=360, stretch="always")

        # ── Columna derecha: vista previa ───────────────────────────────────────
        right_col = ctk.CTkFrame(hpane, fg_color=BG_PANEL, corner_radius=0)
        hpane.add(right_col, minsize=260, width=PREV_W + 42, stretch="never")
        self._build_preview_panel(right_col)

        # ── Barra superior: subir + acciones ───────────────────────────────────
        top_row = ctk.CTkFrame(left_col, fg_color="transparent")
        top_row.pack(fill="x", pady=(14, 8))

        self.btn_upload = ctk.CTkButton(
            top_row, text="↑  Subir PDFs",
            command=self._start_processing,
            font=("Segoe UI", 12, "bold"),
            height=36, width=140, corner_radius=8,
            fg_color=ACCENT, hover_color=ACCENT_HV, text_color=BG_ROOT
        )
        self.btn_upload.pack(side="left")

        self.btn_save_all = ctk.CTkButton(
            top_row, text="Guardar todos",
            command=self._save_all,
            font=FONT_SMALL, height=36, width=120, corner_radius=8,
            fg_color="transparent", hover_color=BG_CARD,
            text_color=TEXT_PRI, border_width=1, border_color=BORDER,
            state="disabled"
        )
        self.btn_save_all.pack(side="left", padx=8)

        self.btn_clear = ctk.CTkButton(
            top_row, text="Limpiar cola",
            command=self._clear_queue,
            font=FONT_SMALL, height=36, width=110, corner_radius=8,
            fg_color="transparent", hover_color=BG_CARD,
            text_color=TEXT_SEC, border_width=1, border_color=BORDER,
            state="disabled"
        )
        self.btn_clear.pack(side="left")

        # Stats a la derecha
        self.lbl_stats = ctk.CTkLabel(
            top_row, text="",
            font=FONT_MONO_S, text_color=TEXT_SEC
        )
        self.lbl_stats.pack(side="right", padx=4)

        # Pipeline info
        pipeline = ctk.CTkFrame(left_col, fg_color="#0D1E1A", corner_radius=6)
        pipeline.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(
            pipeline,
            text="metadata  →  DOI  →  CrossRef título  →  ISBN  →  heurísticas",
            font=FONT_MONO_S, text_color="#4AADA0"
        ).pack(pady=5, padx=8)

        # ── Cola scrolleable ───────────────────────────────────────────────────
        queue_header = ctk.CTkFrame(left_col, fg_color="transparent")
        queue_header.pack(fill="x", pady=(4, 4))

        ctk.CTkLabel(queue_header, text="COLA",
                     font=FONT_BADGE, text_color=TEXT_SEC).pack(side="left")

        self.queue = ctk.CTkScrollableFrame(
            left_col, fg_color=BG_PANEL, corner_radius=10,
            border_width=1, border_color=BORDER,
            scrollbar_button_color=BG_CARD, scrollbar_button_hover_color=BORDER,
            height=280
        )
        self.queue.pack(fill="both", expand=True)

        # Placeholder cuando está vacía
        self.empty_label = ctk.CTkLabel(
            self.queue,
            text="La cola está vacía.\nHacé clic en 'Subir PDFs' para empezar.",
            font=FONT_MONO_S, text_color=TEXT_SEC, justify="center"
        )
        self.empty_label.pack(pady=40)

        # ── Log (compacto) ─────────────────────────────────────────────────────
        log_header = ctk.CTkFrame(left_col, fg_color="transparent")
        log_header.pack(fill="x", pady=(8, 4))

        ctk.CTkLabel(log_header, text="LOG",
                     font=FONT_BADGE, text_color=TEXT_SEC).pack(side="left")
        ctk.CTkButton(log_header, text="limpiar",
                      font=FONT_MONO_S, text_color=TEXT_SEC,
                      fg_color="transparent", hover_color=BG_CARD,
                      width=56, height=20, command=self._clear_log).pack(side="right")

        self.log_box = ctk.CTkTextbox(
            left_col, font=("Consolas", 10), fg_color=BG_CARD, text_color=TEXT_PRI,
            border_width=1, border_color=BORDER, corner_radius=8,
            height=110, wrap="word",
            scrollbar_button_color=BG_PANEL, scrollbar_button_hover_color=BORDER
        )
        self.log_box.pack(fill="x")
        self.log_box.configure(state="disabled")

        self._log("Sistema listo. Sin API key necesaria.", TEXT_SEC)
        if not OCR_AVAILABLE:
            self._log("OCR inactivo. Instalar: pip install pymupdf pytesseract Pillow", TEXT_WARN)

    def _build_preview_panel(self, parent):
        # ── Divisor vertical arrastrable: imagen arriba / texto abajo ──────────
        vpane = tk.PanedWindow(
            parent, orient="vertical",
            bg=BORDER, sashwidth=5, sashpad=0,
            sashrelief="flat", bd=0
        )
        vpane.pack(fill="both", expand=True)

        # ── Panel superior: imagen + zoom ───────────────────────────────────────
        img_frame = ctk.CTkFrame(vpane, fg_color="transparent")
        vpane.add(img_frame, minsize=180, stretch="never")

        ctk.CTkLabel(
            img_frame, text="VISTA PREVIA",
            font=FONT_BADGE, text_color=TEXT_SEC
        ).pack(pady=(14, 6))

        self._canvas = tk.Canvas(
            img_frame,
            width=PREV_W, height=PREV_H,
            bg="#0E1017", highlightthickness=1,
            highlightbackground=BORDER, cursor="hand2"
        )
        self._canvas.pack(padx=14)

        self._canvas_img_id  = self._canvas.create_image(
            PREV_W // 2, PREV_H // 2, anchor="center"
        )
        self._canvas_text_id = self._canvas.create_text(
            PREV_W // 2, PREV_H // 2,
            text="Subí un PDF\npara ver\nla vista previa",
            fill=TEXT_SEC, font=("Consolas", 10), justify="center"
        )

        self._canvas.bind("<ButtonPress-1>",   self._on_drag_start)
        self._canvas.bind("<B1-Motion>",       self._on_drag_move)
        self._canvas.bind("<ButtonRelease-1>", self._on_drag_end)
        self._canvas.bind("<MouseWheel>",      self._on_wheel)

        zoom_row = ctk.CTkFrame(img_frame, fg_color="transparent")
        zoom_row.pack(pady=(6, 2))

        ctk.CTkButton(
            zoom_row, text="−", width=30, height=26,
            font=("Segoe UI", 14, "bold"),
            fg_color=BG_CARD, hover_color=BORDER,
            text_color=TEXT_PRI, border_width=1, border_color=BORDER,
            command=lambda: self._do_zoom(1 / 1.35)
        ).pack(side="left", padx=2)

        self.zoom_label = ctk.CTkLabel(
            zoom_row, text="100%", width=52,
            font=FONT_MONO_S, text_color=TEXT_SEC
        )
        self.zoom_label.pack(side="left", padx=4)

        ctk.CTkButton(
            zoom_row, text="+", width=30, height=26,
            font=("Segoe UI", 14, "bold"),
            fg_color=BG_CARD, hover_color=BORDER,
            text_color=TEXT_PRI, border_width=1, border_color=BORDER,
            command=lambda: self._do_zoom(1.35)
        ).pack(side="left", padx=2)

        ctk.CTkButton(
            zoom_row, text="↺", width=30, height=26,
            font=("Segoe UI", 11),
            fg_color=BG_CARD, hover_color=BORDER,
            text_color=TEXT_SEC, border_width=1, border_color=BORDER,
            command=self._zoom_reset
        ).pack(side="left", padx=(8, 2))

        self._preview_ref = None

        self.preview_filename = ctk.CTkLabel(
            img_frame, text="",
            font=("Consolas", 8), text_color=TEXT_SEC,
            wraplength=PREV_W, justify="center"
        )
        self.preview_filename.pack(pady=(4, 8), padx=8)

        # ── Panel inferior: texto extraído (arrastrable) ────────────────────────
        txt_frame = ctk.CTkFrame(vpane, fg_color="transparent")
        vpane.add(txt_frame, minsize=80, height=200, stretch="always")

        ctk.CTkLabel(
            txt_frame, text="TEXTO  PAG. 1",
            font=FONT_BADGE, text_color=TEXT_SEC
        ).pack(anchor="w", padx=14, pady=(8, 4))

        self._page_textbox = ctk.CTkTextbox(
            txt_frame,
            font=("Consolas", 11), fg_color=BG_ROOT, text_color=TEXT_PRI,
            border_width=1, border_color=BORDER, corner_radius=4,
            wrap="word",
            scrollbar_button_color=BG_CARD,
            scrollbar_button_hover_color=BORDER
        )
        self._page_textbox.pack(fill="both", expand=True, padx=14, pady=(0, 10))
        self._page_textbox.insert("1.0", "sin texto")
        self._page_textbox.configure(state="disabled")

    def show_preview(self, item: 'QueueItem'):
        if not THUMB_AVAILABLE or item._pil_img is None:
            return
        self._full_img = item._pil_img
        self._zoom, self._pan_x, self._pan_y = 1.0, 0.0, 0.0
        self._canvas.itemconfig(self._canvas_text_id, state="hidden")
        self._render_preview()
        self.preview_filename.configure(text=os.path.basename(item.ruta_original))
        self._update_zoom_label()
        txt = (item._page_text or "").strip()
        self._page_textbox.configure(state="normal")
        self._page_textbox.delete("1.0", "end")
        self._page_textbox.insert("1.0", txt if txt else "sin texto")

    def _render_preview(self):
        if self._full_img is None:
            return
        fw, fh = self._full_img.size
        view_w = fw / self._zoom
        view_h = fh / self._zoom
        self._pan_x = max(0.0, min(self._pan_x, max(0.0, fw - view_w)))
        self._pan_y = max(0.0, min(self._pan_y, max(0.0, fh - view_h)))
        x0, y0 = int(self._pan_x), int(self._pan_y)
        x1, y1 = int(self._pan_x + view_w), int(self._pan_y + view_h)
        display = self._full_img.crop((x0, y0, x1, y1)).resize(
            (PREV_W, PREV_H), Image.LANCZOS
        )
        photo = ImageTk.PhotoImage(display)
        self._preview_ref = photo
        self._canvas.itemconfig(self._canvas_img_id, image=photo)

    def _do_zoom(self, factor: float, mx: int = None, my: int = None):
        if self._full_img is None:
            return
        fw, fh   = self._full_img.size
        new_zoom = max(1.0, min(6.0, self._zoom * factor))
        if new_zoom == self._zoom:
            return
        # Punto de anclaje en coordenadas de imagen (mouse o centro)
        sx = mx if mx is not None else PREV_W / 2
        sy = my if my is not None else PREV_H / 2
        img_x = self._pan_x + sx * (fw / self._zoom / PREV_W)
        img_y = self._pan_y + sy * (fh / self._zoom / PREV_H)
        self._zoom  = new_zoom
        self._pan_x = img_x - sx * (fw / new_zoom / PREV_W)
        self._pan_y = img_y - sy * (fh / new_zoom / PREV_H)
        self._render_preview()
        self._update_zoom_label()

    def _zoom_reset(self):
        if self._full_img is None:
            return
        self._zoom, self._pan_x, self._pan_y = 1.0, 0.0, 0.0
        self._render_preview()
        self._update_zoom_label()

    def _update_zoom_label(self):
        self.zoom_label.configure(text=f"{int(self._zoom * 100)}%")

    def _on_drag_start(self, event):
        self._drag_start = (event.x, event.y)
        self._canvas.configure(cursor="fleur")

    def _on_drag_move(self, event):
        if self._drag_start is None or self._full_img is None:
            return
        dx = event.x - self._drag_start[0]
        dy = event.y - self._drag_start[1]
        self._drag_start = (event.x, event.y)
        fw, fh = self._full_img.size
        self._pan_x -= dx * (fw / self._zoom / PREV_W)
        self._pan_y -= dy * (fh / self._zoom / PREV_H)
        self._render_preview()

    def _on_drag_end(self, event):
        self._drag_start = None
        self._canvas.configure(cursor="hand2")

    def _on_wheel(self, event):
        self._do_zoom(1.35 if event.delta > 0 else 1 / 1.35, event.x, event.y)

    # ── Log ────────────────────────────────────────────────────────────────────
    def _log(self, msg: str, color: str = TEXT_PRI):
        prefix = {TEXT_OK: "✓ ", TEXT_ERR: "✗ ", TEXT_WARN: "⚠ ", TEXT_SEC: "· "}.get(color, "  ")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"{prefix}{msg}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _log_ts(self, msg: str, color: str = TEXT_PRI):
        self.after(0, lambda: self._log(msg, color))

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    # ── Stats / cola ───────────────────────────────────────────────────────────
    def _update_stats(self):
        total     = len(self._items)
        analyzed  = sum(1 for it in self._items if it.analyzed)
        saved     = sum(1 for it in self._items if it.saved)

        if total == 0:
            self.lbl_stats.configure(text="")
            self.btn_save_all.configure(state="disabled")
            self.btn_clear.configure(state="disabled")
            return

        self.lbl_stats.configure(
            text=f"{analyzed}/{total} analizados · {saved} guardados"
        )

        pendientes_guardar = any(it.analyzed and not it.saved for it in self._items)
        self.btn_save_all.configure(
            state="normal" if pendientes_guardar else "disabled"
        )
        self.btn_clear.configure(state="normal")

    def _remove_item(self, item: QueueItem):
        if item in self._items:
            self._items.remove(item)
        if not self._items and self.empty_label.winfo_exists() is False:
            self.empty_label = ctk.CTkLabel(
                self.queue,
                text="La cola está vacía.\nHacé clic en 'Subir PDFs' para empezar.",
                font=FONT_MONO_S, text_color=TEXT_SEC, justify="center"
            )
            self.empty_label.pack(pady=40)
        self._update_stats()

    def _clear_queue(self):
        for it in list(self._items):
            it.frame.destroy()
        self._items.clear()
        if not self.empty_label.winfo_exists():
            self.empty_label = ctk.CTkLabel(
                self.queue,
                text="La cola está vacía.\nHacé clic en 'Subir PDFs' para empezar.",
                font=FONT_MONO_S, text_color=TEXT_SEC, justify="center"
            )
            self.empty_label.pack(pady=40)
        self._clear_preview()
        self._update_stats()
        self._log("Cola limpiada.", TEXT_SEC)

    def _clear_preview(self):
        self._full_img    = None
        self._zoom        = 1.0
        self._pan_x       = 0.0
        self._pan_y       = 0.0
        self._preview_ref = None
        self._canvas.itemconfig(self._canvas_img_id,  image="")
        self._canvas.itemconfig(self._canvas_text_id, state="normal")
        self.preview_filename.configure(text="")
        self._update_zoom_label()
        self._page_textbox.configure(state="normal")
        self._page_textbox.delete("1.0", "end")
        self._page_textbox.insert("1.0", "sin texto")
        self._page_textbox.configure(state="disabled")

    def _save_all(self):
        pendientes = [it for it in self._items if it.analyzed and not it.saved]
        if not pendientes:
            return
        self._log(f"Guardando {len(pendientes)} archivos…", TEXT_SEC)
        for it in pendientes:
            it.save()  # abre un diálogo por cada uno

    # ── Procesamiento ──────────────────────────────────────────────────────────
    def _start_processing(self):
        if self._processing:
            return

        rutas = filedialog.askopenfilenames(
            title=f"Seleccioná PDFs (máx. {MAX_PDFS})",
            filetypes=[("Archivos PDF", "*.pdf")]
        )
        if not rutas:
            return

        if len(rutas) > MAX_PDFS:
            messagebox.showwarning(
                "Límite excedido",
                f"Se procesarán los primeros {MAX_PDFS} archivos."
            )
            rutas = rutas[:MAX_PDFS]

        # Sacar el placeholder si existe
        if self.empty_label.winfo_exists():
            self.empty_label.destroy()

        # Crear ítems en la cola inmediatamente
        nuevos_items = []
        for ruta in rutas:
            item = QueueItem(self.queue, ruta, self)
            self._items.append(item)
            nuevos_items.append(item)

        self._update_stats()
        self._log(f"── Analizando {len(nuevos_items)} archivos ──", TEXT_SEC)

        self._processing = True
        self.btn_upload.configure(state="disabled", text="Analizando…")

        threading.Thread(
            target=self._analyze_items, args=(nuevos_items,), daemon=True
        ).start()

    def _analyze_items(self, items: list):
        """Analiza cada item en segundo plano y actualiza su fila."""
        for item in items:
            ruta   = item.ruta_original
            nombre = os.path.basename(ruta)

            self.after(0, lambda n=nombre: self._log(f"── {n}", TEXT_SEC))

            try:
                with open(ruta, "rb") as f:
                    reader = PyPDF2.PdfReader(f)
                    if not reader.pages:
                        raise ValueError("PDF sin páginas.")
                    nuevo = get_new_name(reader, ruta, self._log_ts)
            except Exception as e:
                msg = str(e)
                self.after(0, lambda it=item, m=msg: (
                    it.set_error(m),
                    self._log(f"Error en {nombre}: {m}", TEXT_ERR)
                ))
                self.after(0, self._update_stats)
                continue

            pil_img, page_text = _generate_thumbnail(ruta)
            self.after(0, lambda it=item, n=nuevo, img=pil_img, txt=page_text: (
                it.set_analyzed(n),
                it.set_thumbnail(img, txt),
                self._log(f"✓ {nombre}  →  {n}", TEXT_OK)
            ))
            self.after(0, self._update_stats)

        self.after(0, self._finish_analysis)

    def _finish_analysis(self):
        self._processing = False
        self.btn_upload.configure(state="normal", text="↑  Subir PDFs")
        self._log("Análisis completo. Revisá la cola y guardá.", TEXT_OK)


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = PDFScholar()
    app.mainloop()
