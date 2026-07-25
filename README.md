# Nomenclador Académico (PDF Scholar)

Renombra automáticamente PDFs académicos —papers, capítulos, libros— usando el
formato **`Autor (Año) - Título`**, a partir de lo que el propio documento y las
bases bibliográficas públicas saben sobre él.

La idea es simple: uno acumula archivos llamados `s41586-024-07487-w.pdf`,
`Microsoft Word - final2.pdf` o `descarga (3).pdf`, y termina con una carpeta
imposible de leer. El programa los abre, averigua qué son y propone un nombre
legible que uno puede corregir antes de guardar.

## Cómo funciona

El motor (`engine.py`) prueba ocho estrategias en cascada y se queda con la
primera que da un resultado confiable:

| # | Estrategia | Sirve para |
|---|---|---|
| 1 | DOI embebido → CrossRef | Papers con DOI. Prácticamente infalible |
| 2 | Cabecera tipográfica → verificación en API | Título detectado por tamaño de fuente y confirmado contra CrossRef/OpenAlex |
| 3 | LLM (opcional) → verificación en API | Parser universal, incluye visión para escaneados |
| 4 | Metadata embebida del PDF | Ebooks bien formados |
| 5 | Búsqueda clásica por título | CrossRef, OpenAlex, Semantic Scholar |
| 6 | ISBN → Open Library | Libros |
| 7 | Cabecera tipográfica directa | Sin confirmación externa |
| 8 | Heurísticas de texto | Último recurso |

Todo resultado pasa por un post-procesador que normaliza títulos en
MAYÚSCULAS, colapsa espacios, recorta títulos desmedidos y saca los caracteres
que Windows no admite en nombres de archivo.

Detalles de implementación que conviene saber:

- Se analizan hasta **8 páginas** por archivo y hasta **50 PDFs** por tanda.
- El análisis corre en **4 hilos concurrentes**.
- Si el PDF no tiene capa de texto, se intenta **OCR** con Tesseract; si tampoco
  hay Tesseract, queda el LLM con visión como última carta.
- El archivo original **nunca se modifica ni se mueve**: al guardar se copia con
  el nombre nuevo a la carpeta destino que elijas.

## Instalación

Requiere Python 3.10 o superior.

```bash
pip install -r requirements.txt
```

Dependencias externas, ambas opcionales:

- **Tesseract OCR** — habilita el reconocimiento de PDFs escaneados.
  [Instalador para Windows](https://github.com/UB-Mannheim/tesseract/wiki)
- **WebView2 Runtime** — el motor de la ventana. Ya viene con Windows 11 y con
  las versiones recientes de Windows 10.

Hace falta conexión a internet: el pipeline consulta CrossRef, OpenAlex,
Semantic Scholar y Open Library, y la interfaz carga React desde CDN.

## Uso

```bash
python main.py
```

En Windows también sirve hacer doble clic en `Abrir PDF Scholar.bat`, que abre
la aplicación sin consola.

El flujo dentro de la app: agregás PDFs con el selector de archivos, el motor
los analiza y propone un nombre para cada uno, revisás la propuesta contra la
miniatura o el texto de la página, editás lo que haga falta, y guardás de a uno
o todos juntos en una carpeta destino.

## Extractor por IA (opcional)

El paso 3 del pipeline usa un modelo de lenguaje para los casos que las demás
estrategias no resuelven. Está desactivado por defecto y **el programa funciona
completo sin él**.

Para activarlo, copiá `config.json.example` como `config.json` y completá tu
clave:

```json
{
  "anthropic_api_key": "sk-ant-...",
  "llm_model": "claude-haiku-4-5"
}
```

`config.json` está en `.gitignore` — la clave nunca se versiona.

## Estructura

```
main.py              Lanzador: abre la ventana pywebview
bridge.py            API expuesta a JavaScript (analizar, guardar, renderizar)
engine.py            Motor de renombrado: el pipeline de 8 pasos
header.py            Detección del título por análisis tipográfico
llm_extract.py       Extractor opcional por LLM
ui/                  Frontend (React sin build step, vía Babel standalone)
PDF Scholar.spec     Receta de PyInstaller para generar el ejecutable
```

## Compilar el ejecutable

```bash
pyinstaller "PDF Scholar.spec"
```

El binario queda en `dist/PDF Scholar/`.

## Licencia

MIT — ver [LICENSE](LICENSE). Podés usarlo, modificarlo y redistribuirlo
libremente conservando el aviso de copyright.

Una aclaración sobre las dependencias: **PyMuPDF es AGPL-3.0**. Instalada por
separado con `pip`, como acá, no afecta la licencia de este código. Pero si
generás el ejecutable con PyInstaller, el bundle resultante incluye PyMuPDF y
queda alcanzado por la AGPL: no lo redistribuyas sin tener eso en cuenta.

## Citación

Si lo usás en tu investigación, hay un [CITATION.cff](CITATION.cff) en la raíz:
GitHub muestra un botón *Cite this repository* que genera la referencia en
BibTeX y APA.

---

Desarrollado en el marco del **LATDAA** (Laboratorio de Tecnologías Digitales
Aplicadas a la Arqueología).
