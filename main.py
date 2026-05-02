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

import webview

from bridge import Api


HERE     = os.path.dirname(os.path.abspath(__file__))
UI_INDEX = os.path.join(HERE, "ui", "index.html")


def main() -> None:
    if not os.path.exists(UI_INDEX):
        print(f"[!] Falta {UI_INDEX}", file=sys.stderr)
        sys.exit(1)

    api = Api()
    window = webview.create_window(
        title="PDF Scholar",
        url=UI_INDEX,
        js_api=api,
        width=1280,
        height=820,
        min_size=(960, 660),
        background_color="#080b11",
    )
    api._attach_window(window)

    # En Windows pywebview usa Edge WebView2 (Chromium).
    webview.start(debug=False)


if __name__ == "__main__":
    main()
