FROM python:3.11-slim

WORKDIR /app

# System dependencies for OpenCV and PyMuPDF
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

COPY Worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.yaml .
COPY Worker/ .

CMD ["python", "worker.py"]
