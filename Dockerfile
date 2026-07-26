FROM python:3.12-slim

# ffmpeg is required for merging YouTube video+audio streams
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY bot/ bot/

# Run as non-root
RUN useradd -m appuser && chown -R appuser /app \
    && mkdir -p /data && chown appuser /data
USER appuser

CMD ["python", "main.py"]
