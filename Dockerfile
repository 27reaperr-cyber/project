# ──────────────────────────────────────────────────────
# Telegram Recolor Bot — Dockerfile
# ──────────────────────────────────────────────────────
FROM python:3.11-slim

# Системные зависимости:
#   ffmpeg        — сборка MP4 / GIF
#   gcc           — компиляция C-расширений Python
#   libgl1        — OpenCV (headless работает без X11, но libgl нужен)
#   libglib2.0-0  — зависимость OpenCV
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        gcc \
        libgl1 \
        libglib2.0-0 \
        libcairo2-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала копируем только requirements для кэша слоёв
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY bot.py .

# База данных создаётся при первом запуске в /app/database.db
# Через volume-mount можно сохранять между перезапусками

ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
