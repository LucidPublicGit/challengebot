FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    fonts-dejavu-core \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY challenges ./challenges
COPY achievements ./achievements
COPY completion_pictures ./completion_pictures
COPY new_challenger ./new_challenger

RUN groupadd --gid 10001 tracker \
    && useradd --uid 10001 --gid tracker --no-create-home tracker \
    && mkdir -p /app/data \
    && chown tracker:tracker /app/data

ENV DATABASE_PATH=/app/data/challenges.db
ENV CHALLENGE_CATALOG=/app/challenges/2026.yaml
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV PYTHONDONTWRITEBYTECODE=1

USER tracker:tracker

CMD ["python", "-m", "app.main"]
