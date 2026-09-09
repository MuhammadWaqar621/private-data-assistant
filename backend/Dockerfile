FROM python:3.11-slim

WORKDIR /app

# System deps:
#   build-essential + libpq-dev  -> psycopg2-binary / general builds
#   freetds-dev + libssl-dev     -> pymssql (FreeTDS headers; the wheel
#                                   bundles the library, but a source build
#                                   on a platform without a wheel needs these)
# No OCR/PyTorch stack here - this project has no document ingestion, so the
# image stays small and builds in well under a minute.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential libpq-dev freetds-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
