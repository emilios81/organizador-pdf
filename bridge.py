"""Puente Python <-> JavaScript para PDF Scholar (pywebview).

La clase `Api` se expone como `window.pywebview.api.*` en el frontend.
Cada método público es invocable desde JS y devuelve una Promise.

Eventos del motor (logs, progreso) se empujan al frontend por
`window.evaluate_js()`, que invoca callbacks registrados en
`window.__pdfBridge` (definidos en `ui/app.jsx`).
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any

# Workers concurrentes para el pipeline de renombrado. La latencia es
# casi toda red (CrossRef / OpenAlex / Semantic Scholar / Open Library),
# así que paralelizar I/O da el mayor speedup. 4 es un punto seguro:
# CrossRef tolera ~50 req/s, OpenAlex 10/s y S2 ~100 req/5min — con 4
# workers ninguno se acerca al rate-limit.
ANALYZE_WORKERS = 4

import PyPDF2
import webview

import engine
from engine import (
    MAX_PDFS,
    TEXT_ERR,
    TEXT_OK,
    TEXT_PRI,
    TEXT_SEC,
    TEXT_WARN,
    get_new_name,
)

# Mapeo de los "colores" del motor a niveles semánticos para el frontend.
_LEVEL_MAP = {
    TEXT_OK:   "ok",
    TEXT_ERR:  "err",
    TEXT_WARN: "warn",
    TEXT_SEC:  "info",
    TEXT_PRI:  "info",
}


def _color_to_level(color: str) -> str:
    return _LEVEL_MAP.get(color, "info")


class FileEntry:
    """Estado de un PDF en cola."""

    def __init__(self, file_id: int, path: str):
        self.id          = file_id
        self.path        = path
        self.orig        = os.path.basename(path)
        self.name        = ""
        self.src         = ""
        self.saved       = False
        self.error       = ""
        self.page_count  = 0
        self._fitz_doc   = None  # PyMuPDF document, abierto solo si está disponible

    def to_dict(self) -> dict:
        return {
            "id":        self.id,
            "orig":      self.orig,
            "path":      self.path,
            "name":      self.name,
            "src":       self.src,
            "saved":     self.saved,
            "error":     self.error,
            "pageCount": self.page_count,
        }

    def close(self):
        if self._fitz_doc is not None:
            try:
                self._fitz_doc.close()
            except Exception:
                pass
            self._fitz_doc = None


class Api:
    """API expuesta a JavaScript via pywebview.

    Métodos sin guion bajo son llamables desde el frontend como
    `window.pywebview.api.<metodo>(...)` y devuelven Promises.
    """

    def __init__(self):
        self._window: webview.Window | None = None
        self.files: dict[int, FileEntry] = {}
        self._next_id = 1
        self._lock    = threading.Lock()
        self._busy    = False

    # ── Setup desde el launcher (no expuesto a JS) ─────────────────────────
    def _attach_window(self, window: webview.Window) -> None:
        self._window = window

    # ── Helpers internos ─────────────────────────────────────────────────────
    def _emit(self, kind: str, payload: dict) -> None:
        """Empuja un evento al frontend.

        Frontend debe definir `window.__pdfBridge.on<Kind>(payload)`.
        """
        if self._window is None:
            return
        method = "on" + kind[:1].upper() + kind[1:]
        try:
            data = json.dumps(payload, ensure_ascii=False, default=str)
            js = (
                f"window.__pdfBridge && typeof window.__pdfBridge.{method} === 'function' "
                f"&& window.__pdfBridge.{method}({data})"
            )
            self._window.evaluate_js(js)
        except Exception:
            traceback.print_exc()

    def _open_fitz(self, entry: FileEntry) -> None:
        if not engine.THUMB_AVAILABLE:
            return
        try:
            entry._fitz_doc = engine.fitz.open(entry.path)
            entry.page_count = entry._fitz_doc.page_count
        except Exception as e:
            self._emit("log", {"level": "warn", "msg": f"PyMuPDF no pudo abrir {entry.orig}: {e}"})

    # ── Métodos llamables desde JS ───────────────────────────────────────────
    def env(self) -> dict:
        """Capacidades disponibles (mostradas en el header)."""
        import llm_extract
        return {
            "ocrAvailable":   engine.OCR_AVAILABLE,
            "thumbAvailable": engine.THUMB_AVAILABLE,
            "llmBackend":     llm_extract.backend_cached(),  # "api"|"cli"|"none"|"unknown"
            "maxFiles":       MAX_PDFS,
        }

    def pick_files(self) -> list[str]:
        """Abre el diálogo nativo de selección múltiple de PDFs."""
        if self._window is None:
            return []
        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=True,
            file_types=("PDF Files (*.pdf)",),
        )
        if not result:
            return []
        # En Windows result es tuple/list de paths.
        return list(result)

    def analyze(self, paths: list[str]) -> list[dict]:
        """Encola los PDFs y arranca el análisis en un hilo aparte.

        Devuelve la representación inicial de los items (con `name=""`).
        El frontend recibe luego eventos `progress` con el resultado.
        """
        if self._busy:
            self._emit("log", {"level": "warn", "msg": "Ya hay un análisis en curso."})
            return []

        if len(paths) > MAX_PDFS:
            self._emit("log", {
                "level": "warn",
                "msg":   f"Límite excedido: se procesarán los primeros {MAX_PDFS} archivos.",
            })
            paths = paths[:MAX_PDFS]

        new_entries: list[FileEntry] = []
        skipped = 0
        with self._lock:
            # No re-encolar archivos que ya están en la cola (mismo path):
            # si el usuario re-selecciona una carpeta con archivos ya
            # analizados, se duplicaban y el guardado en lote los copiaba
            # dos veces.
            queued = {os.path.normcase(os.path.abspath(e.path))
                      for e in self.files.values()}
            for p in paths:
                key = os.path.normcase(os.path.abspath(p))
                if key in queued:
                    skipped += 1
                    continue
                queued.add(key)
                fid = self._next_id
                self._next_id += 1
                entry = FileEntry(fid, p)
                self.files[fid] = entry
                new_entries.append(entry)

        if skipped:
            self._emit("log", {
                "level": "warn",
                "msg":   f"{skipped} archivo(s) ya estaban en la cola — omitidos.",
            })
        if not new_entries:
            return []

        snapshot = [e.to_dict() for e in new_entries]
        self._busy = True
        threading.Thread(
            target=self._analyze_thread,
            args=([e.id for e in new_entries],),
            daemon=True,
        ).start()
        return snapshot

    def _analyze_thread(self, ids: list[int]) -> None:
        """Dispatcher: corre los workers en paralelo y emite el "done" final."""
        try:
            self._emit("log", {
                "level": "info",
                "msg":   f"── Analizando {len(ids)} archivo(s) en paralelo (≤{ANALYZE_WORKERS}) ──",
            })

            with ThreadPoolExecutor(
                max_workers=ANALYZE_WORKERS,
                thread_name_prefix="pdf-analyze",
            ) as pool:
                futures = [pool.submit(self._analyze_one, fid) for fid in ids]
                for fut in futures:
                    try:
                        fut.result()
                    except Exception as e:
                        # Los workers manejan sus propios errores; este
                        # catch es defensivo para excepciones inesperadas.
                        self._emit("log", {"level": "err", "msg": f"Worker excepcion: {e}"})
                        traceback.print_exc()

            self._emit("log", {"level": "ok", "msg": "✓ Análisis completo. Revisá la cola y guardá."})
            self._emit("done", {})
        finally:
            self._busy = False

    def _analyze_one(self, fid: int) -> None:
        """Procesa un único PDF — puede correr concurrente con otros.

        Es seguro porque cada worker:
          - Abre su propio file handle (PyPDF2 reader local al thread)
          - Trabaja sobre su propio FileEntry (sin compartir estado con otros)
          - Las llamadas HTTP (urllib) y los logs (`window.evaluate_js`)
            son thread-safe.
        """
        entry = self.files.get(fid)
        if entry is None:
            return

        self._emit("log", {"level": "info", "msg": f"── {entry.orig}"})
        self._emit("progress", {"id": fid, "status": "analyzing"})

        # Capturamos la "fuente" desde los logs del motor.
        src_holder = {"value": ""}

        def log(msg: str, color: str = TEXT_PRI, _h=src_holder, _fid=fid) -> None:
            level = _color_to_level(color)
            if "Fuente:" in msg:
                _h["value"] = msg.split("Fuente:", 1)[-1].strip()
            self._emit("log", {"level": level, "msg": msg, "fileId": _fid})

        try:
            with open(entry.path, "rb") as f:
                reader = PyPDF2.PdfReader(f)
                if not reader.pages:
                    raise ValueError("PDF sin páginas.")
                new_name = get_new_name(reader, entry.path, log)

            entry.name = new_name
            entry.src  = src_holder["value"] or "heurística"

            # Abrir fitz para preview / texto bajo demanda
            self._open_fitz(entry)

            self._emit("log", {
                "level":  "ok",
                "msg":    f"✓ {entry.orig} → {new_name}",
                "fileId": fid,
            })
            self._emit("progress", {
                "id":        fid,
                "status":    "done",
                "name":      new_name,
                "src":       entry.src,
                "pageCount": entry.page_count,
            })

        except Exception as e:
            msg = str(e) or e.__class__.__name__
            entry.error = msg
            self._emit("log", {
                "level":  "err",
                "msg":    f"Error en {entry.orig}: {msg}",
                "fileId": fid,
            })
            self._emit("progress", {
                "id":     fid,
                "status": "error",
                "error":  msg,
            })

    def list_files(self) -> list[dict]:
        return [self.files[k].to_dict() for k in sorted(self.files.keys())]

    def update_name(self, file_id: int, name: str) -> bool:
        entry = self.files.get(int(file_id))
        if not entry:
            return False
        entry.name  = name
        entry.saved = False
        return True

    def get_text(self, file_id: int, page: int = 1) -> str:
        entry = self.files.get(int(file_id))
        if not entry or not entry._fitz_doc:
            return ""
        try:
            idx = max(0, min(int(page) - 1, entry._fitz_doc.page_count - 1))
            return entry._fitz_doc[idx].get_text("text") or ""
        except Exception as e:
            return f"(Error extrayendo texto: {e})"

    def render_page(self, file_id: int, page: int = 1, width: int = 720) -> str:
        """Renderiza una página como PNG y la devuelve como data URL."""
        entry = self.files.get(int(file_id))
        if not entry or not entry._fitz_doc:
            return ""
        try:
            doc = entry._fitz_doc
            idx = max(0, min(int(page) - 1, doc.page_count - 1))
            pg  = doc[idx]
            zoom = max(0.4, min(3.0, float(width) / pg.rect.width))
            pix  = pg.get_pixmap(matrix=engine.fitz.Matrix(zoom, zoom), alpha=False)
            png_bytes = pix.tobytes("png")
            b64 = base64.b64encode(png_bytes).decode("ascii")
            return f"data:image/png;base64,{b64}"
        except Exception as e:
            self._emit("log", {"level": "err", "msg": f"render_page falló: {e}"})
            return ""

    def save_one(self, file_id: int) -> dict:
        """Abre el diálogo "Guardar como…" para un archivo y lo copia."""
        entry = self.files.get(int(file_id))
        if not entry or self._window is None:
            return {"saved": False, "error": "Archivo inválido"}

        nombre = (entry.name or "").strip()
        if not nombre:
            return {"saved": False, "error": "Nombre vacío"}
        if not nombre.lower().endswith(".pdf"):
            nombre += ".pdf"

        result = self._window.create_file_dialog(
            webview.SAVE_DIALOG,
            directory=os.path.dirname(entry.path),
            save_filename=nombre,
            file_types=("PDF Files (*.pdf)",),
        )
        if not result:
            return {"saved": False, "cancelled": True}

        dest = result if isinstance(result, str) else result[0]
        try:
            shutil.copy2(entry.path, dest)
            entry.saved = True
            self._emit("log", {"level": "ok", "msg": f"✓ Guardado: {os.path.basename(dest)}"})
            return {"saved": True, "dest": dest}
        except Exception as e:
            self._emit("log", {"level": "err", "msg": f"Error guardando: {e}"})
            return {"saved": False, "error": str(e)}

    def save_all(self) -> dict:
        """Guardado en lote: se elige UNA carpeta destino y se copian todos
        los pendientes de una vez (sin un diálogo por archivo)."""
        pending = [e for e in self.files.values() if e.name and not e.saved and not e.error]
        if not pending:
            return {"saved": 0, "total": 0}
        if self._window is None:
            return {"saved": 0, "total": len(pending), "error": "Sin ventana"}

        result = self._window.create_file_dialog(
            webview.FOLDER_DIALOG,
            directory=os.path.dirname(pending[0].path),
        )
        if not result:
            return {"saved": 0, "total": len(pending), "cancelled": True}
        dest_dir = result if isinstance(result, str) else result[0]

        self._emit("log", {
            "level": "info",
            "msg":   f"Guardando {len(pending)} archivo(s) en {dest_dir}…",
        })

        saved = 0
        for entry in pending:
            nombre = entry.name.strip()
            if not nombre.lower().endswith(".pdf"):
                nombre += ".pdf"
            dest = os.path.join(dest_dir, nombre)
            # Colisiones (otro archivo del lote o uno preexistente con el
            # mismo nombre): sufijo ' (2)', ' (3)', …
            base, ext = os.path.splitext(dest)
            n = 2
            collided = os.path.exists(dest)
            while os.path.exists(dest):
                dest = f"{base} ({n}){ext}"
                n += 1
            if collided:
                self._emit("log", {
                    "level": "warn",
                    "msg":   (f"⚠ {entry.orig}: ya existe un PDF con el nombre "
                              f"«{nombre}» — posible duplicado; guardado como "
                              f"{os.path.basename(dest)}"),
                })
            try:
                shutil.copy2(entry.path, dest)
                entry.saved = True
                saved += 1
                self._emit("progress", {"id": entry.id, "status": "saved"})
                self._emit("log", {"level": "ok", "msg": f"✓ {os.path.basename(dest)}"})
            except Exception as e:
                self._emit("log", {
                    "level": "err",
                    "msg":   f"Error guardando {entry.orig}: {e}",
                })

        self._emit("log", {
            "level": "ok" if saved == len(pending) else "warn",
            "msg":   f"Guardado en lote: {saved}/{len(pending)} archivo(s).",
        })
        return {"saved": saved, "total": len(pending), "dest": dest_dir}

    def remove_file(self, file_id: int) -> bool:
        fid = int(file_id)
        entry = self.files.pop(fid, None)
        if entry is not None:
            entry.close()
            return True
        return False

    def clear_all(self) -> bool:
        for e in list(self.files.values()):
            e.close()
        self.files.clear()
        self._emit("log", {"level": "info", "msg": "Cola limpiada."})
        return True
