FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    tesseract-ocr \
    tesseract-ocr-deu \
    tesseract-ocr-eng \
    ffmpeg \
    libmagic1 \
    curl \
    imagemagick \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py models.py seed.sql seed_normalization.sql mcp_stdio.py ./
COPY settings.py logging_setup.py utils.py mistral_client.py intelligence.py ./
COPY templates_db.py routing.py api_rest.py api_mcp.py audit_db.py api_rest_audit.py ./
COPY normalizer.py normalizer_db.py normalizer_cache.py api_rest_normalize.py ./
COPY converters/ ./converters/
COPY renderers/ ./renderers/
COPY scripts/ ./scripts/

RUN mkdir -p /data /tmp/markitdown

ARG DAIGESTR_VERSION=10.1.1
ENV DAIGESTR_VERSION=${DAIGESTR_VERSION}

EXPOSE 8080 8081

CMD ["python", "server.py"]
