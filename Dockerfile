FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
# CPU-only torch keeps the image small (~1GB instead of ~4GB)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt

COPY app ./app

# HF_HOME points the model cache at the mounted volume, so the ~340 MB
# second-stage classifier is downloaded once and survives rebuilds.
ENV HF_HOME=/data/hf \
    PYTHONUNBUFFERED=1
CMD ["python", "-m", "app.main"]
