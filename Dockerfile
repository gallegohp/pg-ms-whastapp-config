FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    curl \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

ENV CHROME_BIN=/usr/bin/chromium \
    CHROMEDRIVER_BIN=/usr/bin/chromedriver

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

ENV CHROME_PROFILE_DIR=/app/chrome-profile \
    QR_PATH=/app/data/qr.png \
    HEADLESS=true

EXPOSE 8095

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8095"]
