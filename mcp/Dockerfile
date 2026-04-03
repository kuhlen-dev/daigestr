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

COPY server.py models.py seed.sql ./
COPY settings.py logging_setup.py utils.py mistral_client.py intelligence.py ./
COPY templates_db.py routing.py api_rest.py api_mcp.py ./
COPY converters/ ./converters/
COPY renderers/ ./renderers/

RUN mkdir -p /data /tmp/markitdown

ARG DAIGESTR_VERSION=dev
ENV DAIGESTR_VERSION=${DAIGESTR_VERSION}

EXPOSE 8080 8081

CMD ["python", "server.py"]
