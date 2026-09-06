FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY challenges ./challenges
COPY completion_pictures ./completion_pictures

RUN mkdir -p /app/data

ENV DATABASE_PATH=/app/data/challenges.db
ENV CHALLENGE_CATALOG=/app/challenges/2026.yaml
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

CMD ["python", "-m", "app.main"]
