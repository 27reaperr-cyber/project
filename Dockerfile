FROM python:3.11-slim

# System dependencies: ffmpeg + build tools for rlottie-python
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    gcc \
    g++ \
    cmake \
    make \
    libpng-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Font directory (Montserrat.ttf goes here)
RUN mkdir -p fonts

ENV TEMP_DIR=/tmp/stickerbot

CMD ["python", "bot.py"]
