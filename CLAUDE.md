# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Was ist das?

Daigestr — Document Intelligence Service v3.1.0. Konvertiert Dokumente, Bilder, Audio/Video zu Markdown mit LLM-gestützter Analyse. Zwei Schnittstellen:
- **MCP** (Port 8080, extern 18005): Für Claude und MCP-Clients via SSE oder stdio
- **REST** (Port 8081, extern 18006): Für n8n und HTTP-Clients (FastAPI mit Swagger unter `/docs`)

Auto-Routing: Bilder → Mistral Vision API, gescannte PDFs → Mistral OCR 3, Audio/Video → Whisper (lokal), Dokumente → MarkItDown-Library.

## Architektur

Ein Container (`daigestr`) definiert in `docker-compose.yml`, Build-Context ist `./mcp/`.

Kern-Dateien im `mcp/`-Verzeichnis:
- `server.py` (~6500 Zeilen) — Monolith: MCP-Tools, REST-Endpoints, Vision/OCR/Whisper-Integration, PDF-Pipeline, Excel-Verarbeitung, Template-Registry (SQLite), Klassifizierung, Extraktion, Chunking, Quality-Scoring
- `models.py` — Pydantic-Schemas: Request/Response-Modelle, ErrorCodes, MetaData
- `mcp_stdio.py` — Alternativer stdio-Transport (importiert `mcp` aus server.py)
- `seed.sql` — Initiale Template-Registry-Daten (SQLite)

### Datenfluß
Request → Dateityp-Erkennung (Extension + Magic Bytes) → Routing:
- Bilder → resize + Mistral Vision API → Markdown
- Gescannte PDFs → Mistral OCR 3 oder Tesseract-Fallback → Markdown
- Audio/Video → ffmpeg-Extraktion + faster-whisper → Transkript
- Dokumente → MarkItDown Library (+ Enhanced Excel, DOCX-Extras, PDF-Metadata via PyMuPDF)
- Optional: classify, extract (Schema), quality-score, chunking, OCR-Korrektur

### REST-Endpoints
| Endpoint | Methode | Beschreibung |
|----------|---------|-------------|
| `/v1/convert` | POST | Datei → Markdown (path, base64, url) |
| `/v1/convert/folder` | POST | Ordner → zusammengeführtes Markdown |
| `/v1/extract` | POST | Strukturierte Datenextraktion mit JSON-Schema |
| `/v1/analyze` | POST | Analyse (Vision-basiert) |
| `/v1/templates` | GET/POST | Template-Registry CRUD |
| `/v1/templates/{id}` | GET/PUT/DELETE | Einzelnes Template |
| `/v1/templates/bulk` | POST | Bulk-Import |
| `/v1/templates/categories` | GET | Kategorien aus Registry |
| `/v1/templates/search` | GET | Template-Suche |
| `/v1/health` | GET | Health-Check |
| `/v1/formats` | GET | Unterstützte Formate |
| `/v1/tips` | GET | LLM-Nutzungshinweise |

### MCP-Tools
`convert`, `extract`, `convert_folder`, `health`, `list_files`, `get_tips`

## Convert-Optionen (WICHTIG für LLM-Nutzer!)

Alle Optionen sind standardmäßig **AUS**. Für eine vollständige Konvertierung müssen Features explizit aktiviert werden:

| Parameter | Default | Wann nutzen |
|-----------|---------|------------|
| `describe_images` | `false` | **DOCX/PPTX mit Bildern/Diagrammen/Charts** — jedes Bild wird automatisch klassifiziert und typ-spezifisch verarbeitet: Diagramme → **Mermaid-Syntax**, Charts → **Datentabelle**, Fotos → Beschreibung, Text-Scans → OCR, Dekorativ → übersprungen. Ohne diesen Parameter werden alle Bilder als `[image]` Platzhalter ausgegeben! |
| `accuracy` | `"standard"` | **Gescannte PDFs** — `"high"` aktiviert OCR-Korrektur + Dual-Pass-Validierung |
| `classify` | `false` | Dokumenttyp erkennen (invoice, contract, cv, etc.) → `meta.document_type` |
| `auto_extract` | `false` | Automatisch klassifizieren + passendes Template finden + strukturiert extrahieren — alles in einem Call |
| `template` | `null` | Vordefiniertes Extraktions-Template (z.B. `"invoice"`, `"cv"`, `"contract"`) → Ergebnis in `extracted` |
| `extract_schema` | `null` | Custom JSON-Schema für strukturierte Extraktion → Ergebnis in `extracted` |
| `chunk` | `false` | Markdown in RAG-ready Chunks aufteilen → Ergebnis in `chunks` |
| `ocr_correct` | `false` | LLM-Nachkorrektur für OCR-Fehler (automatisch bei `accuracy="high"`) |
| `ocr_embed` | `false` | OCR-Text als unsichtbare Ebene in PDF einbetten (durchsuchbar machen) |
| `show_formulas` | `false` | Excel-Formeln anzeigen: `42 [=SUM(A1:A10)]` |
| `language` | `"de"` | Sprache für Vision/OCR-Antworten |
| `prompt` | `null` | Custom-Prompt für Vision-Analyse |

### Typische Nutzungsmuster

```bash
# Maximale Extraktion (Bilder beschrieben, hohe Genauigkeit, Dokumenttyp erkannt)
curl -X POST http://localhost:18006/v1/convert \
  -d '{"path": "/data/doc.pdf", "describe_images": true, "accuracy": "high", "classify": true}'

# Rechnung strukturiert extrahieren (vordefiniertes Template)
curl -X POST http://localhost:18006/v1/convert \
  -d '{"path": "/data/rechnung.pdf", "template": "invoice"}'

# Vollautomatisch: Typ erkennen + passendes Template + extrahieren
curl -X POST http://localhost:18006/v1/convert \
  -d '{"path": "/data/doc.pdf", "auto_extract": true}'

# RAG-Pipeline: Konvertieren + Chunken
curl -X POST http://localhost:18006/v1/convert \
  -d '{"path": "/data/doc.pdf", "chunk": true, "chunk_size": 1024}'
```

### Response-Felder
- `markdown` — Immer vorhanden
- `extracted` — Nur wenn `extract_schema`, `template` oder `auto_extract` gesetzt
- `chunks` — Nur wenn `chunk=true`
- `meta` — Immer vorhanden (quality_score, duration, pipeline_steps, zugferd, xmp_metadata, exif, etc.)

### Bild-Klassifizierung (bei describe_images: true)

Jedes eingebettete Bild wird automatisch via Vision AI in 5 Kategorien klassifiziert:

| Kategorie | Verarbeitung |
|-----------|-------------|
| `diagram` | → Mermaid-Syntax (graph TD, sequenceDiagram, classDiagram) |
| `chart` | → Markdown-Datentabelle (Achsen, Werte, Legende) |
| `photo` | → Textuelle Beschreibung |
| `text_scan` | → OCR/Text-Extraktion |
| `decorative` | → übersprungen (Logos, Icons) |

Kein extra Parameter nötig — die Klassifizierung und typ-spezifische Verarbeitung ist automatisch.

### Häufige Fehler
- **`extracted` ist null** → `template`, `extract_schema` oder `auto_extract` fehlt
- **Bilder/Diagramme nicht beschrieben** → `describe_images: true` fehlt
- **Schlechte OCR-Qualität** → `accuracy: "high"` setzen
- **`chunks` ist null** → `chunk: true` fehlt

### Tips-Endpoint
`GET /v1/tips` bzw. MCP-Tool `get_tips` liefert die vollständige Feature-Referenz als JSON — ideal für LLM-Self-Discovery.

## Build & Run

```bash
# Build und Start
docker compose up -d --build

# Nur rebuilden
docker compose build daigestr && docker compose up -d daigestr

# Logs
docker logs -f daigestr
```

**Kein Dev-Mode mit Volume-Mounts für Source-Code** — nach Code-Änderungen muss rebuildet werden. Volume `./data:/data` ist nur für Nutz-Daten.

## Tests

Tests liegen in `mcp/tests/` und nutzen pytest. Sie mocken schwere Abhängigkeiten (PIL, MarkItDown, Mistral APIs) via `conftest.py:load_server_module()`.

```bash
# Alle Tests (lokal, nicht im Container)
cd mcp && python -m pytest tests/ -v

# Einzelner Test
cd mcp && python -m pytest tests/test_extract.py -v

# Einzelne Test-Funktion
cd mcp && python -m pytest tests/test_extract.py::test_function_name -v
```

**Wichtig:** Tests importieren `server.py` mit gemockten Abhängigkeiten. `conftest.py` sichert echtes PIL vor dem Mocking. `load_server_module(use_real_pil=True)` für Tests die echte Bildverarbeitung brauchen.

## REST-API testen

```bash
# Health-Check
curl http://localhost:18006/v1/health

# Datei konvertieren (Pfad im Container)
curl -X POST http://localhost:18006/v1/convert \
  -H "Content-Type: application/json" \
  -d '{"path": "/data/datei.pdf"}'

# Swagger UI: http://localhost:18006/docs
```

## Konfiguration (.env)

Siehe `.env.example` für alle Variablen. Wichtigste:

| Variable | Default | Zweck |
|----------|---------|-------|
| `MISTRAL_API_KEY` | - | Pflicht für Vision/OCR/Classify/Extract |
| `MISTRAL_VISION_MODEL` | mistral-large-latest | Vision-Modell |
| `MISTRAL_OCR_MODEL` | mistral-ocr-latest | OCR-Modell |
| `MISTRAL_TEXT_MODEL` | mistral-large-latest | Text-Modell (Classify, Extract, OCR-Korrektur) |
| `MISTRAL_OCR_ENABLED` | true | OCR 3 aktivieren |
| `WHISPER_MODEL_SIZE` | base | Whisper-Modell (tiny/base/small/medium/large) |

## Unterstützte Formate

- **Vision (Bilder):** jpg, jpeg, png, gif, webp, bmp
- **MarkItDown (Dokumente):** pdf, docx, doc, pptx, ppt, xlsx, xls, odt, ods, odp, html, htm, xml, json, csv, txt, md, rtf, eml
- **Audio:** mp3, wav, ogg, flac, m4a
- **Video:** mp4, mkv, webm, avi, mov

## Netzwerk

Container läuft im `shared-network` (Docker external network, definiert in `docker-compose.override.yml`). Volume `./data` wird als `/data` gemountet.

## Systemabhängigkeiten im Container

OCR: tesseract (deu+eng), PDF: poppler-utils, Audio/Video: ffmpeg, Bilder: imagemagick + Pillow, Dateityp: libmagic, Whisper: faster-whisper (CPU)
