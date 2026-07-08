"""Motor de renombrado de PDF Scholar (sin GUI).

Pipeline: metadata PDF -> DOI/CrossRef -> CrossRef(titulo) -> OpenAlex ->
          Semantic Scholar -> ISBN/Open Library -> OCR -> heuristicas

Expone:
  - get_new_name(reader, ruta, log) -> str
  - _generate_thumbnail(ruta) -> (PIL.Image | None, str)
  - constantes de nivel: TEXT_OK / TEXT_ERR / TEXT_WARN / TEXT_SEC / TEXT_PRI
"""

import os
import re
import json
import shutil
import threading
import unicodedata
import urllib.request
import urllib.parse

import PyPDF2

import header as header_mod
import llm_extract

# ── Miniaturas y OCR opcionales ────────────────────────────────────────────────
# Instalar con: pip install pymupdf pytesseract Pillow
# También requiere Tesseract OCR: https://github.com/UB-Mannheim/tesseract/wiki
try:
    import fitz                          # PyMuPDF
    from PIL import Image
    THUMB_AVAILABLE = True
except ImportError:
    fitz = Image = None
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

# Niveles del callback `log(msg, level)` (los call-sites originales pasan
# colores hex; los conservamos como identificadores opacos para no romper
# nada y los mapeamos a niveles claros en `bridge.py`).
TEXT_PRI  = "#DCE0EA"   # info / neutral
TEXT_SEC  = "#5A6075"   # info secundario
TEXT_OK   = "#00C9A7"   # éxito
TEXT_ERR  = "#FF5F6D"   # error
TEXT_WARN = "#F0A500"   # warning


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

    # ── Autor: herramientas de IA (dejan su nombre en PDFs exportados) ───────
    if any(w in al for w in ("chatgpt", "gpt-", "openai", "claude", "gemini",
                             "copilot", "perplexity", "notebooklm",
                             "deep research", "deepseek")):
        return False

    # ── Título: demasiado corto para ser un título académico real ────────────
    # (placeholders tipo 'Mesa temática –' pasan los filtros anteriores).
    # Se cuentan solo tokens con letras: guiones/números sueltos no son palabras.
    words = [w for w in title.split() if re.search(r'[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]', w)]
    if len(words) < 3 and len(title) < 25:
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

    # PyMuPDF extrae con mejor orden de lectura y maneja ligaduras (ﬂ→fl);
    # PyPDF2 queda como fallback y para cuando PyMuPDF no está instalado.
    doc_fitz = None
    if THUMB_AVAILABLE:
        try:
            doc_fitz = fitz.open(ruta)
        except Exception:
            pass

    for i in range(pages):
        page_text = ""
        if doc_fitz is not None and i < doc_fitz.page_count:
            try:
                page_text = doc_fitz[i].get_text("text") or ""
            except Exception:
                page_text = ""
        if len(page_text.strip()) < 30:
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


def _deaccent(s: str) -> str:
    """'Arqueología' → 'arqueologia'. Crítico para comparar texto extraído
    (que suele perder tildes por encoding/OCR) contra títulos canónicos
    de las APIs (que las traen)."""
    nfkd = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _title_similarity(q: str, api_title: str) -> tuple:
    """
    Devuelve (fracción, count_overlap): fracción de palabras ≥4 chars del
    api_title que aparecen en q, y cantidad absoluta de palabras en común.
    Usar ambas en validación: fracción alta + count mínimo evita matches
    espurios con títulos API cortos (donde 1/2 palabras da ratio 0.5).
    Compara sin tildes en ambos lados.
    """
    q_words = set(re.findall(r'[a-zñ]{4,}', _deaccent(q)))
    t_words = set(re.findall(r'[a-zñ]{4,}', _deaccent(api_title)))
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
        # Último token alfabético como apellido (evita basura tipo 'Argentina)'
        # cuando el registro trae paréntesis o lugares en el nombre).
        fams = []
        for n in names:
            toks = [t.strip("(),.") for t in n.split()]
            toks = [t for t in toks if t and re.fullmatch(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ\-']+", t)]
            if toks:
                fams.append(toks[-1])
        if not fams:
            fams = ["Anónimo"]
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


# ── 6. Cabecera estructural (tipografía) + LLM opcional ──────────────────────
#
# El título de un artículo es casi siempre el texto con la fuente MÁS GRANDE
# de su primera página, en cualquier revista. `header.py` explota esa señal
# universal. Con ese título limpio (más un apellido), la búsqueda en CrossRef/
# OpenAlex pasa de no matchear nunca (candidatos basura del texto plano) a
# resolver la mayoría de los papers sin DOI.

def _verify_via_apis(title: str, first_surname: str, text: str, log) -> str | None:
    """Busca `title` (+autor) en CrossRef y OpenAlex y devuelve el registro
    canónico si pasa las validaciones de similitud/autor/año contra el texto."""
    text_lower = _deaccent(text)
    text_years = set(re.findall(r'\b(?:19|20)\d{2}\b', text))
    author_q = f"&query.author={urllib.parse.quote(first_surname)}" if first_surname else ""

    # CrossRef
    url = (
        "https://api.crossref.org/works?"
        f"query.title={urllib.parse.quote(title[:250])}"
        f"{author_q}&rows=3&select=title,author,issued,score"
    )
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": UA}), timeout=10
        ) as r:
            items = json.loads(r.read()).get("message", {}).get("items", [])
    except Exception:
        items = []

    for item in items:
        api_title = (item.get("title", [""]) or [""])[0]
        if not api_title or not _api_title_is_clean(api_title):
            continue
        sim, common = _title_similarity(title, api_title)
        if sim < 0.70 or common < 3:
            continue
        year = None
        parts = item.get("issued", {}).get("date-parts", [[None]])[0]
        if parts and parts[0]:
            year = str(parts[0])
        if year and text_years and \
           not any(abs(int(year) - int(ty)) <= 1 for ty in text_years):
            continue
        fams = [a.get("family", "") for a in item.get("author", []) if a.get("family")]
        if fams and not any(_deaccent(f) in text_lower for f in fams):
            continue
        author_str = _format_authors_crossref(item.get("author", []))
        log(f"CrossRef confirmó el candidato (sim={sim:.2f})", TEXT_OK)
        return _sanitize(f"{author_str} ({year or 's.f.'}) - {api_title}")

    # OpenAlex
    q = f"{title} {first_surname}".strip()
    url = f"https://api.openalex.org/works?search={urllib.parse.quote(q[:300])}&per-page=3"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": UA}), timeout=10
        ) as r:
            items = json.loads(r.read()).get("results", [])
    except Exception:
        items = []

    for item in items:
        api_title = (item.get("title") or "").strip()
        if not api_title or not _api_title_is_clean(api_title):
            continue
        sim, common = _title_similarity(title, api_title)
        if sim < 0.70 or common < 3:
            continue
        year = item.get("publication_year")
        if year and text_years and \
           not any(abs(int(year) - int(ty)) <= 1 for ty in text_years):
            continue
        authorships = item.get("authorships", [])
        fams = [(a.get("author", {}) or {}).get("display_name", "").split()[-1]
                for a in authorships
                if (a.get("author", {}) or {}).get("display_name")]
        if fams and not any(_deaccent(f) in text_lower for f in fams):
            continue
        author_str = _openalex_format_authors(authorships)
        log(f"OpenAlex confirmó el candidato (sim={sim:.2f})", TEXT_OK)
        return _sanitize(f"{author_str} ({year or 's.f.'}) - {api_title}")

    return None


def _from_header(ruta: str, text: str, log):
    """Extrae cabecera por tipografía y devuelve (verificado, directo).

    - verificado: registro canónico de CrossRef/OpenAlex, o None
    - directo:    nombre armado con la cabecera sola (sin confirmar), o None
    """
    info = header_mod.extract_header(ruta)
    if info is None or info.is_scanned or not info.title:
        return None, None

    log(f"Cabecera por tipografía (pág. {info.page + 1}): "
        f"{info.title[:60]}…" if len(info.title) > 60 else
        f"Cabecera por tipografía (pág. {info.page + 1}): {info.title}",
        TEXT_SEC)

    first_surname = info.surnames[0] if info.surnames else ""
    verified = _verify_via_apis(info.title, first_surname, text, log)

    direct = None
    if info.surnames and info.confidence == "high":
        year = _extract_year_stream(text)
        direct = _sanitize(
            f"{_surnames_to_str(info.surnames)} ({year}) - {info.title}"
        )
    return verified, direct


def _from_llm(ruta: str, text: str, log) -> str | None:
    """Extracción por LLM (si hay backend configurado) + verificación en APIs.

    Se acepta el resultado del LLM aunque las APIs no lo confirmen (p. ej.
    revistas no indexadas): un modelo leyendo la página es más confiable que
    las heurísticas por regex. La verificación, cuando existe, lo canoniza.
    """
    if not llm_extract.available():
        return None

    # Armar la entrada: líneas con tamaño de fuente de las primeras páginas
    # con texto; si el documento es escaneado, mandar la página 1 como imagen.
    header_text = ""
    scanned = False
    if THUMB_AVAILABLE:
        try:
            doc = fitz.open(ruta)
            chunks = []
            for pno in range(min(3, doc.page_count)):
                lines = header_mod._page_lines(doc[pno])
                if lines:
                    chunks.append(f"--- página {pno + 1} ---")
                    chunks.extend(f"[{s}] {t}" for s, _y, t in lines[:60])
            scanned = not chunks
            if scanned and llm_extract.backend() == "api":
                pix = doc[0].get_pixmap(dpi=150)
                png = pix.tobytes("png")
                doc.close()
                log("PDF escaneado: consultando LLM con la imagen de pág. 1…", TEXT_SEC)
                data = llm_extract.extract_from_image(png)
            else:
                doc.close()
                if scanned:
                    return None
                log("Consultando LLM…", TEXT_SEC)
                data = llm_extract.extract_from_text("\n".join(chunks))
        except Exception as e:
            log(f"LLM falló: {e}", TEXT_WARN)
            return None
    else:
        data = llm_extract.extract_from_text(text[:6000])

    if not data or not data.get("title"):
        return None

    surnames = data.get("surnames", [])
    title    = data["title"]
    year     = data.get("year") or _extract_year_stream(text)

    verified = _verify_via_apis(title, surnames[0] if surnames else "", text, log)
    if verified:
        return verified

    log("LLM sin confirmación de APIs — se usa su lectura directa", TEXT_SEC)
    return _sanitize(f"{_surnames_to_str(surnames)} ({year or 's.f.'}) - {title}")


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
    Pipeline completo (v2 — extracción consciente del layout):
      1. DOI → CrossRef                  → papers con DOI embebido (infalible)
      2. Cabecera tipográfica → APIs     → título por tamaño de fuente,
                                            confirmado en CrossRef/OpenAlex
      3. LLM (opcional) → APIs           → parser universal si hay backend
      4. Metadata embebida del PDF       → validada (ebooks bien formados)
      5. Búsqueda clásica por título     → CrossRef/OpenAlex/S2 con líneas
      6. ISBN → Open Library             → libros
      7. Cabecera tipográfica directa    → sin confirmación de API
      8. Heurísticas de texto            → último recurso
    Todos los resultados pasan por _finalize() para normalización final.
    """
    # Extraer texto (casi todos los pasos lo usan)
    text = _extract_text(reader, ruta, log)
    if len(text.strip()) < 10:
        # Documento escaneado sin OCR: el LLM con visión es la última carta.
        result = _from_llm(ruta, text, log)
        if result:
            log("Fuente: LLM (visión)", TEXT_OK)
            return _finalize(result)
        log("No se pudo extraer texto de ninguna página.", TEXT_ERR)
        return "Documento sin texto"

    # 1. DOI exacto → CrossRef
    doi = _find_doi(text)
    if doi:
        log(f"DOI encontrado: {doi}", TEXT_SEC)
        result = _query_crossref(doi)
        if result:
            log("Fuente: CrossRef (DOI)", TEXT_OK)
            return _finalize(result)
        log("CrossRef no respondió al DOI", TEXT_WARN)

    # 2. Cabecera por tipografía → verificación en APIs
    header_verified, header_direct = _from_header(ruta, text, log)
    if header_verified:
        log("Fuente: cabecera tipográfica + API", TEXT_OK)
        return _finalize(header_verified)

    # 3. LLM opcional (lee la página como un humano; cualquier layout)
    result = _from_llm(ruta, text, log)
    if result:
        log("Fuente: LLM", TEXT_OK)
        return _finalize(result)

    # 4. Metadata PDF (validada)
    result = _from_pdf_metadata(reader, log)
    if result:
        log("Fuente: metadata del PDF", TEXT_OK)
        return _finalize(result)

    # 5a. Búsqueda por título en CrossRef (fuzzy, 150M+ obras)
    log("Buscando por título en CrossRef…", TEXT_SEC)
    result = _query_crossref_by_title(text, log)
    if result:
        log("Fuente: CrossRef (búsqueda por título)", TEXT_OK)
        return _finalize(result)

    # 5b. OpenAlex (buena cobertura de revistas regionales/latinoamericanas)
    log("Buscando en OpenAlex…", TEXT_SEC)
    result = _query_openalex_by_title(text, log)
    if result:
        log("Fuente: OpenAlex", TEXT_OK)
        return _finalize(result)

    # 5c. Semantic Scholar
    log("Buscando en Semantic Scholar…", TEXT_SEC)
    result = _query_semantic_scholar_by_title(text, log)
    if result:
        log("Fuente: Semantic Scholar", TEXT_OK)
        return _finalize(result)

    # 6. ISBN → Open Library
    isbn = _find_isbn(text)
    if isbn:
        log(f"ISBN encontrado: {isbn}", TEXT_SEC)
        result = _query_open_library(isbn)
        if result:
            log("Fuente: Open Library (libros)", TEXT_OK)
            return _finalize(result)
        log("Open Library no respondió", TEXT_WARN)

    # 7. Cabecera tipográfica sin confirmación de API (mejor que heurísticas)
    if header_direct:
        log("Fuente: cabecera tipográfica (sin confirmar)", TEXT_WARN)
        return _finalize(header_direct)

    # 8. Heurísticas (último recurso)
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
