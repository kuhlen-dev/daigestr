# Daigestr

**Turn messy business documents into reliable Markdown and structured data.**

Daigestr is for the point where generic document conversion stops being useful: scanned invoices, ugly PDFs, DOCX reports with embedded charts, mixed-language attachments, or document folders that break downstream automation. It converts, classifies, extracts, and normalizes documents through one self-hosted service with a REST API for workflows and an MCP interface for agents.

Current version: **v13.5.1**

## Why Teams Use It

Most teams do not want "document conversion". They want:

- clean Markdown for LLMs, RAG, search, and archives
- structured fields from invoices, contracts, policies, and operational documents
- one stable ingestion endpoint instead of brittle format-specific scripts
- self-hosted processing with database-backed templates, prompts, jobs, cache, and normalization

Daigestr is built for ugly inputs, not demo files.

## What It Does

Daigestr routes each document through the right path for that document.

- **PDF intelligence**: detect scans, run OCR, merge cross-page tables, preserve more structure
- **Image understanding**: describe embedded visuals, turn diagrams into Mermaid, charts into tables
- **Classification**: identify document type with confidence scores
- **Schema extraction**: extract structured JSON from templates or custom schemas
- **Auto-extract**: classify, select a template, and extract in one call
- **Normalization**: map template-specific fields onto stable downstream fields
- **Folder and async processing**: process batches, long-running jobs, and webhook callbacks
- **Two interfaces, one engine**: REST for pipelines, MCP for agents

## What Makes It Different

- It is optimized for messy production documents, not just clean office files.
- It combines conversion and extraction instead of making you stitch together multiple tools.
- It is **DB-first**: templates, prompts, cache, jobs, audit, and normalization live in PostgreSQL.
- It stays self-hosted and `.env`-configured.
- It is automation-oriented, not just a document viewer wrapped as an API.

## Typical Outcomes

- convert a scanned invoice into searchable Markdown plus extracted invoice fields
- ingest mixed folders of tax, telecom, insurance, and contract documents with consistent extraction
- feed cleaner Markdown into LLM or RAG systems than generic converters usually produce
- normalize heterogeneous document outputs so downstream systems do not need document-specific logic

## Capabilities At A Glance

| Area | What Daigestr adds |
|------|--------------------|
| Conversion | Markdown-first output across PDF, Office, images, web, audio, and video |
| Scans | OCR routing for scanned PDFs and image-heavy documents |
| Tables | Better handling for cross-page and image-based tables |
| Visuals | Diagram, chart, photo, and OCR-style handling for embedded images |
| Extraction | Template-backed or custom-schema JSON extraction |
| Classification | Configurable document-type classification with confidence |
| Normalization | Stable downstream fields across heterogeneous templates |
| Operations | Async jobs, webhooks, request cache, audit log, health endpoints |
| Integrations | REST API, MCP tools, Docker Compose deployment |

## What It Looks Like

### Scanned PDF to Markdown

```bash
curl -X POST http://localhost:18006/v1/convert \
  -H "Content-Type: application/json" \
  -d '{"path": "/data/scan.pdf", "accuracy": "high"}'
```

This runs the higher-accuracy path for scans and returns Markdown plus metadata such as OCR model, pipeline steps, and quality score.

### Auto-Extract in One Call

```bash
curl -X POST http://localhost:18006/v1/convert \
  -H "Content-Type: application/json" \
  -d '{"path": "/data/rechnung.pdf", "auto_extract": true}'
```

This classifies the document, selects the matching template from PostgreSQL, and returns structured JSON in `extracted`.

### Explicit Extraction with a Template

```bash
curl -X POST http://localhost:18006/v1/extract \
  -H "Content-Type: application/json" \
  -d '{"path": "/data/invoice.pdf", "template": "invoice"}'
```

### DOCX or PDF with Embedded Visuals

```bash
curl -X POST http://localhost:18006/v1/convert \
  -H "Content-Type: application/json" \
  -d '{"path": "/data/report.docx", "describe_images": true, "classify": true}'
```

Embedded diagrams can become Mermaid, charts can become tables, and photos can receive descriptive captions instead of `[image]` placeholders.

## Quick Start

### Requirements

- Docker and Docker Compose
- a Mistral API key for OCR, image understanding, classification, and extraction features

### Setup

```bash
git clone https://github.com/kuhlen-dev/daigestr.git
cd daigestr
cp .env.example .env
```

Set at least:

```env
MISTRAL_API_KEY=your-api-key-here
```

Then start the stack:

```bash
docker compose up -d --build
```

### Default Ports

Both published host ports are `.env` configurable:

| Interface | Default host port | Purpose |
|-----------|-------------------|---------|
| REST | `18006` | HTTP API and Swagger |
| MCP | `18005` | MCP SSE endpoint |
| PostgreSQL | `15432` | local DB access for debugging/admin work |

### Verify

```bash
curl http://localhost:18006/v1/health
```

## Core REST Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/v1/convert` | Convert one file, URL, or base64 payload |
| `POST` | `/v1/convert/folder` | Convert all files in a folder |
| `POST` | `/v1/extract` | Extract structured data |
| `POST` | `/v1/convert/async` | Start an async conversion job |
| `GET` | `/v1/jobs/{id}` | Check async job status |
| `GET` | `/v1/jobs/{id}/result` | Fetch async job result |
| `GET` | `/v1/templates` | Inspect live template registry |
| `GET` | `/v1/health` | Health and readiness |

Interactive docs:

```text
http://localhost:18006/docs
```

## Common Request Patterns

### Standard conversion

```bash
curl -X POST http://localhost:18006/v1/convert \
  -H "Content-Type: application/json" \
  -d '{"path": "/data/report.pdf"}'
```

### URL to Markdown

```bash
curl -X POST http://localhost:18006/v1/convert \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/docs/page"}'
```

### Base64 payload

```bash
curl -X POST http://localhost:18006/v1/convert \
  -H "Content-Type: application/json" \
  -d '{"base64": "<base64>", "filename": "invoice.pdf"}'
```

### Folder conversion

```bash
curl -X POST http://localhost:18006/v1/convert/folder \
  -H "Content-Type: application/json" \
  -d '{"path": "/data/documents"}'
```

### Async job with webhook

```bash
curl -X POST http://localhost:18006/v1/convert/async \
  -H "Content-Type: application/json" \
  -d '{
    "path": "/data/large.pdf",
    "mode": "full",
    "webhook_url": "https://example.com/hooks/daigestr"
  }'
```

## Important Request Options

| Option | Purpose |
|--------|---------|
| `accuracy: "high"` | OCR correction and dual-pass validation for harder inputs |
| `classify: true` | add `document_type` and confidence |
| `template: "invoice"` | extract with a known template |
| `extract_schema: {...}` | extract against a custom JSON Schema |
| `auto_extract: true` | classify, template-match, and extract automatically |
| `describe_images: true` | enrich output with image understanding |
| `mode: "full"` | convenient high-feature mode for broad extraction |
| `pages: "1-3,!2"` | restrict PDF processing to selected pages |
| `ocr_embed: true` | embed searchable OCR text into scanned PDFs |
| `chunk: true` | produce RAG-friendly chunks in addition to Markdown |
| `no_cache: true` | bypass cached results |
| `compact: true` | remove null fields from the normalized output |

## Output Model

Successful responses are Markdown-first and can add structured payloads on top.

```json
{
  "success": true,
  "markdown": "# Document Title\n\n...",
  "meta": {
    "format": "pdf",
    "duration_ms": 1240,
    "quality_score": 0.91,
    "document_type": "invoice",
    "template_used": "invoice"
  },
  "extracted": {
    "invoice_number": "INV-2026-0042"
  }
}
```

Depending on options and template coverage, responses can also include:

- `chunks`
- `normalized`
- `compact`
- `enriched_pdf`
- `html`

## Template Registry And Normalization

Daigestr keeps its extraction and normalization logic in PostgreSQL, not in ad-hoc code paths.

- **Templates** define extraction schemas, metadata, matching hints, and priority.
- **Prompts** are DB-backed and centrally managed.
- **Normalization** maps template-specific fields onto a smaller stable downstream vocabulary.
- **Auto-extract** uses classification plus template matching to drive schema extraction.

Use `GET /v1/templates` to inspect the live registry on your instance.

## Supported Inputs

| Category | Formats |
|----------|---------|
| Documents | PDF, DOCX, DOC, PPTX, PPT, XLSX, XLS, ODT, ODS, ODP, RTF |
| Web / Text | HTML, HTM, XML, JSON, CSV, TXT, Markdown |
| Images | JPG, JPEG, PNG, GIF, WebP, BMP |
| Audio | MP3, WAV, OGG, FLAC, M4A |
| Video | MP4, MKV, WebM, AVI, MOV |

## Configuration

Everything important is `.env`-driven. No hardcoded deployment assumptions are required for normal operation.

### Core Variables

| Variable | Purpose |
|----------|---------|
| `MISTRAL_API_KEY` | required for OCR, Vision, classification, and extraction |
| `MISTRAL_API_URL` | Mistral API base URL |
| `MISTRAL_VISION_MODEL` | Vision and image understanding model |
| `MISTRAL_TEXT_MODEL` | classification and extraction model |
| `MISTRAL_OCR_MODEL` | OCR model for scanned PDFs |
| `DATABASE_URL` | PostgreSQL connection string |
| `REST_HOST_PORT` | published REST host port |
| `MCP_HOST_PORT` | published MCP host port |
| `POSTGRES_HOST_PORT` | published PostgreSQL host port |
| `DATA_DIR` | mounted document directory inside the container |
| `TEMP_DIR` | temporary processing directory |

### Operational Variables

| Variable | Purpose |
|----------|---------|
| `CACHE_TTL_SECONDS` | request cache TTL |
| `JOB_TIMEOUT_SECONDS` | timeout for async jobs |
| `WEBHOOK_TIMEOUT_SECONDS` | webhook timeout |
| `SCAN_THRESHOLD_CHARS` | scanned-PDF detection threshold |
| `MAX_FILE_SIZE_MB` | input size limit |
| `PDF_RENDER_DPI` | render DPI for image-based PDF handling |
| `WHISPER_MODEL_SIZE` | Whisper model for audio/video transcription |
| `BRIX_URL` | Brix orchestrator URL for batch-related integration |

See [.env.example](./.env.example) for the full set.

## Architecture

The deployment is intentionally simple:

- `daigestr`: Python application exposing REST and MCP
- `daigestr-postgres`: PostgreSQL for templates, prompts, cache, jobs, audit, and normalization

Important modules in [`mcp/`](./mcp):

- `server.py`: startup and compatibility re-exports
- `api_rest.py`: REST API
- `api_mcp.py`: MCP tools
- `routing.py`: routing and orchestration
- `intelligence.py`: classify, extract, validate, chunk, quality scoring
- `templates_db.py`: DB-backed templates, prompts, cache, and jobs
- `normalizer.py`: normalized field pipeline

## MCP Interface

Daigestr exposes the same engine through MCP for agent systems.

Core tools:

- `convert`
- `convert_folder`
- `extract`
- `health`
- `list_files`
- `get_tips`

## Local Development

```bash
docker compose restart daigestr
docker logs -f daigestr
cd mcp && python3 -m pytest tests/ -v
```

The repo includes a local `docker-compose.override.yml` pattern for developer-specific mounts. Keep machine-local tweaks there rather than in tracked deployment config.

## Positioning

Daigestr is a strong fit when:

- you need self-hosted document ingestion
- your inputs are messy enough that vanilla converters stop being useful
- you need both Markdown and structured extraction
- you want one service for workflows, backends, and agents

It is probably not the right tool when:

- you only need clean-file Markdown conversion
- you do not need OCR, extraction, normalization, async jobs, or templates
- you prefer a fully managed cloud document service over self-hosting

## License

MIT
