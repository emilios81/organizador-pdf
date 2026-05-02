"""Extrae el logo LATDAA del HTML embebido en Downloads y lo guarda como
PNG con fondo transparente en ui/assets/latdaa.png. Script de uso único."""
import base64
import re
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image

SRC  = Path(r"C:/Users/Usuario/Downloads/LATDAA.html")
DEST = Path(__file__).parent / "ui" / "assets" / "latdaa.png"

html = SRC.read_text(encoding="utf-8", errors="replace")

# Primera imagen base64 del HTML (el logo)
m = re.search(r'src="data:image/(jpeg|png|jpg);base64,([A-Za-z0-9+/=\s]+?)"', html)
if not m:
    print("No image found in HTML", file=sys.stderr)
    sys.exit(1)

mime = m.group(1)
b64  = re.sub(r"\s+", "", m.group(2))
data = base64.b64decode(b64)
print(f"Imagen embebida: {mime}, {len(data):,} bytes")

img = Image.open(BytesIO(data)).convert("RGBA")
print(f"Tamaño original: {img.size}")

# Quitar el fondo. El JPEG comprimido tiene fondo crema (~244,243,239),
# así que no podemos usar solo "qué tan blanco". Usamos dos señales:
#   - claridad: max(R,G,B) — fondo claro vs. logo oscuro/saturado
#   - saturación: max-min — fondo es desaturado, los colores del logo no
#
# Un pixel es FONDO sólo si es claro Y desaturado (gris claro / crema).
# Con eso, el rojo "L" (sat alta) y el beige lagartija (claridad menor)
# se preservan, mientras que el crema del papel (sat ~5, claridad >230)
# se vuelve transparente.
import numpy as np
arr = np.array(img, dtype=np.int16)           # int16 para restas con signo
rgb = arr[:, :, :3]
luma_max = rgb.max(axis=2)
luma_min = rgb.min(axis=2)
sat = luma_max - luma_min                     # saturación cruda

# Condición de "fondo": claro y desaturado.
# Anti-alias: alpha lineal entre opacidad plena y transparencia plena.
LIGHT_OPAQUE = 200    # claridad <= esto → 100% opaco (logo)
LIGHT_GONE   = 245    # claridad >= esto + bajo sat → 0% (puro fondo)
SAT_HIGH     = 25     # sat >= esto → siempre opaco (rojo de la "L")

# Solo los pixeles desaturados (sat <= SAT_BG_MAX) son candidatos a fondo;
# para esos, el alpha es proporcional a qué tan claros son. Pixeles
# saturados (rojo, beige) nunca se desvanecen.
SAT_BG_MAX  = 15
LIGHT_OPAQUE = 180
LIGHT_GONE   = 244

is_bg_like = (sat <= SAT_BG_MAX).astype(np.float32)
clarity_ratio = np.clip((luma_max - LIGHT_OPAQUE) / (LIGHT_GONE - LIGHT_OPAQUE), 0, 1)
fade = is_bg_like * clarity_ratio
alpha = ((1.0 - fade) * 255).astype(np.uint8)

arr_out = np.zeros_like(arr, dtype=np.uint8)
arr_out[:, :, :3] = rgb.astype(np.uint8)
arr_out[:, :, 3]  = alpha
img = Image.fromarray(arr_out, mode="RGBA")
total = arr.shape[0] * arr.shape[1]
fully_transparent = int((alpha < 5).sum())
mostly_transparent = int((alpha < 64).sum())
print(f"Píxeles ~totalmente transparentes: {fully_transparent:,} / {total:,} ({fully_transparent*100//total}%)")
print(f"Píxeles bastante transparentes:    {mostly_transparent:,} / {total:,} ({mostly_transparent*100//total}%)")

# Recortar bordes transparentes para que el logo ocupe todo el cuadro útil.
bbox = img.getbbox()
if bbox:
    img = img.crop(bbox)
    print(f"Bbox tras recorte: {img.size}")

DEST.parent.mkdir(parents=True, exist_ok=True)
img.save(DEST, format="PNG", optimize=True)
print(f"Guardado: {DEST}  ({DEST.stat().st_size:,} bytes)")
