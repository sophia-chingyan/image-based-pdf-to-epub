FROM python:3.11-slim

WORKDIR /app

ENV PYTHONPATH=/app

# System dependencies for OpenCV, PyMuPDF, and health-check curl
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application source
COPY config.yaml .
COPY store.py .
COPY Api/main.py .
COPY Api/static ./static
COPY Worker/ Worker/

EXPOSE ${PORT:-8000}

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips '*'"]
