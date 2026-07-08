"""Extractor bibliográfico por LLM (opcional).

Un modelo de lenguaje resuelve el problema de fondo del nomenclador: cada
revista diagrama su cabecera distinto y las heurísticas por regex no
escalan. El LLM lee la primera página (texto con tamaños de fuente, o la
imagen si el PDF es escaneado) y devuelve autores/año/título en JSON.

Es 100% OPCIONAL: si no hay backend configurado, `available()` da False y
el motor sigue con el pipeline clásico. Backends, en orden:

  1. API de Anthropic — poner la key en `config.json` junto al programa:
         { "anthropic_api_key": "sk-ant-..." }
     o en la variable de entorno ANTHROPIC_API_KEY.
  2. CLI de Claude Code (`claude -p`) si está instalado y logueado.

Usa claude-haiku (rápido y barato: fracciones de centavo por PDF).
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

DEFAULT_MODEL = "claude-haiku-4-5"
API_URL = "https://api.anthropic.com/v1/messages"

PROMPT = (
    "Sos un bibliotecario experto. Te paso la cabecera de la primera página "
    "de un documento académico (paper, capítulo, libro, tesis o reseña). "
    "Cada línea viene precedida por su tamaño de fuente entre corchetes.\n\n"
    "Identificá los AUTORES del trabajo (no editores de la revista, no "
    "afiliaciones), el AÑO de publicación y el TÍTULO completo del trabajo "
    "(no el nombre de la revista ni el encabezado del número).\n\n"
    "Respondé SOLO un JSON válido, sin texto adicional, con esta forma:\n"
    '{"surnames": ["ApellidoDelPrimerAutor", "..."], "year": "2020", '
    '"title": "Título completo"}\n'
    'Si un dato no está, usá [] para surnames, "" para year/title.\n\n'
    "DOCUMENTO:\n"
)

VISION_PROMPT = (
    "Sos un bibliotecario experto. Esta imagen es la primera página de un "
    "documento académico escaneado. Identificá autores, año y título del "
    "trabajo. Respondé SOLO un JSON válido: "
    '{"surnames": ["Apellido", "..."], "year": "2020", "title": "Título"}. '
    'Si un dato no se lee, usá [] o "".'
)


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _api_key() -> str | None:
    return _load_config().get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")


def _model() -> str:
    return _load_config().get("llm_model", DEFAULT_MODEL)


def _find_claude_cli() -> str | None:
    """Busca el CLI de Claude Code (PATH o instalación de la app de escritorio)."""
    path = shutil.which("claude")
    if path:
        return path
    base = os.path.join(os.environ.get("APPDATA", ""), "Claude", "claude-code")
    if os.path.isdir(base):
        versions = sorted(os.listdir(base), reverse=True)
        for v in versions:
            cand = os.path.join(base, v, "claude.exe")
            if os.path.exists(cand):
                return cand
    return None


# Cache del estado del backend (se resuelve una sola vez por sesión).
# La resolución puede tardar (prueba el CLI con un subproceso), así que se
# precalienta en un hilo daemon al importar; `backend()` bloquea hasta tener
# respuesta y `backend_cached()` devuelve el estado sin bloquear (para la UI).
import threading

_BACKEND: str | None = None   # "api" | "cli" | "none"
_RESOLVE_LOCK = threading.Lock()


def _resolve_backend() -> str:
    global _BACKEND
    with _RESOLVE_LOCK:
        if _BACKEND is not None:
            return _BACKEND
        if _api_key():
            _BACKEND = "api"
            return _BACKEND
        cli = _find_claude_cli()
        if cli:
            try:
                r = subprocess.run(
                    [cli, "-p", "Respondé solo: ok", "--model", "haiku"],
                    capture_output=True, text=True, timeout=60,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if r.returncode == 0:
                    _BACKEND = "cli"
                    return _BACKEND
            except Exception:
                pass
        _BACKEND = "none"
        return _BACKEND


def backend() -> str:
    """'api', 'cli' o 'none'. Bloquea hasta resolver (cacheado)."""
    return _BACKEND if _BACKEND is not None else _resolve_backend()


def backend_cached() -> str:
    """Estado sin bloquear: 'unknown' si la resolución sigue en curso."""
    return _BACKEND if _BACKEND is not None else "unknown"


def available() -> bool:
    return backend() != "none"


# Precalentar en segundo plano para no frenar el arranque de la UI.
threading.Thread(target=_resolve_backend, daemon=True).start()


def _parse_json_reply(raw: str) -> dict | None:
    """Extrae el primer objeto JSON de la respuesta del modelo."""
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None
    surnames = [s for s in data.get("surnames", []) if isinstance(s, str) and s.strip()]
    title = (data.get("title") or "").strip()
    year = str(data.get("year") or "").strip()
    if not re.fullmatch(r'(19|20)\d{2}', year):
        year = ""
    if not title and not surnames:
        return None
    return {"surnames": surnames, "year": year, "title": title}


def _call_api(content_blocks: list) -> str | None:
    req = urllib.request.Request(
        API_URL,
        data=json.dumps({
            "model": _model(),
            "max_tokens": 500,
            "messages": [{"role": "user", "content": content_blocks}],
        }).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": _api_key(),
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            data = json.loads(r.read())
        return "".join(b.get("text", "") for b in data.get("content", []))
    except Exception:
        return None


def _call_cli(prompt: str) -> str | None:
    cli = _find_claude_cli()
    if not cli:
        return None
    try:
        r = subprocess.run(
            [cli, "-p", prompt, "--model", "haiku"],
            capture_output=True, text=True, timeout=120, encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


def extract_from_text(header_text: str) -> dict | None:
    """header_text: líneas '[tamaño] texto' de la(s) primera(s) página(s)."""
    if not available():
        return None
    prompt = PROMPT + header_text[:6000]
    if backend() == "api":
        raw = _call_api([{"type": "text", "text": prompt}])
    else:
        raw = _call_cli(prompt)
    return _parse_json_reply(raw) if raw else None


def extract_from_image(png_bytes: bytes) -> dict | None:
    """Para PDFs escaneados: manda la página 1 como imagen (solo backend API)."""
    if backend() != "api":
        return None
    blocks = [
        {"type": "image", "source": {
            "type": "base64", "media_type": "image/png",
            "data": base64.b64encode(png_bytes).decode("ascii"),
        }},
        {"type": "text", "text": VISION_PROMPT},
    ]
    raw = _call_api(blocks)
    return _parse_json_reply(raw) if raw else None
