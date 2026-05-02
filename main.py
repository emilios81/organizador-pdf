"""PDF Scholar — launcher de pywebview.

Carga el frontend (`ui/index.html`) en una ventana nativa con WebView2 y
expone el motor de renombrado a JavaScript a través de `bridge.Api`.

Para arrancar:
    python main.py

El motor de renombrado está en `engine.py`; la API JS en `bridge.py`.
"""

from __future__ import annotations

import os
import sys
import tempfile

import webview

from bridge import Api


HERE     = os.path.dirname(os.path.abspath(__file__))
# `url=` debe ser RELATIVO para que pywebview use su servidor HTTP interno
# (caso contrario carga vía file:// y los <script src="..."> no resuelven
# por CORS de WebView2). Por eso no lo pasamos absoluto.
UI_INDEX_REL = os.path.join("ui", "index.html")


def main() -> None:
    abs_index = os.path.join(HERE, UI_INDEX_REL)
    if not os.path.exists(abs_index):
        print(f"[!] Falta {abs_index}", file=sys.stderr)
        sys.exit(1)

    # pywebview busca el url relativo desde cwd; nos aseguramos de cwd correcto.
    os.chdir(HERE)

    api = Api()
    window = webview.create_window(
        title="PDF Scholar",
        url=UI_INDEX_REL,
        js_api=api,
        width=1280,
        height=820,
        min_size=(960, 660),
        background_color="#080b11",
    )
    api._attach_window(window)

    # `private_mode=False` evita el bug de recursión en
    # `window.native.AccessibilityObject.*` que aparece con WebView2 +
    # pythonnet en algunas configuraciones Windows.
    # `http_server=True` sirve los archivos locales (HTML, JSX) por HTTP
    # interno, lo que arregla la carga de scripts externos en WebView2.
    storage = os.path.join(tempfile.gettempdir(), "pdf-scholar-webview")
    os.makedirs(storage, exist_ok=True)

    webview.start(
        debug=False,
        http_server=True,
        private_mode=False,
        storage_path=storage,
    )


if __name__ == "__main__":
    main()
