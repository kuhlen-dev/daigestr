# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Was ist das?

Daigestr — Document Intelligence Service v5.4.0. Konvertiert Dokumente, Bilder, Audio/Video zu Markdown mit LLM-gestützter Analyse. Zwei Schnittstellen:
- **MCP** (Port 8080, extern 18005): Für Claude und MCP-Clients via SSE oder stdio
- **REST** (Port 8081, extern 18006): Für n8n und HTTP-Clients (FastAPI mit Swagger unter `/docs`)

Auto-Routing: Bilder → Mistral Vision API, gescannte PDFs → Mistral OCR 3, Audio/Video → Whisper (lokal), Dokumente → MarkItDown-Library.

## Architektur

Ein Container (`daigestr`) definiert in `docker-compose.yml`, Build-Context ist `./mcp/`.

### Modulstruktur (`mcp/`)

| Modul | Zeilen | Zweck |
|-------|--------|-------|
| `server.py` | 281 | Startup, uvloop, Re-Exports für Backwards-Compatibility |
| `settings.py` | ~160 | Alle Umgebungsvariablen und Konstanten |
| `logging_setup.py` | — | structlog-Konfiguration |
| `utils.py` | — | Hilfsfunktionen: `_get()`, `resolve_path`, Dateityp-Erkennung |
| `mistral_client.py` | — | Mistral Vision API-Client |
| `converters/images.py` | — | Resize, EXIF, Bilder aus DOCX/PPTX/PDF/ODT/ODP/HTML, `describe_embedded_images` |
| `converters/pdf.py` | — | `is_scanned_pdf`, `convert_scanned_pdf`, `embed_ocr_in_pdf` |
| `converters/office.py` | — | `convert_with_markitdown` (inkl. Excel-Formeln, DOCX-Extras) |
| `converters/audio.py` | — | ffmpeg-Extraktion + faster-whisper Transkription |
| `converters/email.py` | — | EML-Parsing, Routing-Metadaten, Thread-Infos, Kalender-Events |
| `intelligence.py` | 1279 | `classify`, `extract`, `quality_score`, `chunk`, `dual_pass_validate`, `_apply_auto_extract` |
| `templates_db.py` | 318 | SQLite: templates, prompts, scoring_weights, cache (`cache_get/set/clear`) |
| `routing.py` | 1041 | `convert_auto`, `convert_folder_contents`, `convert_url`, `_build_tips_dict` |
| `api_rest.py` | 749 | FastAPI-App, alle REST-Endpoints |
| `api_mcp.py` | 471 | FastMCP-Instanz, alle MCP-Tools |
| `renderers/html.py` | — | Markdown → vollständiges HTML (Mermaid.js, highlight.js, CSS) |
| `renderers/text.py` | — | Markdown → Plaintext (Markdown-Syntax entfernen) |
| `models.py` | — | Pydantic-Schemas: Request/Response-Modelle, ErrorCodes, MetaData |

### Datenfluß

```
Request → Dateityp-Erkennung (Extension + Magic Bytes)
        → Cache-Check (CACHE_ENABLED)
        → Routing:
           Bilder        → resize + Mistral Vision API → Markdown
           Gescannte PDF → Mistral OCR 3 / Tesseract-Fallback → Markdown
           Audio/Video   → ffmpeg + faster-whisper → Transkript
           Dokumente     → MarkItDown Library (+ Enhanced Excel, DOCX/PDF-Extras)
        → Optional: classify, describe_images, extract, quality-score, chunk, OCR-embed
        → output_format-Rendering (markdown/html/text)
        → Cache-Set → Response
```

## Convert-Optionen (WICHTIG für LLM-Nutzer!)

Alle Features sind standardmäßig **AUS** — explizit aktivieren oder `mode: "full"` verwenden.

| Parameter | Default | Beschreibung |
|-----------|---------|-------------|
| `mode` | `"default"` | `"full"` schaltet alle Features ein: `describe_images`, `accuracy="high"`, `classify`, `ocr_correct`, `auto_extract`, `chunk` |
| `output_format` | `"markdown"` | Ausgabeformat: `"markdown"`, `"html"` (Mermaid + highlight.js), `"text"` (Plaintext ohne Syntax) |
| `describe_images` | `false` | Eingebettete Bilder in **allen** Formaten beschreiben: PDF, DOCX, PPTX, ODT, ODP, HTML. Automatische Bild-Klassifizierung: `diagram` → Mermaid-Syntax, `chart` → Datentabelle, `photo` → Beschreibung, `text_scan` → OCR, `decorative` → übersprungen. Ohne diesen Parameter: alle Bilder als `[image]`! |
| `accuracy` | `"standard"` | `"high"` aktiviert OCR-Korrektur + Dual-Pass-Validierung für gescannte PDFs |
| `classify` | `false` | Dokumenttyp via LLM erkennen → `meta.document_type`, `meta.document_type_confidence` |
| `auto_extract` | `false` | Alles in einem: Typ erkennen + passendes Template suchen + strukturiert extrahieren → `extracted` |
| `template` | `null` | Vordefiniertes Extraktions-Template (z.B. `"invoice"`, `"cv"`, `"contract"`) → `extracted` |
| `extract_schema` | `null` | Custom JSON-Schema für strukturierte Extraktion → `extracted` |
| `chunk` | `false` | Markdown in RAG-ready Chunks aufteilen → `chunks` |
| `chunk_size` | `512` | Maximale Chunk-Größe in Tokens |
| `ocr_correct` | `false` | LLM-Nachkorrektur für OCR-Fehler (automatisch bei `accuracy="high"`) |
| `ocr_embed` | `false` | OCR-Text als unsichtbare Textschicht in gescannte PDFs einbetten (→ `enriched_pdf` in Response) |
| `show_formulas` | `false` | Excel-Formeln im Output annotieren: `42 [=SUM(A1:A10)]` |
| `language` | `"de"` | Sprache für Vision/OCR/Classify-Antworten (`"de"` oder `"en"`) |
| `prompt` | `null` | Custom-Prompt für Vision-Analyse |
| `min_confidence` | `0.7` | Minimale Klassifizierungs-Konfidenz für `auto_extract` |
| `pages` | `null` | Seitenauswahl für PDFs. Syntax: `"1-3"`, `"7,14,22"`, `"10-20,!15"`. Null = alle Seiten. |
| `webhook_url` | `null` | URL für POST-Callback wenn Konvertierung fertig (besonders nützlich mit Async Jobs) |
| `classify_categories` | (aus Registry) | Erlaubte Dokumenttypen (überschreibt Default) |

### Bild-Klassifizierung (bei `describe_images: true`)

Automatische Klassifizierung via Vision AI — kein extra Parameter nötig:

| Kategorie | Verarbeitung |
|-----------|-------------|
| `diagram` | → Mermaid-Syntax (graph TD, sequenceDiagram, classDiagram) |
| `chart` | → Markdown-Datentabelle (Achsen, Werte, Legende) |
| `photo` | → Textuelle Beschreibung |
| `text_scan` | → OCR/Text-Extraktion |
| `decorative` | → übersprungen (Logos, Icons) |

### Typische Nutzungsmuster

```bash
# Alle Features auf einmal
curl -X POST http://localhost:18006/v1/convert \
  -H "Content-Type: application/json" \
  -d '{"path": "/data/doc.pdf", "mode": "full"}'

# Gescanntes PDF mit Bildbeschreibung + Klassifizierung
curl -X POST http://localhost:18006/v1/convert \
  -H "Content-Type: application/json" \
  -d '{"path": "/data/doc.pdf", "describe_images": true, "accuracy": "high", "classify": true}'

# Rechnung strukturiert extrahieren (vordefiniertes Template)
curl -X POST http://localhost:18006/v1/convert \
  -H "Content-Type: application/json" \
  -d '{"path": "/data/rechnung.pdf", "template": "invoice"}'

# Vollautomatisch: Typ erkennen + passendes Template + extrahieren
curl -X POST http://localhost:18006/v1/convert \
  -H "Content-Type: application/json" \
  -d '{"path": "/data/doc.pdf", "auto_extract": true}'

# RAG-Pipeline: Konvertieren + Chunken
curl -X POST http://localhost:18006/v1/convert \
  -H "Content-Type: application/json" \
  -d '{"path": "/data/doc.pdf", "chunk": true, "chunk_size": 1024}'

# HTML-Output mit Mermaid-Rendering
curl -X POST http://localhost:18006/v1/convert \
  -H "Content-Type: application/json" \
  -d '{"path": "/data/report.docx", "output_format": "html", "describe_images": true}'

# Nur bestimmte Seiten eines PDFs konvertieren
curl -X POST http://localhost:18006/v1/convert \
  -H "Content-Type: application/json" \
  -d '{"path": "/data/doc.pdf", "pages": "1-3,7,!2"}'

# Async-Konvertierung mit Webhook-Callback
curl -X POST http://localhost:18006/v1/convert/async \
  -H "Content-Type: application/json" \
  -d '{"path": "/data/large.pdf", "mode": "full", "webhook_url": "https://example.com/callback"}'

# OCR-Text als durchsuchbare Schicht einbetten
curl -X POST http://localhost:18006/v1/convert \
  -H "Content-Type: application/json" \
  -d '{"path": "/data/scan.pdf", "ocr_embed": true}'
```

### Response-Felder

| Feld | Präsenz |
|------|---------|
| `markdown` | Immer vorhanden (außer bei Fehler) |
| `html` | Nur wenn `output_format="html"` |
| `extracted` | Nur wenn `extract_schema`, `template` oder `auto_extract` gesetzt |
| `chunks` | Nur wenn `chunk=true` |
| `enriched_pdf` | Nur wenn `ocr_embed=true` und Dokument ist gescanntes PDF (Base64) |
| `meta` | Immer vorhanden — enthält: `quality_score`, `duration_ms`, `pipeline_steps`, `document_type`, `zugferd`, `xmp_metadata`, `exif`, `iptc`, `cached`, etc. |
| `error` | Nur wenn `success=false` |

### Häufige Fehler

- **`extracted` ist null** → `template`, `extract_schema` oder `auto_extract` fehlt
- **Bilder/Diagramme nicht beschrieben** → `describe_images: true` fehlt
- **Schlechte OCR-Qualität** → `accuracy: "high"` setzen
- **`chunks` ist null** → `chunk: true` fehlt

## REST-Endpoints

| Endpoint | Methode | Beschreibung |
|----------|---------|-------------|
| `/v1/convert` | POST | Datei → Markdown (path, base64, url) |
| `/v1/convert/folder` | POST | Ordner → zusammengeführtes Markdown |
| `/v1/extract` | POST | Konvertierung + strukturierte Extraktion kombiniert |
| `/v1/analyze` | POST | Explizite Vision-Analyse (nur Bilder) |
| `/v1/templates` | GET | Alle Templates auflisten |
| `/v1/templates` | POST | Neues Template erstellen |
| `/v1/templates/{id}` | GET | Einzelnes Template abrufen |
| `/v1/templates/{id}` | PUT | Template aktualisieren (partial update) |
| `/v1/templates/{id}` | DELETE | Template löschen |
| `/v1/templates/bulk` | POST | Bulk-Upsert für Templates |
| `/v1/templates/categories` | GET | Template-Kategorien mit Anzahl |
| `/v1/templates/search` | GET | Templates nach Stichwort suchen (`?q=invoice`) |
| `/v1/convert/async` | POST | Async-Konvertierung starten → gibt Job-ID zurück |
| `/v1/jobs` | GET | Alle Jobs auflisten |
| `/v1/jobs/{id}` | GET | Job-Status abfragen |
| `/v1/jobs/{id}/result` | GET | Job-Ergebnis abrufen (wenn fertig) |
| `/v1/jobs/{id}` | DELETE | Job löschen |
| `/v1/cache` | DELETE | Request-Level-Cache leeren |
| `/v1/health` | GET | Health-Check |
| `/v1/formats` | GET | Unterstützte Formate |
| `/v1/tips` | GET | Vollständige Feature-Referenz als JSON (ideal für LLM-Self-Discovery) |

## MCP-Tools

| Tool | Beschreibung |
|------|-------------|
| `convert` | Datei → Markdown (path, base64_data, url) — alle Optionen verfügbar |
| `extract` | Konvertierung + strukturierte Extraktion kombiniert |
| `convert_folder` | Alle Dateien eines Ordners → zusammengeführtes Markdown |
| `health` | Service-Status |
| `list_files` | Dateien im /data-Verzeichnis auflisten |
| `get_tips` | Vollständige Feature-Referenz (analog zu GET /v1/tips) |

**Hinweis:** MCP nutzt `base64_data`, REST nutzt `base64` als Feldname.

## Build & Run

```bash
# Build und Start
docker compose up -d --build

# Nur rebuilden
docker compose build daigestr && docker compose up -d daigestr

# Logs
docker logs -f daigestr
```

**Dev-Mode:** `docker-compose.override.yml` mountet alle Source-Dateien als Volumes. Code-Änderungen sind sofort im Container verfügbar (Container-Restart nötig, aber kein Rebuild). Volume `./data:/data` ist für Nutz-Daten.

## Tests

Tests liegen in `mcp/tests/` und nutzen pytest. Schwere Abhängigkeiten (PIL, MarkItDown, Mistral APIs) werden via `conftest.py:load_server_module()` gemockt.

```bash
# Alle Tests (lokal, nicht im Container)
cd mcp && python -m pytest tests/ -v

# Einzelner Test-File
cd mcp && python -m pytest tests/test_extract.py -v

# Einzelne Test-Funktion
cd mcp && python -m pytest tests/test_extract.py::test_function_name -v
```

**Wichtig:** `conftest.py` sichert echtes PIL vor dem Mocking. `load_server_module(use_real_pil=True)` für Tests die echte Bildverarbeitung brauchen.

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

## Konfiguration

Siehe `.env.example` für alle Variablen mit Kommentaren.

Wichtigste Variablen:

| Variable | Default | Zweck |
|----------|---------|-------|
| `MISTRAL_API_KEY` | — | Pflicht für Vision/OCR/Classify/Extract |
| `MISTRAL_VISION_MODEL` | mistral-large-latest | Vision-Modell |
| `MISTRAL_OCR_MODEL` | mistral-ocr-latest | OCR-Modell |
| `MISTRAL_TEXT_MODEL` | mistral-large-latest | Text-Modell (Classify, Extract, OCR-Korrektur) |
| `MISTRAL_OCR_ENABLED` | true | Mistral OCR 3 aktivieren |
| `CACHE_ENABLED` | true | Request-Level-Cache aktivieren |
| `CACHE_TTL_SECONDS` | 3600 | Cache-TTL in Sekunden |
| `RATE_LIMIT_MAX_WAIT_SECONDS` | 60 | Max. Wartezeit bei Rate-Limit |
| `MERMAID_CDN_URL` | jsdelivr | CDN für Mermaid.js (HTML-Output) |
| `HIGHLIGHTJS_CDN_URL` | cdnjs | CDN für highlight.js (HTML-Output) |
| `HIGHLIGHTJS_CSS_URL` | cdnjs | CDN für highlight.js CSS (HTML-Output) |
| `MAX_DESCRIBE_IMAGES` | 50 | Max. Anzahl eingebetteter Bilder pro Dokument (Crash-Prävention) |
| `CONVERT_TIMEOUT_SECONDS` | 300 | Globaler Timeout für eine einzelne Konvertierung |
| `WEBHOOK_TIMEOUT_SECONDS` | 30 | Timeout für Webhook-Zustellung |
| `WHISPER_MODEL_SIZE` | base | Whisper-Modell (tiny/base/small/medium/large) |

## Unterstützte Formate

- **Vision (Bilder):** jpg, jpeg, png, gif, webp, bmp
- **MarkItDown (Dokumente):** pdf, docx, doc, pptx, ppt, xlsx, xls, odt, ods, odp, html, htm, xml, json, csv, txt, md, rtf, eml
- **Audio:** mp3, wav, ogg, flac, m4a
- **Video:** mp4, mkv, webm, avi, mov

## Netzwerk

Container läuft im `shared-network` (Docker external network). Volume `./data` wird als `/data` gemountet.

## Systemabhängigkeiten im Container

OCR: tesseract (deu+eng), PDF: poppler-utils, Audio/Video: ffmpeg, Bilder: imagemagick + Pillow, Dateityp: libmagic, Whisper: faster-whisper (CPU)
