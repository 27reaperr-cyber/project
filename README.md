# 🎛 StickerForge Bot

Telegram-бот для экспорта анимированных TGS стикеров и premium эмодзи в **GIF / MOV / PNG**
с поддержкой кастомного фона, тонирования цвета (умный hue-shift) и вотермарки.

---

## Функции

| Параметр | Описание |
|---|---|
| **Фон** | Любой HEX-цвет, например `#1A1A2E` |
| **Цвет стикера** | Hue-shift: сдвигает оттенок, сохраняя тени и блики |
| **Вотермарка** | Текст с выбором шрифта (Montserrat), автопозиция |
| **Разрешение** | Пресеты + ввод произвольного `ШxВ` |
| **FPS** | 24 / 30 / 60 |
| **Формат** | GIF / MOV (H.264) / PNG (средний кадр) |

---

## Установка

### 1. Клонировать / скопировать файлы

```
tgs_bot/
├── bot.py
├── handlers.py
├── render_utils.py
├── keyboards.py
├── states.py
├── config.py
├── requirements.txt
├── .env
└── fonts/
    └── Montserrat.ttf        ← скачать ниже
```

### 2. Создать виртуальное окружение

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Установить системные зависимости

**Ubuntu / Debian:**
```bash
sudo apt install ffmpeg
```

**macOS (Homebrew):**
```bash
brew install ffmpeg
```

**Windows:** скачай ffmpeg с https://ffmpeg.org/download.html и добавь в PATH.

> **ffmpeg нужен только для MOV.** GIF и PNG работают без него.

### 4. Скачать шрифт Montserrat

```bash
mkdir -p fonts
curl -L "https://github.com/google/fonts/raw/main/ofl/montserrat/Montserrat-Regular.ttf" \
     -o fonts/Montserrat.ttf
```

Или вручную: https://fonts.google.com/specimen/Montserrat → Download family

### 5. Настроить `.env`

```bash
cp .env.example .env
# Открой .env и вставь токен от @BotFather
```

```
BOT_TOKEN=1234567890:ABCDefghIJKlmnopQRSTuvwxyz
TEMP_DIR=/tmp/stickerbot
```

### 6. Запустить

```bash
python bot.py
```

---

## Использование

1. Напиши боту `/start`
2. Отправь **анимированный TGS стикер** — или — **ID premium эмодзи** (10–20 цифр, берётся из Telegram)
3. В меню настрой параметры (каждая кнопка редактирует то же самое сообщение)
4. Нажми **🚀 Конвертировать**

---

## Архитектура

```
handlers.py        — aiogram 3 handlers + FSM flow (self-editing menu)
render_utils.py    — TGS рендер → PIL frames → tint → watermark → encode
keyboards.py       — все inline-клавиатуры
states.py          — FSM-состояния
config.py          — конфигурация
bot.py             — точка входа
```

### Алгоритм тонирования

Используется **hue-only shift** в пространстве HSV:

- **H** (оттенок) → заменяется целевым оттенком
- **S** (насыщенность) → сохраняется из оригинала
- **V** (яркость) → сохраняется из оригинала

Это аналог эффекта «Colorize» в Photoshop: стикер сохраняет все тени,
блики и детали, но меняет цветовую гамму.

---

## Зависимости

| Пакет | Назначение |
|---|---|
| `aiogram` 3.x | Telegram Bot API framework |
| `rlottie-python` | Рендер TGS/Lottie в PIL-кадры |
| `Pillow` | Обработка изображений, GIF/PNG |
| `numpy` | Векторизованное HSV-преобразование |
| `python-dotenv` | Загрузка `.env` |
| `ffmpeg` (system) | Кодирование MOV |

---

## Troubleshooting

**`rlottie-python` не устанавливается** — убедись что используешь Python 3.9–3.12.
На некоторых ARM-системах может потребоваться сборка из исходников.

**Ошибка ffmpeg при MOV** — проверь `ffmpeg --version` в терминале.

**Шрифт не найден** — бот использует встроенный шрифт PIL (менее красивый).
Скачай `Montserrat.ttf` в папку `fonts/`.
