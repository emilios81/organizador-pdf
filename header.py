"""Extracción estructural de cabecera usando layout y tipografía (PyMuPDF).

Idea central: en casi cualquier revista, el TÍTULO es el texto con la fuente
más grande de la primera página del artículo, y los AUTORES están en un
bloque cercano (debajo o arriba) con fuente intermedia. Esta señal es
universal y no depende de cómo cada revista ordene su cabecera — que es
justamente donde fallan las heurísticas basadas en texto plano.

Expone:
  - extract_header(ruta) -> HeaderInfo | None
    HeaderInfo.title / .authors_line / .surnames / .page / .confidence

No hace red ni OCR: si las páginas no tienen texto, devuelve None con
is_scanned=True para que el caller decida (LLM visión / OCR / abortar).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

# Páginas a inspeccionar buscando la cabecera del artículo (salta tapas,
# páginas de créditos de libros, índices de revista).
MAX_HEADER_PAGES = 4

# Nombre propio: 'Nombre [I.]* Apellido [SegundoApellido]'
_NAME_RE = re.compile(
    r'[A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]+'
    r'(?:\s+[A-ZÁÉÍÓÚÜÑ]\.)*'
    r'\s+[A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]+'
    r'(?:\s+[A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]+)?'
)

# Nombre en VERSALES: 'C. BLANCO PÉREZ', 'JUAN PÉREZ' (común en revistas).
_CAPS_NAME_RE = re.compile(
    r'(?:[A-ZÁÉÍÓÚÜÑ]\.\s*)*[A-ZÁÉÍÓÚÜÑ]{3,}(?:\s+[A-ZÁÉÍÓÚÜÑ]{2,}){1,3}'
)

_AFFILIATION_RE = re.compile(
    r'universidad|university|facultad|faculty|departamento|department|'
    r'instituto|institute|conicet|museo|museum|escuela|school|'
    r'centro\s+de|c[aá]tedra|laboratorio',
    re.IGNORECASE
)

_NOISE_RE = re.compile(
    r'issn|doi\s*:|doi\.org|https?://|www\.|copyright|©|'
    r'derechos\s+reservados|all\s+rights|recibido|aceptado|received|accepted',
    re.IGNORECASE
)

# Marcadores de que la página ES la cabecera de un artículo (y no una tapa).
_ARTICLE_MARKER_RE = re.compile(
    r'\b(resumen|abstract|palabras\s+clave|keywords?|introducci[oó]n|'
    r'introduction|summary)\b',
    re.IGNORECASE
)

# Palabras que delatan página de créditos/legales de un libro.
_CREDITS_PAGE_RE = re.compile(
    r'coordinaci[oó]n\s+editorial|dise[ñn]o\s+de\s+tapa|'
    r'hecho\s+el\s+dep[oó]sito|queda\s+prohibida|impreso\s+en|'
    r'printed\s+in|primera\s+edici[oó]n|cataloga[cd]i[oó]n',
    re.IGNORECASE
)


@dataclass
class HeaderInfo:
    title: str = ""
    authors_line: str = ""
    surnames: list = field(default_factory=list)
    page: int = 0            # página (0-based) donde se encontró la cabecera
    confidence: str = "low"  # "high" si título por fuente + autores con nombre
    is_scanned: bool = False


def _page_lines(page) -> list:
    """Devuelve [(size, y, text)] de las líneas de la página, en orden visual."""
    out = []
    try:
        d = page.get_text("dict")
    except Exception:
        return out
    for block in d.get("blocks", []):
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            txt = "".join(s.get("text", "") for s in spans).strip()
            if not txt:
                continue
            size = max(round(s.get("size", 0), 1) for s in spans)
            y = round(line.get("bbox", (0, 0, 0, 0))[1], 1)
            out.append((size, y, txt))
    out.sort(key=lambda t: t[1])
    return out


def _body_size(lines: list) -> float:
    """Tamaño de fuente 'de cuerpo': el más frecuente ponderado por chars."""
    counter = Counter()
    for size, _y, txt in lines:
        counter[size] += len(txt)
    return counter.most_common(1)[0][0] if counter else 10.0


def _is_noise_line(txt: str) -> bool:
    if len(txt.strip()) < 3:
        return True
    if _NOISE_RE.search(txt):
        return True
    if re.match(r'^[\d\W]+$', txt):          # solo números/símbolos
        return True
    # Cita de revista: 'Vol. 27(1): 25-41'
    if re.search(r'\b\d{1,3}\s*[\(:]\s*\d', txt) and \
       re.search(r'\d+\s*[-–]\s*\d+', txt):
        return True
    # Encabezado corrido de revista: 'COMECHINGONIA 29 (3)'
    if re.search(r'\b\d{1,3}\s*\(\d{1,3}\)', txt):
        return True
    return False


# Palabras que no inician un nombre de persona (evita que 'El Depósito' o
# 'La Zaranda' matcheen como nombre propio).
_NOT_NAME_STARTERS = {
    "el", "la", "los", "las", "un", "una", "unos", "unas",
    "de", "del", "al", "en", "y", "the", "a", "an", "of", "in",
}


def _looks_like_person_line(txt: str) -> bool:
    """Línea de autores en caja mixta ('Víctor Sierpe1, Cristóbal Palacios2').

    A diferencia de `_looks_like_authors`, exige nombres en mixto (NO acepta
    versales) y una línea corta, porque se usa como ANCLA para distinguir
    título de autores en layouts donde el título va en ALL-CAPS al mismo
    cuerpo que el texto.
    """
    if len(txt) < 5 or len(txt) > 120 or len(txt.split()) > 8:
        return False
    if '@' in txt or _AFFILIATION_RE.search(txt) or _NOISE_RE.search(txt):
        return False
    if _caps_ratio(txt) > 0.6:
        return False
    m = _NAME_RE.search(txt)
    if not m:
        return False
    return m.group(0).split()[0].lower() not in _NOT_NAME_STARTERS


def _looks_like_authors(txt: str) -> bool:
    """La línea contiene nombres de persona y no es afiliación/ruido."""
    if len(txt) < 5 or len(txt) > 300:
        return False
    if '@' in txt or _AFFILIATION_RE.search(txt) or _NOISE_RE.search(txt):
        return False
    return bool(_NAME_RE.search(txt) or _CAPS_NAME_RE.search(txt))


def _surnames_from_line(txt: str) -> list:
    """Extrae apellidos (último token de cada nombre) de una línea de autores.

    Corta la línea en las marcas de afiliación por superíndice que PyMuPDF
    baja a texto plano ('Taboada1,2', 'Pérez*').
    """
    surnames = []
    # Normalizar separadores: ' y ', ' and ', ' e ', ';', '&'
    parts = re.split(r',|;|\s+y\s+|\s+and\s+|\s+e\s+|&', txt)
    for p in parts:
        p = re.sub(r'[\d\*†‡\.]+$', '', p.strip()).strip()
        if not p:
            continue
        m = _NAME_RE.search(p)
        if m:
            surnames.append(m.group(0).split()[-1])
            continue
        m = _CAPS_NAME_RE.search(p)
        if m:
            # 'C. BLANCO PÉREZ' → 'Blanco Pérez' → último token capitalizado
            toks = [t for t in m.group(0).split() if not re.match(r'^[A-ZÁÉÍÓÚÜÑ]\.$', t)]
            if toks:
                surnames.append(toks[-1].capitalize())
    # dedup conservando orden
    seen, out = set(), []
    for s in surnames:
        if s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def _caps_ratio(txt: str) -> float:
    """Proporción de letras en mayúscula (para detectar títulos ALL-CAPS)."""
    letters = [c for c in txt if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if c.isupper()) / len(letters)


def _first_group_by_gap(cands: list, size: float) -> list:
    """Agrupa líneas candidatas consecutivas cortando en huecos verticales
    anómalos (> 2× el espaciado interno observado). Evita pegar el título
    con su traducción al inglés, que suele venir a doble espacio."""
    group, gaps = [], []
    for y, t in cands:
        if group:
            gap = y - group[-1][0]
            typical = min(gaps) if gaps else size * 1.6
            if gap > max(typical * 1.8, size * 1.6):
                break
            gaps.append(gap)
        group.append((y, t))
    return group


def _header_from_page(page, pno: int) -> HeaderInfo | None:
    lines = _page_lines(page)
    if not lines:
        return None

    body = _body_size(lines)
    page_text = " ".join(t for _s, _y, t in lines)

    # Página de créditos de libro → saltear
    if _CREDITS_PAGE_RE.search(page_text):
        return None

    # ── Candidatas a título, dos señales en orden de fuerza ─────────────────
    # (a) tipografía: líneas con fuente mayor al cuerpo
    big = [(s, y, t) for s, y, t in lines
           if s >= body * 1.15 and s >= body + 1.2 and not _is_noise_line(t)]

    group, max_size = [], body
    if big:
        # Grupo de líneas consecutivas con el tamaño MÁXIMO (tolerancia 0.5pt)
        max_size = max(s for s, _y, _t in big)
        cands = [(y, t) for s, y, t in big if abs(s - max_size) <= 0.5]
        group = _first_group_by_gap(cands, max_size)

    if not group:
        # (b) fallback: título en ALL-CAPS al mismo cuerpo que el texto.
        # Un título en versales es indistinguible de una línea de autores por
        # patrón, así que primero se ancla la línea de autores (en caja mixta,
        # que SÍ es distinguible) y el título es el bloque de versales
        # inmediatamente ANTERIOR a esa ancla.
        anchor_y = None
        for s, y, t in lines:
            # Los autores nunca van en fuente menor que el cuerpo del texto
            # (los encabezados de evento/revista sí suelen ir más chicos).
            if s >= body - 0.1 and _looks_like_person_line(t):
                anchor_y = y
                break
        if anchor_y is not None:
            cands = [(y, t) for s, y, t in lines
                     if y < anchor_y and len(t) >= 12
                     and _caps_ratio(t) >= 0.7 and not _is_noise_line(t)]
            group = _first_group_by_gap(cands, body)

    if not group:
        return None

    title = re.sub(r'\s+', ' ', " ".join(t for _y, t in group)).strip()
    if len(title) < 12 or len(title.split()) < 3:
        return None
    title_top = group[0][0]
    title_bottom = group[-1][0]

    # ── Autores: líneas "con nombres" DESPUÉS del título (lo usual) ─────────
    # Se juntan las consecutivas (cada autor suele ir en su propia línea).
    # Dos pasadas: primero caja mixta (señal fuerte, salta el título traducido
    # en versales); si no hay, versales (portadas tipo 'SVEND AAGE BUUS').
    authors_parts = []
    for matcher in (_looks_like_person_line, _looks_like_authors):
        last_y = None
        for s, y, t in lines:
            if y <= title_bottom or s > max_size + 0.5:
                continue
            if authors_parts and (y - last_y) > max(s, body) * 2.5:
                break
            if matcher(t):
                authors_parts.append(t)
                last_y = y
            elif authors_parts:
                break
        if authors_parts:
            break
    if not authors_parts:
        # Algunas revistas ponen los autores ARRIBA del título.
        for s, y, t in reversed(lines):
            if y >= title_top:
                continue
            if _looks_like_authors(t):
                authors_parts.append(t)
                break
    authors_line = ", ".join(authors_parts)

    surnames = _surnames_from_line(authors_line) if authors_line else []

    # ── Confianza ────────────────────────────────────────────────────────────
    # Alta si hay autores con nombre propio Y la página parece cabecera de
    # artículo (marcador Resumen/Abstract), primera página, o una portada de
    # libro (pocas líneas, tipografía grande).
    has_marker = bool(_ARTICLE_MARKER_RE.search(page_text))
    is_title_page = len(lines) <= 15
    confidence = "high" if (surnames and (has_marker or pno == 0 or is_title_page)) \
                 else "low"

    return HeaderInfo(
        title=title,
        authors_line=authors_line,
        surnames=surnames,
        page=pno,
        confidence=confidence,
    )


def extract_header(ruta: str) -> HeaderInfo | None:
    """Busca la cabecera del artículo en las primeras páginas del PDF.

    Devuelve HeaderInfo con is_scanned=True si ninguna página inspeccionada
    tiene texto (documento escaneado sin OCR).
    """
    if fitz is None:
        return None
    try:
        doc = fitz.open(ruta)
    except Exception:
        return None
    try:
        any_text = False
        for pno in range(min(MAX_HEADER_PAGES, doc.page_count)):
            page = doc[pno]
            if len((page.get_text("text") or "").strip()) < 30:
                continue
            any_text = True
            info = _header_from_page(page, pno)
            if info:
                return info
        if not any_text:
            return HeaderInfo(is_scanned=True)
        return None
    finally:
        doc.close()
