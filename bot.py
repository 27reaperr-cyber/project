"""
Telegram Bot: Sticker & Image Recolor
======================================
Принимает стикеры, изображения и анимации.
Перекрашивает через HSV hue-shift.
Экспортирует: GIF, MP4, PNG.
"""

import asyncio
import io
import json
import logging
import math
import os
import shutil
import sqlite3
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

import cv2
import imageio
import numpy as np
from PIL import Image, ImageSequence
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ──────────────────────────────────────────────────────────────────────────────
# НАСТРОЙКА
# ──────────────────────────────────────────────────────────────────────────────

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DEFAULT_COLOR = os.getenv("DEFAULT_COLOR", "#FF0000")
DEFAULT_FORMAT = os.getenv("DEFAULT_FORMAT", "GIF")
DB_PATH = os.getenv("DB_PATH", "database.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
log = logging.getLogger("recolor_bot")

# ──────────────────────────────────────────────────────────────────────────────
# FSM СОСТОЯНИЯ
# ──────────────────────────────────────────────────────────────────────────────

class RecolorStates(StatesGroup):
    waiting_file      = State()
    choosing_color    = State()
    choosing_format   = State()
    choosing_quality  = State()
    processing        = State()


# ──────────────────────────────────────────────────────────────────────────────
# БАЗА ДАННЫХ (SQLite, без ORM)
# ──────────────────────────────────────────────────────────────────────────────

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def db_init() -> None:
    """Создаёт таблицы если не существуют."""
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                color       TEXT    NOT NULL DEFAULT '#FF0000',
                format      TEXT    NOT NULL DEFAULT 'GIF',
                quality     TEXT    NOT NULL DEFAULT 'medium',
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                file_type   TEXT,
                color       TEXT,
                format      TEXT,
                quality     TEXT,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
    log.info("Database initialized: %s", DB_PATH)


def db_get_user(user_id: int) -> dict:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row:
            return dict(row)
        # Создаём дефолтную запись
        conn.execute(
            "INSERT INTO users (user_id, color, format, quality) VALUES (?, ?, ?, ?)",
            (user_id, DEFAULT_COLOR, DEFAULT_FORMAT, "medium"),
        )
        conn.commit()
        return {"user_id": user_id, "color": DEFAULT_COLOR,
                "format": DEFAULT_FORMAT, "quality": "medium"}


def db_update_user(user_id: int, **kwargs) -> None:
    if not kwargs:
        return
    fields = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [user_id]
    with db_connect() as conn:
        conn.execute(
            f"UPDATE users SET {fields}, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
            values,
        )
        # Upsert — если записи нет
        if conn.execute("SELECT changes()").fetchone()[0] == 0:
            conn.execute(
                "INSERT OR IGNORE INTO users (user_id, color, format, quality) VALUES (?, ?, ?, ?)",
                (user_id, DEFAULT_COLOR, DEFAULT_FORMAT, "medium"),
            )
            conn.execute(
                f"UPDATE users SET {fields}, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                values,
            )
        conn.commit()


def db_log_conversion(user_id: int, file_type: str,
                       color: str, fmt: str, quality: str) -> None:
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO conversions (user_id, file_type, color, format, quality) VALUES (?, ?, ?, ?, ?)",
            (user_id, file_type, color, fmt, quality),
        )
        conn.commit()


# ──────────────────────────────────────────────────────────────────────────────
# УТИЛИТЫ: ЦВЕТ
# ──────────────────────────────────────────────────────────────────────────────

def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """#RRGGBB → (R, G, B)"""
    hex_color = hex_color.strip().lstrip("#")
    if len(hex_color) == 3:
        hex_color = "".join(c * 2 for c in hex_color)
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return r, g, b


def rgb_to_hue(r: int, g: int, b: int) -> float:
    """Возвращает Hue в градусах [0, 360)."""
    r_, g_, b_ = r / 255.0, g / 255.0, b / 255.0
    img = np.array([[[r_, g_, b_]]], dtype=np.float32)
    hsv = cv2.cvtColor(img, cv2.COLOR_RGB2HSV)
    return float(hsv[0, 0, 0]) * 2  # OpenCV: H in [0,180]


def validate_hex(hex_color: str) -> bool:
    hex_color = hex_color.strip().lstrip("#")
    return len(hex_color) in (3, 6) and all(
        c in "0123456789abcdefABCDEF" for c in hex_color
    )


# ──────────────────────────────────────────────────────────────────────────────
# ЯДРО ОБРАБОТКИ ИЗОБРАЖЕНИЙ
# ──────────────────────────────────────────────────────────────────────────────

def recolor_frame(frame: np.ndarray, target_hue_deg: float) -> np.ndarray:
    """
    Перекрашивает один кадр (RGBA или RGB numpy array).
    Меняет только канал Hue, сохраняет Saturation и Value.

    Args:
        frame: numpy array shape (H, W, 3) RGB или (H, W, 4) RGBA, dtype uint8
        target_hue_deg: целевой Hue в градусах [0, 360)

    Returns:
        numpy array той же формы и dtype.
    """
    has_alpha = frame.shape[2] == 4
    if has_alpha:
        alpha = frame[:, :, 3:4].copy()
        rgb = frame[:, :, :3]
    else:
        rgb = frame
        alpha = None

    # RGB → HSV (OpenCV использует H: [0,180], S: [0,255], V: [0,255])
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV).astype(np.float32)

    # Целевой hue в формате OpenCV (0..180)
    target_hue_ocv = (target_hue_deg % 360.0) / 2.0

    # Сдвигаем Hue для каждого пикселя к целевому значению
    # Для пикселей с низкой насыщенностью (серые/белые/чёрные) меняем Hue мягко,
    # чтобы не получить артефакты — через взвешенный blend по Saturation.
    sat_norm = hsv[:, :, 1] / 255.0  # [0..1]

    original_hue = hsv[:, :, 0]
    # Полный hue-замена, взвешенная по насыщенности
    new_hue = target_hue_ocv * sat_norm + original_hue * (1.0 - sat_norm)
    hsv[:, :, 0] = new_hue

    hsv = np.clip(hsv, 0, 255).astype(np.uint8)
    result_rgb = cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)

    if has_alpha:
        result = np.concatenate([result_rgb, alpha], axis=2)
    else:
        result = result_rgb

    return result


def quality_params(quality: str) -> dict:
    """Возвращает параметры обработки в зависимости от качества."""
    if quality == "low":
        return {"max_size": 256, "gif_fps": 10, "mp4_crf": 28}
    elif quality == "high":
        return {"max_size": 1024, "gif_fps": 25, "mp4_crf": 18}
    else:  # medium
        return {"max_size": 512, "gif_fps": 15, "mp4_crf": 23}


def resize_keep_aspect(img: Image.Image, max_size: int) -> Image.Image:
    w, h = img.size
    if max(w, h) <= max_size:
        return img
    scale = max_size / max(w, h)
    new_w, new_h = int(w * scale), int(h * scale)
    return img.resize((new_w, new_h), Image.LANCZOS)


# ──────────────────────────────────────────────────────────────────────────────
# ОБРАБОТКА СТАТИЧНЫХ ИЗОБРАЖЕНИЙ
# ──────────────────────────────────────────────────────────────────────────────

def process_static_image(
    img_bytes: bytes,
    target_hue_deg: float,
    quality: str,
    output_format: str,
) -> bytes:
    """
    Обрабатывает статичное изображение (PNG/WEBP/JPG).
    Возвращает bytes результата.
    """
    params = quality_params(quality)
    img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    img = resize_keep_aspect(img, params["max_size"])

    frame_np = np.array(img)
    recolored_np = recolor_frame(frame_np, target_hue_deg)
    result_img = Image.fromarray(recolored_np, "RGBA")

    buf = io.BytesIO()
    if output_format == "PNG":
        result_img.save(buf, format="PNG", optimize=True)
    elif output_format == "GIF":
        # Статичный GIF
        rgb_img = Image.new("RGB", result_img.size, (0, 0, 0))
        rgb_img.paste(result_img, mask=result_img.split()[3])
        rgb_img.save(buf, format="GIF")
    elif output_format == "MP4":
        # Статичный MP4 — 3 секунды из одного кадра
        return _static_to_mp4(result_img, params)
    buf.seek(0)
    return buf.read()


def _static_to_mp4(img: Image.Image, params: dict) -> bytes:
    """Конвертирует статичное изображение в короткое MP4."""
    with tempfile.TemporaryDirectory() as tmp:
        frame_path = Path(tmp) / "frame.png"
        img.convert("RGB").save(frame_path)

        out_path = Path(tmp) / "out.mp4"
        # Создаём 3-секундное видео из одного кадра, 25 fps
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1",
            "-i", str(frame_path),
            "-t", "3",
            "-vf", f"fps=25,scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libx264",
            "-crf", str(params["mp4_crf"]),
            "-pix_fmt", "yuv420p",
            str(out_path),
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        return out_path.read_bytes()


# ──────────────────────────────────────────────────────────────────────────────
# ОБРАБОТКА АНИМИРОВАННЫХ ИЗОБРАЖЕНИЙ (GIF / WEBP анимация)
# ──────────────────────────────────────────────────────────────────────────────

def extract_frames_pil(img: Image.Image) -> list[tuple[np.ndarray, float]]:
    """
    Извлекает кадры из PIL-анимации.
    Возвращает список (frame_rgba_np, duration_sec).
    """
    frames = []
    try:
        for frame in ImageSequence.Iterator(img):
            duration_ms = frame.info.get("duration", 100)
            rgba = frame.convert("RGBA")
            frames.append((np.array(rgba), duration_ms / 1000.0))
    except EOFError:
        pass
    return frames


def process_animated(
    img_bytes: bytes,
    target_hue_deg: float,
    quality: str,
    output_format: str,
) -> bytes:
    """
    Обрабатывает анимированный GIF/WEBP.
    """
    params = quality_params(quality)
    img = Image.open(io.BytesIO(img_bytes))

    raw_frames = extract_frames_pil(img)
    if not raw_frames:
        # Fallback на статичную обработку
        return process_static_image(img_bytes, target_hue_deg, quality, output_format)

    processed_frames: list[np.ndarray] = []
    durations: list[float] = []

    for frame_np, dur in raw_frames:
        # Ресайз
        pil_f = Image.fromarray(frame_np, "RGBA")
        pil_f = resize_keep_aspect(pil_f, params["max_size"])
        frame_np_resized = np.array(pil_f)

        recolored = recolor_frame(frame_np_resized, target_hue_deg)
        processed_frames.append(recolored)
        durations.append(dur)

    if output_format == "GIF":
        return _frames_to_gif(processed_frames, durations)
    elif output_format == "MP4":
        fps = params["gif_fps"]
        return _frames_to_mp4(processed_frames, fps, params)
    elif output_format == "PNG":
        # Возвращаем первый кадр как PNG
        buf = io.BytesIO()
        Image.fromarray(processed_frames[0], "RGBA").save(buf, format="PNG")
        buf.seek(0)
        return buf.read()

    return b""


def _frames_to_gif(
    frames: list[np.ndarray],
    durations: list[float],
) -> bytes:
    """
    Собирает GIF из RGBA-кадров через imageio.
    Сохраняет прозрачность через palette-режим Pillow.
    """
    pil_frames: list[Image.Image] = []
    for f in frames:
        img = Image.fromarray(f, "RGBA")
        # Конвертируем в палитровый с прозрачностью
        converted = img.convert("P", palette=Image.ADAPTIVE, colors=255)
        converted.info["transparency"] = 0
        pil_frames.append(converted)

    buf = io.BytesIO()
    pil_frames[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=pil_frames[1:],
        duration=[int(d * 1000) for d in durations],
        loop=0,
        disposal=2,
    )
    buf.seek(0)
    return buf.read()


def _frames_to_mp4(
    frames: list[np.ndarray],
    fps: float,
    params: dict,
) -> bytes:
    """
    Собирает MP4 из RGBA-кадров через ffmpeg.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        frames_dir = tmp_path / "frames"
        frames_dir.mkdir()

        for i, frame in enumerate(frames):
            pil_img = Image.fromarray(frame, "RGBA").convert("RGB")
            pil_img.save(frames_dir / f"frame_{i:06d}.png")

        out_path = tmp_path / "out.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-framerate", str(fps),
            "-i", str(frames_dir / "frame_%06d.png"),
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libx264",
            "-crf", str(params["mp4_crf"]),
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(out_path),
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        return out_path.read_bytes()


# ──────────────────────────────────────────────────────────────────────────────
# ОБРАБОТКА TGS (Lottie-анимации)
# ──────────────────────────────────────────────────────────────────────────────

def _try_import_lottie():
    try:
        from lottie import parsers as lottie_parsers
        from lottie.exporters import exporters as lottie_exporters
        return lottie_parsers, lottie_exporters
    except ImportError:
        return None, None


def process_tgs(
    tgs_bytes: bytes,
    target_hue_deg: float,
    quality: str,
    output_format: str,
) -> Optional[bytes]:
    """
    Обрабатывает TGS (gzip-сжатый Lottie JSON).
    Пытается рендерить через lottie-python, иначе возвращает None.
    """
    import gzip

    lottie_parsers, lottie_exporters = _try_import_lottie()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # Декомпрессия TGS → JSON
        try:
            json_bytes = gzip.decompress(tgs_bytes)
        except Exception:
            log.warning("TGS: не удалось декомпрессировать")
            return None

        if lottie_parsers is None:
            # Fallback: попробуем рендерить через lottie-python если он установлен иначе skip
            log.warning("lottie не установлен, TGS не поддерживается")
            return None

        try:
            json_path = tmp_path / "anim.json"
            json_path.write_bytes(json_bytes)

            animation = lottie_parsers.parse(str(json_path))
            fps = animation.frame_rate
            total_frames = int(animation.out_point - animation.in_point)

            frames_dir = tmp_path / "frames"
            frames_dir.mkdir()

            params = quality_params(quality)

            from lottie.exporters.cairo import export_png

            processed_frames = []
            durations = []
            frame_duration = 1.0 / fps

            for i in range(total_frames):
                frame_path = frames_dir / f"frame_{i:06d}.png"
                export_png(animation, str(frame_path), frame=i)
                pil_img = Image.open(frame_path).convert("RGBA")
                pil_img = resize_keep_aspect(pil_img, params["max_size"])
                frame_np = np.array(pil_img)
                recolored = recolor_frame(frame_np, target_hue_deg)
                processed_frames.append(recolored)
                durations.append(frame_duration)

            if output_format == "GIF":
                return _frames_to_gif(processed_frames, durations)
            elif output_format == "MP4":
                return _frames_to_mp4(processed_frames, fps, params)
            elif output_format == "PNG":
                buf = io.BytesIO()
                Image.fromarray(processed_frames[0], "RGBA").save(buf, format="PNG")
                buf.seek(0)
                return buf.read()
        except Exception as e:
            log.error("Ошибка обработки TGS: %s", e)
            return None


# ──────────────────────────────────────────────────────────────────────────────
# ГЛАВНЫЙ ДИСПЕТЧЕР ОБРАБОТКИ
# ──────────────────────────────────────────────────────────────────────────────

async def process_file(
    file_bytes: bytes,
    file_name: str,
    hex_color: str,
    output_format: str,
    quality: str,
) -> tuple[bytes, str]:
    """
    Основная точка входа обработки файла.

    Returns:
        (result_bytes, mime_extension)
    """
    target_hue = rgb_to_hue(*hex_to_rgb(hex_color))
    ext = Path(file_name).suffix.lower()

    log.info(
        "Processing: file=%s ext=%s color=%s hue=%.1f° format=%s quality=%s",
        file_name, ext, hex_color, target_hue, output_format, quality,
    )

    # Определяем тип файла
    is_animated = False

    if ext == ".tgs":
        result = await asyncio.to_thread(
            process_tgs, file_bytes, target_hue, quality, output_format
        )
        if result is None:
            raise ValueError(
                "TGS-формат не поддерживается: установите lottie-python в Docker-образ"
            )
    else:
        # Проверяем анимированность через PIL
        try:
            img = Image.open(io.BytesIO(file_bytes))
            try:
                img.seek(1)
                is_animated = True
                img.seek(0)
            except EOFError:
                is_animated = False
        except Exception:
            is_animated = False

        if is_animated:
            result = await asyncio.to_thread(
                process_animated, file_bytes, target_hue, quality, output_format
            )
        else:
            result = await asyncio.to_thread(
                process_static_image, file_bytes, target_hue, quality, output_format
            )

    ext_map = {"GIF": "gif", "MP4": "mp4", "PNG": "png"}
    out_ext = ext_map.get(output_format, "gif")
    return result, out_ext


# ──────────────────────────────────────────────────────────────────────────────
# КЛАВИАТУРЫ
# ──────────────────────────────────────────────────────────────────────────────

def kb_main_menu(user: dict) -> InlineKeyboardMarkup:
    color = user.get("color", DEFAULT_COLOR)
    fmt = user.get("format", DEFAULT_FORMAT)
    quality = user.get("quality", "medium")

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"🎨 Цвет: {color}",
                callback_data="set_color"
            ),
        ],
        [
            InlineKeyboardButton(
                text=f"🧾 Формат: {fmt}",
                callback_data="set_format"
            ),
            InlineKeyboardButton(
                text=f"⚙️ Качество: {quality}",
                callback_data="set_quality"
            ),
        ],
        [
            InlineKeyboardButton(text="👁 Превью",      callback_data="preview"),
            InlineKeyboardButton(text="🚀 Конвертировать", callback_data="convert"),
        ],
    ])


def kb_format_select() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎞 GIF", callback_data="fmt_GIF"),
            InlineKeyboardButton(text="🎬 MP4", callback_data="fmt_MP4"),
            InlineKeyboardButton(text="🖼 PNG", callback_data="fmt_PNG"),
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_menu")],
    ])


def kb_quality_select() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔻 Низкое",  callback_data="qual_low"),
            InlineKeyboardButton(text="⚖️ Среднее", callback_data="qual_medium"),
            InlineKeyboardButton(text="💎 Высокое", callback_data="qual_high"),
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_menu")],
    ])


def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="back_menu")]
    ])


# ──────────────────────────────────────────────────────────────────────────────
# РОУТЕР / ХЕНДЛЕРЫ
# ──────────────────────────────────────────────────────────────────────────────

router = Router()


# ── /start ─────────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    user = db_get_user(user_id)
    await state.set_state(RecolorStates.waiting_file)
    await message.answer(
        "👋 Привет! Я бот для перекраски стикеров и изображений.\n\n"
        "📎 Отправь мне:\n"
        "  • Стикер (WEBP / TGS)\n"
        "  • Изображение (PNG / JPG)\n"
        "  • Анимацию (GIF)\n\n"
        "Затем настрой цвет и формат:",
        reply_markup=kb_main_menu(user),
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "ℹ️ <b>Как пользоваться:</b>\n\n"
        "1. Отправь стикер или изображение\n"
        "2. Выбери цвет (HEX, например <code>#FF5500</code>)\n"
        "3. Выбери формат: GIF / MP4 / PNG\n"
        "4. Нажми <b>🚀 Конвертировать</b>\n\n"
        "<b>Форматы вывода:</b>\n"
        "  🎞 GIF — анимированный\n"
        "  🎬 MP4 — видео\n"
        "  🖼 PNG — статичное изображение\n\n"
        "<b>Качество:</b>\n"
        "  Влияет на размер и детализацию результата",
    )


@router.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    user = db_get_user(message.from_user.id)
    await message.answer(
        f"⚙️ <b>Текущие настройки:</b>\n\n"
        f"🎨 Цвет: <code>{user['color']}</code>\n"
        f"🧾 Формат: {user['format']}\n"
        f"⚙️ Качество: {user['quality']}",
        reply_markup=kb_main_menu(user),
    )


# ── Приём файлов ───────────────────────────────────────────────────────────────

async def _handle_media(
    message: Message,
    state: FSMContext,
    file_id: str,
    file_name: str,
    file_type: str,
) -> None:
    """Общий обработчик входящего файла."""
    user = db_get_user(message.from_user.id)
    await state.update_data(
        file_id=file_id,
        file_name=file_name,
        file_type=file_type,
    )
    await state.set_state(RecolorStates.waiting_file)
    await message.answer(
        f"✅ Получен файл: <code>{file_name}</code> ({file_type})\n\n"
        f"Настрой параметры и нажми <b>🚀 Конвертировать</b>:",
        reply_markup=kb_main_menu(user),
    )


@router.message(F.sticker)
async def handle_sticker(message: Message, state: FSMContext) -> None:
    sticker = message.sticker
    file_name = f"sticker.{'tgs' if sticker.is_animated else 'webp'}"
    file_type = "TGS" if sticker.is_animated else "WEBP"
    await _handle_media(
        message, state,
        sticker.file_id, file_name, file_type
    )


@router.message(F.animation)
async def handle_animation(message: Message, state: FSMContext) -> None:
    anim = message.animation
    file_name = anim.file_name or "animation.gif"
    await _handle_media(
        message, state,
        anim.file_id, file_name, "GIF"
    )


@router.message(F.photo)
async def handle_photo(message: Message, state: FSMContext) -> None:
    photo = message.photo[-1]  # Наибольший размер
    await _handle_media(
        message, state,
        photo.file_id, "photo.jpg", "JPG"
    )


@router.message(F.document)
async def handle_document(message: Message, state: FSMContext) -> None:
    doc = message.document
    fname = doc.file_name or "file"
    ext = Path(fname).suffix.lower()
    allowed = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".tgs"}
    if ext not in allowed:
        await message.answer(
            f"⚠️ Формат <b>{ext}</b> не поддерживается.\n"
            "Поддерживаются: PNG, JPG, WEBP, GIF, TGS"
        )
        return
    await _handle_media(
        message, state,
        doc.file_id, fname, ext.lstrip(".").upper()
    )


# ── Inline кнопки ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "set_color")
async def cb_set_color(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(RecolorStates.choosing_color)
    await call.message.edit_text(
        "🎨 Введи цвет в формате HEX:\n"
        "Например: <code>#FF5500</code> или <code>#0AF</code>",
        reply_markup=kb_cancel(),
    )
    await call.answer()


@router.message(RecolorStates.choosing_color)
async def process_color_input(message: Message, state: FSMContext) -> None:
    color = message.text.strip()
    if not validate_hex(color):
        await message.answer(
            "❌ Неверный формат. Введи HEX-цвет, например: <code>#FF5500</code>"
        )
        return

    if not color.startswith("#"):
        color = "#" + color

    db_update_user(message.from_user.id, color=color)
    user = db_get_user(message.from_user.id)
    await state.set_state(RecolorStates.waiting_file)
    await message.answer(
        f"✅ Цвет установлен: <code>{color}</code>",
        reply_markup=kb_main_menu(user),
    )


@router.callback_query(F.data == "set_format")
async def cb_set_format(call: CallbackQuery, state: FSMContext) -> None:
    await call.message.edit_text(
        "🧾 Выбери формат экспорта:",
        reply_markup=kb_format_select(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("fmt_"))
async def cb_format_selected(call: CallbackQuery, state: FSMContext) -> None:
    fmt = call.data.split("_", 1)[1]
    db_update_user(call.from_user.id, format=fmt)
    user = db_get_user(call.from_user.id)
    await call.message.edit_text(
        f"✅ Формат установлен: <b>{fmt}</b>",
        reply_markup=kb_main_menu(user),
    )
    await call.answer(f"Формат: {fmt}")


@router.callback_query(F.data == "set_quality")
async def cb_set_quality(call: CallbackQuery, state: FSMContext) -> None:
    await call.message.edit_text(
        "⚙️ Выбери качество обработки:",
        reply_markup=kb_quality_select(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("qual_"))
async def cb_quality_selected(call: CallbackQuery, state: FSMContext) -> None:
    qual = call.data.split("_", 1)[1]
    db_update_user(call.from_user.id, quality=qual)
    user = db_get_user(call.from_user.id)
    labels = {"low": "Низкое 🔻", "medium": "Среднее ⚖️", "high": "Высокое 💎"}
    await call.message.edit_text(
        f"✅ Качество: <b>{labels.get(qual, qual)}</b>",
        reply_markup=kb_main_menu(user),
    )
    await call.answer(f"Качество: {qual}")


@router.callback_query(F.data == "back_menu")
async def cb_back_menu(call: CallbackQuery, state: FSMContext) -> None:
    user = db_get_user(call.from_user.id)
    await state.set_state(RecolorStates.waiting_file)
    await call.message.edit_text(
        "📎 Отправь файл или настрой параметры:",
        reply_markup=kb_main_menu(user),
    )
    await call.answer()


# ── Превью ─────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "preview")
async def cb_preview(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    file_id = data.get("file_id")
    file_name = data.get("file_name", "file")

    if not file_id:
        await call.answer("⚠️ Сначала отправь файл!", show_alert=True)
        return

    user = db_get_user(call.from_user.id)
    await call.answer("Генерирую превью...")

    try:
        # Для превью всегда PNG, низкое качество
        tg_file = await bot.get_file(file_id)
        buf = io.BytesIO()
        await bot.download_file(tg_file.file_path, buf)
        file_bytes = buf.getvalue()

        result, _ = await process_file(
            file_bytes=file_bytes,
            file_name=file_name,
            hex_color=user["color"],
            output_format="PNG",
            quality="low",
        )

        await call.message.answer_photo(
            BufferedInputFile(result, filename="preview.png"),
            caption=f"👁 Превью | Цвет: {user['color']}",
        )
    except Exception as e:
        log.error("Preview error: %s", e, exc_info=True)
        await call.message.answer(f"❌ Ошибка превью: {e}")


# ── Конвертировать ─────────────────────────────────────────────────────────────

@router.callback_query(F.data == "convert")
async def cb_convert(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    file_id = data.get("file_id")
    file_name = data.get("file_name", "file")

    if not file_id:
        await call.answer("⚠️ Сначала отправь файл!", show_alert=True)
        return

    user = db_get_user(call.from_user.id)
    fmt = user["format"]
    quality = user["quality"]
    color = user["color"]

    await call.answer("⏳ Начинаю обработку...")
    status_msg = await call.message.answer(
        f"⏳ Обрабатываю...\n"
        f"🎨 Цвет: {color}\n"
        f"🧾 Формат: {fmt}\n"
        f"⚙️ Качество: {quality}"
    )

    try:
        await state.set_state(RecolorStates.processing)

        tg_file = await bot.get_file(file_id)
        buf = io.BytesIO()
        await bot.download_file(tg_file.file_path, buf)
        file_bytes = buf.getvalue()

        result_bytes, out_ext = await process_file(
            file_bytes=file_bytes,
            file_name=file_name,
            hex_color=color,
            output_format=fmt,
            quality=quality,
        )

        out_name = f"recolored_{color.lstrip('#')}.{out_ext}"

        if fmt == "GIF":
            await call.message.answer_animation(
                BufferedInputFile(result_bytes, filename=out_name),
                caption=f"✅ Готово! Цвет: {color}",
            )
        elif fmt == "MP4":
            await call.message.answer_video(
                BufferedInputFile(result_bytes, filename=out_name),
                caption=f"✅ Готово! Цвет: {color}",
            )
        elif fmt == "PNG":
            await call.message.answer_photo(
                BufferedInputFile(result_bytes, filename=out_name),
                caption=f"✅ Готово! Цвет: {color}",
            )

        db_log_conversion(
            user_id=call.from_user.id,
            file_type=data.get("file_type", "unknown"),
            color=color,
            fmt=fmt,
            quality=quality,
        )

    except subprocess.CalledProcessError as e:
        log.error("FFmpeg error: %s", e.stderr)
        await status_msg.edit_text(
            "❌ Ошибка FFmpeg при создании видео.\n"
            "Убедитесь, что ffmpeg установлен в Docker-образе."
        )
    except Exception as e:
        log.error("Conversion error: %s", e, exc_info=True)
        await status_msg.edit_text(f"❌ Ошибка обработки: {e}")
    finally:
        await state.set_state(RecolorStates.waiting_file)
        try:
            await status_msg.delete()
        except Exception:
            pass


# ── Обработка неизвестных сообщений ────────────────────────────────────────────

@router.message(RecolorStates.waiting_file)
async def handle_text_in_wait(message: Message, state: FSMContext) -> None:
    user = db_get_user(message.from_user.id)
    await message.answer(
        "📎 Отправь стикер, изображение или анимацию (GIF, PNG, JPG, WEBP).",
        reply_markup=kb_main_menu(user),
    )


@router.message()
async def fallback_handler(message: Message, state: FSMContext) -> None:
    await cmd_start(message, state)


# ──────────────────────────────────────────────────────────────────────────────
# ЗАПУСК
# ──────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан в .env!")

    db_init()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    log.info("Bot starting...")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        log.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
