FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app
RUN chmod +x /app/ambot_aiosqlite.py

ENV BOT_TOKEN=""
ENV ADMIN_TELEGRAM_ID=""

CMD ["python", "ambot_aiosqlite.py"]
