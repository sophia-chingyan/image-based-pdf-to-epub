FROM python:3.11-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY Api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.yaml .
COPY store.py .
COPY Api/main.py .
COPY Api/static ./static

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
