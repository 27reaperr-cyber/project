"""
Telegram Banner Bot v2.0
========================
Создаёт GIF/MP4/PNG баннеры из стикеров и premium emoji.
• Перекраска через HSV hue-shift
• Кастомный фон (цвет или своё фото)
• Заметки и вотермарка поверх баннера
• TGS через lottie 0.7.x + Cairo
"""

from __future__ import annotations

import asyncio
import gzip
import html
import io
import json as json_mod
import logging
import os
import re
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageSequence
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
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

# ══════════════════════════════════════════════════════════════════════════════
# КОНФИГУРАЦИЯ
# ══════════════════════════════════════════════════════════════════════════════

load_dotenv()

BOT_TOKEN   = os.getenv("BOT_TOKEN", "")
DEFAULT_BG  = os.getenv("DEFAULT_BG_COLOR",    "#FF5E3B")
DEFAULT_EC  = os.getenv("DEFAULT_EMOJI_COLOR", "#FFFFFF")
DEFAULT_FMT = os.getenv("DEFAULT_FORMAT",      "GIF")
DEFAULT_RES = os.getenv("DEFAULT_RESOLUTION",  "1920x530")
DEFAULT_FPS = int(os.getenv("DEFAULT_FPS",     "30"))
DB_PATH     = os.getenv("DB_PATH",             "database.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
log = logging.getLogger("banner_bot")

# ══════════════════════════════════════════════════════════════════════════════
# FSM
# ══════════════════════════════════════════════════════════════════════════════

class S(StatesGroup):
    idle      = State()
    set_bg    = State()
    set_res   = State()
    set_ec    = State()
    set_notes = State()
    set_wm    = State()
    set_media = State()

# ══════════════════════════════════════════════════════════════════════════════
# БД  (SQLite, без ORM)
# ══════════════════════════════════════════════════════════════════════════════

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def db_init() -> None:
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                bg_color    TEXT    DEFAULT '#FF5E3B',
                emoji_color TEXT    DEFAULT '#FFFFFF',
                resolution  TEXT    DEFAULT '1920x530',
                fps         INTEGER DEFAULT 30,
                format      TEXT    DEFAULT 'GIF',
                notes       TEXT    DEFAULT '',
                watermark   TEXT    DEFAULT '',
                media_fid   TEXT    DEFAULT NULL,
                updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER,
                file_type  TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.commit()
    log.info("DB ready: %s", DB_PATH)


def db_user(uid: int) -> dict:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        if row:
            return dict(row)
        c.execute("INSERT INTO users (user_id) VALUES (?)", (uid,))
        c.commit()
        return {
            "user_id": uid,
            "bg_color": DEFAULT_BG, "emoji_color": DEFAULT_EC,
            "resolution": DEFAULT_RES, "fps": DEFAULT_FPS, "format": DEFAULT_FMT,
            "notes": "", "watermark": "", "media_fid": None,
        }


def db_set(uid: int, **kw) -> None:
    if not kw:
        return
    fields = ", ".join(f"{k}=?" for k in kw)
    vals = list(kw.values()) + [uid]
    with _conn() as c:
        c.execute(
            f"UPDATE users SET {fields}, updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
            vals,
        )
        if c.execute("SELECT changes()").fetchone()[0] == 0:
            c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
            c.execute(
                f"UPDATE users SET {fields}, updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
                vals,
            )
        c.commit()


def db_log(uid: int, ftype: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO history (user_id, file_type) VALUES (?,?)", (uid, ftype)
        )
        c.commit()

# ══════════════════════════════════════════════════════════════════════════════
# ЦВЕТ
# ══════════════════════════════════════════════════════════════════════════════

def hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[:2], 16), int(h[2:4], 16), int(h[4:], 16)


def rgb_to_hsv_norm(r: int, g: int, b: int) -> tuple[float, float]:
    """Возвращает (hue_degrees, saturation_0_1) для заданного RGB."""
    px  = np.array([[[r / 255, g / 255, b / 255]]], dtype=np.float32)
    hsv = cv2.cvtColor(px, cv2.COLOR_RGB2HSV)[0, 0]
    hue = float(hsv[0]) * 2.0        # OpenCV H [0,180] → [0,360]
    sat = float(hsv[1]) / 255.0      # [0,1]
    return hue, sat


def valid_hex(h: str) -> bool:
    h = h.strip().lstrip("#")
    return len(h) in (3, 6) and all(c in "0123456789abcdefABCDEF" for c in h)


def norm_hex(h: str) -> str:
    h = h.strip()
    return ("#" + h if not h.startswith("#") else h).upper()

# ══════════════════════════════════════════════════════════════════════════════
# ОБРАБОТКА ИЗОБРАЖЕНИЙ
# ══════════════════════════════════════════════════════════════════════════════

def recolor(frame: np.ndarray, hue_deg: float, target_sat: float = 1.0) -> np.ndarray:
    """
    HSV hue-shift: меняет только Hue, плавно взвешенный по Saturation.
    target_sat: насыщенность целевого цвета [0..1].
      Если цель нейтральная (белый/чёрный/серый) — только яркостная коррекция,
      без сдвига Hue, чтобы не давать артефактный красный.
    """
    has_a = frame.ndim == 3 and frame.shape[2] == 4
    alpha = frame[:, :, 3:4].copy() if has_a else None
    rgb   = frame[:, :, :3].copy()

    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV).astype(np.float32)

    if target_sat > 0.08:          # цветной таргет → полноценный hue-shift
        tgt = (hue_deg % 360.0) / 2.0
        w   = hsv[:, :, 1] / 255.0
        hsv[:, :, 0] = tgt * w + hsv[:, :, 0] * (1.0 - w)
        # усиливаем насыщенность пропорционально target_sat
        hsv[:, :, 1] = np.clip(hsv[:, :, 1] * (0.5 + target_sat), 0, 255)
    # neutral target → оставляем Hue и Sat без изменений (натуральные тона)

    hsv = np.clip(hsv, 0, 255).astype(np.uint8)
    out = cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)
    return np.concatenate([out, alpha], axis=2) if has_a else out


def fit_image(img: Image.Image, mw: int, mh: int) -> Image.Image:
    w, h = img.size
    s = min(mw / w, mh / h, 1.0)
    return img.resize((int(w * s), int(h * s)), Image.LANCZOS) if s < 1.0 else img


def get_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for fp in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ):
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                pass
    return ImageFont.load_default()


def compose_frame(
    sticker: np.ndarray,
    bg_hex: str,
    cw: int, ch: int,
    notes: str = "",
    watermark: str = "",
    bg_img: Optional[Image.Image] = None,
) -> np.ndarray:
    """Накладывает перекрашенный стикер на фон, рисует текст. → RGBA ndarray."""

    # ── Фон ──────────────────────────────────────────────────────────────────
    if bg_img is not None:
        canvas = bg_img.convert("RGBA").resize((cw, ch), Image.LANCZOS)
    else:
        canvas = Image.new("RGBA", (cw, ch), (*hex_to_rgb(bg_hex), 255))

    # ── Стикер ───────────────────────────────────────────────────────────────
    mode = "RGBA" if sticker.shape[2] == 4 else "RGB"
    sk = Image.fromarray(sticker, mode)
    sk = fit_image(sk, int(cw * 0.60), int(ch * 0.85))
    sx, sy = (cw - sk.width) // 2, (ch - sk.height) // 2
    canvas.paste(sk, (sx, sy), sk if sk.mode == "RGBA" else None)

    draw = ImageDraw.Draw(canvas)

    # ── Заметки ───────────────────────────────────────────────────────────────
    if notes:
        fs = max(16, ch // 22)
        mg = max(10, ch // 25)
        draw.text((mg, ch - mg - fs), notes,
                  font=get_font(fs), fill=(255, 255, 255, 210))

    # ── Вотермарка ────────────────────────────────────────────────────────────
    if watermark:
        fs  = max(13, ch // 35)
        fnt = get_font(fs)
        bb  = draw.textbbox((0, 0), watermark, font=fnt)
        tw  = bb[2] - bb[0]
        mg  = max(8, ch // 30)
        draw.text((cw - tw - mg, ch - mg - fs), watermark,
                  font=fnt, fill=(255, 255, 255, 160))

    return np.array(canvas)

# ══════════════════════════════════════════════════════════════════════════════
# СБОРКА GIF / MP4
# ══════════════════════════════════════════════════════════════════════════════

FrameList = list[tuple[np.ndarray, float]]   # (RGBA ndarray, duration_sec)


def extract_pil_frames(img: Image.Image) -> FrameList:
    result: FrameList = []
    try:
        for f in ImageSequence.Iterator(img):
            dur = max(f.info.get("duration", 50) / 1000.0, 0.02)
            result.append((np.array(f.convert("RGBA")), dur))
    except EOFError:
        pass
    return result


def frames_to_gif(frames: list[np.ndarray], durations: list[float]) -> bytes:
    pils: list[Image.Image] = []
    for f in frames:
        p = Image.fromarray(f, "RGBA" if f.shape[2] == 4 else "RGB") \
                 .convert("P", palette=Image.ADAPTIVE, colors=255)
        p.info["transparency"] = 0
        pils.append(p)
    buf = io.BytesIO()
    pils[0].save(
        buf, format="GIF", save_all=True, append_images=pils[1:],
        duration=[int(d * 1000) for d in durations], loop=0, disposal=2,
    )
    buf.seek(0)
    return buf.read()


def frames_to_mp4(frames: list[np.ndarray], fps: float, crf: int = 23) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        fd = Path(tmp) / "fr"
        fd.mkdir()
        for i, f in enumerate(frames):
            mode = "RGBA" if f.ndim == 3 and f.shape[2] == 4 else "RGB"
            Image.fromarray(f, mode).convert("RGB").save(fd / f"f{i:06d}.png")
        out = Path(tmp) / "out.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-framerate", str(fps),
            "-i", str(fd / "f%06d.png"),
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libx264", "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(out),
        ], check=True, capture_output=True)
        return out.read_bytes()

# ══════════════════════════════════════════════════════════════════════════════
# TGS  (lottie 0.7.x + pycairo)
# ══════════════════════════════════════════════════════════════════════════════

def tgs_frames(tgs_bytes: bytes, max_frames: int = 150) -> FrameList:
    """
    Рендерит TGS-анимацию через rlottie-python (родной движок Telegram).
    Возвращает список (RGBA ndarray, duration_sec).
    """
    try:
        from rlottie_python import LottieAnimation
    except ImportError as e:
        log.warning("rlottie-python не установлен: %s", e)
        return []

    try:
        with tempfile.TemporaryDirectory() as tmp:
            tgs_path = os.path.join(tmp, "anim.tgs")
            with open(tgs_path, "wb") as f:
                f.write(tgs_bytes)

            anim        = LottieAnimation.from_tgs(tgs_path)
            fps         = anim.lottie_animation_get_framerate()
            total       = anim.lottie_animation_get_totalframe()
            width, height = anim.lottie_animation_get_size()

            if total <= 0:
                log.warning("TGS: totalframe=0")
                return []

            step      = max(1, total // max_frames)
            frame_dur = step / max(fps, 1)
            result: FrameList = []

            for i in range(0, total, step):
                try:
                    pil_img = anim.render_pillow_frame(frame_num=i)
                    result.append((np.array(pil_img.convert("RGBA")), frame_dur))
                except Exception as ex:
                    log.debug("TGS frame %d: %s", i, ex)

            log.info("TGS: %d кадров @ %.1f fps (%dx%d)", len(result), fps, width, height)
            return result
    except Exception as e:
        log.error("TGS render error: %s", e, exc_info=True)
        return []

# ══════════════════════════════════════════════════════════════════════════════
# ГЛАВНЫЙ ПАЙПЛАЙН
# ══════════════════════════════════════════════════════════════════════════════

def _parse_res(s: str) -> tuple[int, int]:
    m = re.match(r"(\d+)[x×](\d+)", s.lower())
    return (int(m.group(1)), int(m.group(2))) if m else (1920, 530)


def _preview_size(w: int, h: int) -> tuple[int, int]:
    s = min(800 / w, 360 / h, 1.0)
    return int(w * s), int(h * s)


async def _download(bot: Bot, file_id: str) -> bytes:
    f = await bot.get_file(file_id)
    buf = io.BytesIO()
    await bot.download_file(f.file_path, buf)
    return buf.getvalue()


async def build_banner(
    bot: Bot,
    file_id: str,
    file_name: str,
    user: dict,
    preview: bool = False,
) -> tuple[bytes, str]:
    """
    Скачивает файл → декодирует кадры → перекрашивает →
    компонует баннер → экспортирует GIF / MP4 / PNG.
    Возвращает (байты, расширение).
    """
    raw  = await _download(bot, file_id)
    ext  = Path(file_name).suffix.lower()

    bg   = user.get("bg_color",    DEFAULT_BG)
    ec   = user.get("emoji_color", DEFAULT_EC)
    fmt  = "PNG" if preview else user.get("format", DEFAULT_FMT)
    fps  = int(user.get("fps", DEFAULT_FPS))
    note = user.get("notes",     "") or ""
    wm   = user.get("watermark", "") or ""
    cw, ch = _parse_res(user.get("resolution", DEFAULT_RES))
    if preview:
        cw, ch = _preview_size(cw, ch)

    crf = 20 if fps >= 60 else 23

    # Загружаем фоновое изображение пользователя
    bg_img: Optional[Image.Image] = None
    if user.get("media_fid"):
        try:
            bg_img = Image.open(io.BytesIO(await _download(bot, user["media_fid"])))
        except Exception as e:
            log.warning("bg_img load: %s", e)

    target_hue, target_sat = rgb_to_hsv_norm(*hex_to_rgb(ec))

    # ── Декодирование кадров ─────────────────────────────────────────────────
    if ext == ".tgs":
        limit = 60 if preview else 150
        raw_frames: FrameList = await asyncio.to_thread(tgs_frames, raw, limit)
        if not raw_frames:
            raise ValueError(
                "TGS рендеринг недоступен — проверьте что rlottie-python установлен в образе."
            )
    else:
        def _decode() -> FrameList:
            img = Image.open(io.BytesIO(raw))
            try:
                img.seek(1)
                img.seek(0)
                return extract_pil_frames(img)
            except EOFError:
                return [(np.array(img.convert("RGBA")), 0.05)]
        raw_frames = await asyncio.to_thread(_decode)

    if preview and len(raw_frames) > 1:
        raw_frames = raw_frames[:1]

    # ── Перекраска + компоновка ──────────────────────────────────────────────
    def _process() -> tuple[list[np.ndarray], list[float]]:
        out_f, out_d = [], []
        for frm, dur in raw_frames:
            colored = recolor(frm, target_hue, target_sat)
            banner  = compose_frame(colored, bg, cw, ch, note, wm, bg_img)
            out_f.append(banner)
            out_d.append(dur)
        return out_f, out_d

    frames, durs = await asyncio.to_thread(_process)
    animated = len(frames) > 1

    # ── Экспорт ──────────────────────────────────────────────────────────────
    if fmt == "PNG" or not animated:
        buf = io.BytesIO()
        Image.fromarray(frames[0]).convert("RGBA").save(buf, format="PNG")
        buf.seek(0)
        return buf.read(), "png"

    if fmt == "GIF":
        data = await asyncio.to_thread(frames_to_gif, frames, durs)
        return data, "gif"

    if fmt == "MP4":
        data = await asyncio.to_thread(frames_to_mp4, frames, fps, crf)
        return data, "mp4"

    buf = io.BytesIO()
    Image.fromarray(frames[0]).save(buf, format="PNG")
    buf.seek(0)
    return buf.read(), "png"

# ══════════════════════════════════════════════════════════════════════════════
# ДЕКОРАТИВНЫЙ БАННЕР (для /start без стикера)
# ══════════════════════════════════════════════════════════════════════════════

def welcome_banner(user: dict) -> bytes:
    cw, ch = _parse_res(user.get("resolution", DEFAULT_RES))
    pw, ph = _preview_size(cw, ch)
    bg = hex_to_rgb(user.get("bg_color",    DEFAULT_BG))
    ec = hex_to_rgb(user.get("emoji_color", DEFAULT_EC))

    img  = Image.new("RGB", (pw, ph), bg)
    draw = ImageDraw.Draw(img)

    # "Луна" — декоративная сфера из настроек
    cx, cy = pw // 2, ph // 2
    r = min(pw, ph) // 3

    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=ec)

    shine = tuple(min(255, v + 70) for v in ec)
    r2 = r // 3
    draw.ellipse(
        [cx - r + r2 // 2, cy - r + r2 // 3,
         cx - r + r2 * 2,  cy - r + r2 * 4 // 3],
        fill=shine,
    )
    shadow = tuple(max(0, v - 40) for v in ec)
    for ox, oy, cr in [
        (int(r * 0.30), int(-r * 0.10), r // 8),
        (int(-r * 0.20), int(r * 0.30), r // 12),
        (int(r * 0.10),  int(r * 0.35), r // 14),
    ]:
        draw.ellipse(
            [cx + ox - cr, cy + oy - cr, cx + ox + cr, cy + oy + cr],
            fill=shadow,
        )

    wm = user.get("watermark") or ""
    if wm:
        fs  = max(11, ph // 30)
        fnt = get_font(fs)
        bb  = draw.textbbox((0, 0), wm, font=fnt)
        tw  = bb[2] - bb[0]
        mg  = max(6, ph // 30)
        draw.text(
            (pw - tw - mg, ph - mg - fs), wm,
            font=fnt,
            fill=tuple(min(255, v + 100) for v in bg),
        )

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()

# ══════════════════════════════════════════════════════════════════════════════
# КЛАВИАТУРЫ  /  ТЕКСТ
# ══════════════════════════════════════════════════════════════════════════════

def kb_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎨 Цвет фона"),  KeyboardButton(text="📐 Разрешение")],
            [KeyboardButton(text="🧾 Формат"),      KeyboardButton(text="🖼 Своя медиа")],
            [KeyboardButton(text="✨ ЦветEmoji"),   KeyboardButton(text="📝 Заметки")],
            [KeyboardButton(text="💧 Вотермарка"),  KeyboardButton(text="👁 Предосмотр")],
        ],
        resize_keyboard=True,
    )


def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
    ])


def kb_wallet() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Кошелёк · 0₽", callback_data="wallet")]
    ])


def cfg_text(u: dict) -> str:
    return (
        "⚙️ <b>Конфигурация:</b>\n"
        f"┣ 🎨 Цвет фона: <code>{u.get('bg_color',    DEFAULT_BG)}</code>\n"
        f"┣ 📐 Разрешение: {u.get('resolution', DEFAULT_RES)} "
        f"{u.get('fps', DEFAULT_FPS)} FPS\n"
        f"┣ 🧾 Формат: {u.get('format', DEFAULT_FMT)}\n"
        f"┣ ✨ ЦветEmoji: <code>{u.get('emoji_color', DEFAULT_EC)}</code>\n"
        f"┣ 📝 Заметки: {u.get('notes', '')     or '—'}\n"
        f"┣ 💧 Вотермарка: {u.get('watermark', '') or '—'}\n"
        f"┗ 🖼 Своя медиа: {'✅' if u.get('media_fid') else '—'}"
    )

# ══════════════════════════════════════════════════════════════════════════════
# РОУТЕР
# ══════════════════════════════════════════════════════════════════════════════

router = Router()

# ── /start ────────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext) -> None:
    u = db_user(msg.from_user.id)
    await state.set_state(S.idle)
    img = await asyncio.to_thread(welcome_banner, u)
    await msg.answer_photo(
        BufferedInputFile(img, "banner.png"),
        caption=(
            "🌐 <b>Создан для оформления ботов и сайтов</b>\n\n"
            "📤 <b>Отправь мне:</b>\n"
            "┣ прем эмодзи — можно несколько\n"
            "┗ стикер, ссылку на пак emoji or sticker\n\n"
            + cfg_text(u)
        ),
        reply_markup=kb_wallet(),
    )
    await msg.answer("⬇️ Настройки:", reply_markup=kb_main())


@router.message(Command("help"))
async def cmd_help(msg: Message) -> None:
    await msg.answer(
        "ℹ️ <b>Инструкция:</b>\n\n"
        "1. Отправь стикер / premium emoji / изображение\n"
        "2. Бот сгенерирует баннер с текущими настройками\n"
        "3. Кнопка <b>👁 Предосмотр</b> — уменьшенный предпросмотр\n\n"
        "<b>Поддерживаемые входные форматы:</b>\n"
        "WEBP, TGS (анимированный стикер), GIF, PNG, JPG\n\n"
        "<b>Форматы выхода:</b> GIF · MP4 · PNG\n\n"
        "<b>Ссылки на паки:</b>\n"
        "<code>https://t.me/addstickers/PackName</code>\n"
        "<code>https://t.me/addemoji/EmojiPackName</code>",
    )


@router.callback_query(F.data == "wallet")
async def cb_wallet(call: CallbackQuery) -> None:
    await call.answer("💳 Баланс: 0₽\nПополнение временно недоступно.", show_alert=True)


@router.callback_query(F.data == "cancel")
async def cb_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(S.idle)
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.answer("Отменено")

# ── Приём файлов ──────────────────────────────────────────────────────────────

async def _accept(
    msg: Message, state: FSMContext, bot: Bot,
    fid: str, fname: str, ftype: str,
) -> None:
    """Принимает файл и сразу генерирует баннер с текущими настройками."""
    u = db_user(msg.from_user.id)
    await state.update_data(file_id=fid, file_name=fname, file_type=ftype)
    await state.set_state(S.idle)

    status = await msg.answer("⏳ Генерирую баннер...")
    try:
        result, out_ext = await build_banner(bot, fid, fname, u)
        out_name = f"banner.{out_ext}"
        cap = f"✅ Готово\n{cfg_text(u)}"

        if out_ext == "gif":
            await msg.answer_animation(BufferedInputFile(result, out_name), caption=cap)
        elif out_ext == "mp4":
            await msg.answer_video(BufferedInputFile(result, out_name), caption=cap)
        else:
            await msg.answer_photo(BufferedInputFile(result, out_name), caption=cap)

        db_log(msg.from_user.id, ftype)
    except Exception as e:
        log.error("build_banner: %s", e, exc_info=True)
        await msg.answer(
            f"❌ Ошибка: {html.escape(str(e))}",
            reply_markup=kb_main(),
        )
    finally:
        try:
            await status.delete()
        except Exception:
            pass


@router.message(F.sticker)
async def on_sticker(msg: Message, state: FSMContext, bot: Bot) -> None:
    s = msg.sticker
    name  = f"sticker.{'tgs' if s.is_animated else 'webp'}"
    ftype = "TGS" if s.is_animated else "WEBP"
    await _accept(msg, state, bot, s.file_id, name, ftype)


@router.message(F.animation)
async def on_anim(msg: Message, state: FSMContext, bot: Bot) -> None:
    a = msg.animation
    await _accept(msg, state, bot, a.file_id, a.file_name or "animation.gif", "GIF")


@router.message(S.set_media, F.photo)
async def on_media_photo(msg: Message, state: FSMContext) -> None:
    """Принимает фото как фоновое изображение в режиме set_media."""
    db_set(msg.from_user.id, media_fid=msg.photo[-1].file_id)
    await state.set_state(S.idle)
    await msg.answer("✅ Фоновое изображение сохранено!", reply_markup=kb_main())


@router.message(F.photo)
async def on_photo(msg: Message, state: FSMContext, bot: Bot) -> None:
    await _accept(msg, state, bot, msg.photo[-1].file_id, "photo.jpg", "JPG")


@router.message(F.document)
async def on_doc(msg: Message, state: FSMContext, bot: Bot) -> None:
    doc   = msg.document
    fname = doc.file_name or "file"
    ext   = Path(fname).suffix.lower()

    # В режиме set_media принимаем изображения как фон
    if (await state.get_state()) == S.set_media.state and \
       doc.mime_type and doc.mime_type.startswith("image/"):
        db_set(msg.from_user.id, media_fid=doc.file_id)
        await state.set_state(S.idle)
        await msg.answer("✅ Фоновое изображение сохранено!", reply_markup=kb_main())
        return

    allowed = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".tgs"}
    if ext not in allowed:
        await msg.answer(
            f"⚠️ Формат <b>{ext}</b> не поддерживается.\n"
            "Поддерживаемые: PNG, JPG, WEBP, GIF, TGS"
        )
        return
    await _accept(msg, state, bot, doc.file_id, fname, ext.upper().lstrip("."))

# ── Весь текстовый ввод ───────────────────────────────────────────────────────

# Словарь: нажатие кнопки → (флаг-состояние, сопр. функция)
_BTN_HANDLERS: dict[str, str] = {
    "🎨 Цвет фона":  "bg",
    "📐 Разрешение": "res",
    "✨ ЦветEmoji":  "ec",
    "📝 Заметки":    "notes",
    "💧 Вотермарка": "wm",
    "🖼 Своя медиа": "media",
    "🧾 Формат":     "fmt",
    "👁 Предосмотр": "preview",
}


@router.message(F.text)
async def on_text(msg: Message, state: FSMContext, bot: Bot) -> None:
    text = msg.text or ""
    cur  = await state.get_state()

    # ── FSM-ввод (текстовые настройки) ───────────────────────────────────────
    if cur == S.set_bg.state:
        await _input_bg(msg, state); return
    if cur == S.set_res.state:
        await _input_res(msg, state); return
    if cur == S.set_ec.state:
        await _input_ec(msg, state); return
    if cur == S.set_notes.state:
        await _input_notes(msg, state); return
    if cur == S.set_wm.state:
        await _input_wm(msg, state); return

    # ── Нижняя клавиатура ────────────────────────────────────────────────────
    if text == "🎨 Цвет фона":   await _show_bg(msg, state);      return
    if text == "📐 Разрешение":  await _show_res(msg, state);     return
    if text == "🧾 Формат":      await _show_fmt(msg);            return
    if text == "✨ ЦветEmoji":   await _show_ec(msg, state);      return
    if text == "📝 Заметки":     await _show_notes(msg, state);   return
    if text == "💧 Вотермарка":  await _show_wm(msg, state);      return
    if text == "🖼 Своя медиа":  await _show_media(msg, state);   return
    if text == "👁 Предосмотр":  await _show_preview(msg, state, bot); return

    # ── Ссылка на пак ─────────────────────────────────────────────────────────
    m = re.search(r"t\.me/(?:addstickers|addemoji)/(\w+)", text)
    if m:
        await _pack_link(msg, state, bot, m.group(1)); return

    # ── Premium emoji ─────────────────────────────────────────────────────────
    if msg.entities:
        ids = [e.custom_emoji_id for e in msg.entities
               if e.type == "custom_emoji" and e.custom_emoji_id]
        if ids:
            try:
                stickers = await bot.get_custom_emoji_stickers(ids[:1])
                if stickers:
                    s = stickers[0]
                    name  = f"emoji.{'tgs' if s.is_animated else 'webp'}"
                    ftype = "TGS emoji" if s.is_animated else "WEBP emoji"
                    await _accept(msg, state, bot, s.file_id, name, ftype)
                    return
            except Exception as e:
                log.error("custom emoji: %s", e)
                await msg.answer(f"❌ Не удалось получить emoji: {e}")
                return

    # ── Прочее ────────────────────────────────────────────────────────────────
    await msg.answer(
        "📎 Отправь стикер, emoji или изображение.\n"
        "Используй кнопки ниже для настройки.",
        reply_markup=kb_main(),
    )


async def _pack_link(msg: Message, state: FSMContext, bot: Bot, name: str) -> None:
    try:
        ss = await bot.get_sticker_set(name)
        if not ss.stickers:
            await msg.answer("⚠️ Пак пустой"); return
        s     = ss.stickers[0]
        fname = f"pack.{'tgs' if s.is_animated else 'webp'}"
        ftype = "TGS из пака" if s.is_animated else "WEBP из пака"
        await msg.answer(f"✅ Загружен первый стикер из пака <b>{ss.title}</b>")
        await _accept(msg, state, bot, s.file_id, fname, ftype)
    except Exception as e:
        log.error("pack link: %s", e)
        await msg.answer(f"❌ Не удалось загрузить пак: {e}")

# ══════════════════════════════════════════════════════════════════════════════
# ПОКАЗ НАСТРОЕК (открывают инлайн-меню)
# ══════════════════════════════════════════════════════════════════════════════

async def _show_bg(msg: Message, state: FSMContext) -> None:
    await state.set_state(S.set_bg)
    u = db_user(msg.from_user.id)
    await msg.answer(
        f"🎨 <b>Цвет фона</b>\nТекущий: <code>{u['bg_color']}</code>\n\n"
        "Введи HEX (например <code>#FF5E3B</code>) или выбери:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🟠 #FF5E3B", callback_data="bg_FF5E3B"),
                InlineKeyboardButton(text="⚫ #0D0D0D", callback_data="bg_0D0D0D"),
                InlineKeyboardButton(text="🔵 #1A1A2E", callback_data="bg_1A1A2E"),
            ],
            [
                InlineKeyboardButton(text="🟣 #6C3483", callback_data="bg_6C3483"),
                InlineKeyboardButton(text="🟢 #1E8449", callback_data="bg_1E8449"),
                InlineKeyboardButton(text="🩵 #2980B9", callback_data="bg_2980B9"),
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
        ]),
    )


@router.callback_query(F.data.startswith("bg_"))
async def cb_bg(call: CallbackQuery, state: FSMContext) -> None:
    color = "#" + call.data[3:]
    db_set(call.from_user.id, bg_color=color)
    await state.set_state(S.idle)
    await call.message.edit_text(f"✅ Цвет фона: <code>{color}</code>")
    await call.answer()


async def _input_bg(msg: Message, state: FSMContext) -> None:
    raw = msg.text.strip()
    if not valid_hex(raw):
        await msg.answer("❌ Неверный HEX. Пример: <code>#FF5E3B</code>"); return
    color = norm_hex(raw)
    db_set(msg.from_user.id, bg_color=color)
    await state.set_state(S.idle)
    await msg.answer(f"✅ Цвет фона: <code>{color}</code>", reply_markup=kb_main())


# ── ЦветEmoji ─────────────────────────────────────────────────────────────────

async def _show_ec(msg: Message, state: FSMContext) -> None:
    await state.set_state(S.set_ec)
    u = db_user(msg.from_user.id)
    await msg.answer(
        f"✨ <b>ЦветEmoji</b>\nТекущий: <code>{u['emoji_color']}</code>\n\n"
        "Введи HEX или выбери:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="⚪ Белый",   callback_data="ec_FFFFFF"),
                InlineKeyboardButton(text="🟡 Жёлтый",  callback_data="ec_FFD700"),
                InlineKeyboardButton(text="🔴 Красный", callback_data="ec_FF2200"),
            ],
            [
                InlineKeyboardButton(text="🔵 Синий",   callback_data="ec_0088FF"),
                InlineKeyboardButton(text="🟢 Зелёный", callback_data="ec_00CC44"),
                InlineKeyboardButton(text="🟣 Фиолет.", callback_data="ec_BB44FF"),
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
        ]),
    )


@router.callback_query(F.data.startswith("ec_"))
async def cb_ec(call: CallbackQuery, state: FSMContext) -> None:
    color = "#" + call.data[3:]
    db_set(call.from_user.id, emoji_color=color)
    await state.set_state(S.idle)
    await call.message.edit_text(f"✅ ЦветEmoji: <code>{color}</code>")
    await call.answer()


async def _input_ec(msg: Message, state: FSMContext) -> None:
    raw = msg.text.strip()
    if not valid_hex(raw):
        await msg.answer("❌ Неверный HEX. Пример: <code>#FFFFFF</code>"); return
    color = norm_hex(raw)
    db_set(msg.from_user.id, emoji_color=color)
    await state.set_state(S.idle)
    await msg.answer(f"✅ ЦветEmoji: <code>{color}</code>", reply_markup=kb_main())


# ── Разрешение ────────────────────────────────────────────────────────────────

async def _show_res(msg: Message, state: FSMContext) -> None:
    await state.set_state(S.set_res)
    u   = db_user(msg.from_user.id)
    fps = u.get("fps", DEFAULT_FPS)
    res = u.get("resolution", DEFAULT_RES)
    await msg.answer(
        f"📐 <b>Разрешение</b>\nТекущее: {res} {fps} FPS\n\n"
        "Формат: <code>ШИРИНАxВЫСОТА FPS</code> — например <code>1920x530 60</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="📺 1920×530 30fps",  callback_data="res_1920x530_30"),
                InlineKeyboardButton(text="⚡ 1920×530 60fps",  callback_data="res_1920x530_60"),
            ],
            [
                InlineKeyboardButton(text="🖥 1280×720 30fps",  callback_data="res_1280x720_30"),
                InlineKeyboardButton(text="⚡ 1280×720 60fps",  callback_data="res_1280x720_60"),
            ],
            [
                InlineKeyboardButton(text="📱 512×512",         callback_data="res_512x512_30"),
                InlineKeyboardButton(text="🎬 1920×1080 24fps", callback_data="res_1920x1080_24"),
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
        ]),
    )


@router.callback_query(F.data.startswith("res_"))
async def cb_res(call: CallbackQuery, state: FSMContext) -> None:
    _, res, fps_s = call.data.split("_", 2)
    db_set(call.from_user.id, resolution=res, fps=int(fps_s))
    await state.set_state(S.idle)
    await call.message.edit_text(f"✅ Разрешение: {res} {fps_s} FPS")
    await call.answer()


async def _input_res(msg: Message, state: FSMContext) -> None:
    m = re.match(r"(\d{2,4})[x×](\d{2,4})(?:\s+(\d{1,3}))?", msg.text.strip().lower())
    if not m:
        await msg.answer("❌ Формат: <code>1920x530 60</code>"); return
    w, h, fps = int(m.group(1)), int(m.group(2)), int(m.group(3) or 30)
    if not (64 <= w <= 3840 and 64 <= h <= 2160 and 1 <= fps <= 60):
        await msg.answer("❌ Ширина/высота 64–3840, FPS 1–60"); return
    db_set(msg.from_user.id, resolution=f"{w}x{h}", fps=fps)
    await state.set_state(S.idle)
    await msg.answer(f"✅ Разрешение: {w}x{h} {fps} FPS", reply_markup=kb_main())


# ── Формат ────────────────────────────────────────────────────────────────────

async def _show_fmt(msg: Message) -> None:
    u = db_user(msg.from_user.id)
    await msg.answer(
        f"🧾 <b>Формат экспорта</b>\nТекущий: <b>{u['format']}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🎞 GIF", callback_data="fmt_GIF"),
                InlineKeyboardButton(text="🎬 MP4", callback_data="fmt_MP4"),
                InlineKeyboardButton(text="🖼 PNG", callback_data="fmt_PNG"),
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
        ]),
    )


@router.callback_query(F.data.startswith("fmt_"))
async def cb_fmt(call: CallbackQuery) -> None:
    fmt = call.data[4:]
    db_set(call.from_user.id, format=fmt)
    await call.message.edit_text(f"✅ Формат: <b>{fmt}</b>")
    await call.answer(f"Формат: {fmt}")


# ── Заметки ───────────────────────────────────────────────────────────────────

async def _show_notes(msg: Message, state: FSMContext) -> None:
    await state.set_state(S.set_notes)
    u = db_user(msg.from_user.id)
    await msg.answer(
        f"📝 <b>Заметки</b> — текст в нижнем левом углу\n"
        f"Текущее: {u.get('notes') or '—'}\n\n"
        "Введи текст (до 120 символов) или <code>-</code> для удаления:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Удалить заметку", callback_data="notes_del")],
            [InlineKeyboardButton(text="❌ Отмена",           callback_data="cancel")],
        ]),
    )


@router.callback_query(F.data == "notes_del")
async def cb_notes_del(call: CallbackQuery, state: FSMContext) -> None:
    db_set(call.from_user.id, notes="")
    await state.set_state(S.idle)
    await call.message.edit_text("✅ Заметка удалена")
    await call.answer()


async def _input_notes(msg: Message, state: FSMContext) -> None:
    text = msg.text.strip()
    if text == "-":
        text = ""
    if len(text) > 120:
        await msg.answer("❌ Максимум 120 символов"); return
    db_set(msg.from_user.id, notes=text)
    await state.set_state(S.idle)
    await msg.answer(f"✅ Заметка: {text or '(удалена)'}", reply_markup=kb_main())


# ── Вотермарка ────────────────────────────────────────────────────────────────

async def _show_wm(msg: Message, state: FSMContext) -> None:
    await state.set_state(S.set_wm)
    u = db_user(msg.from_user.id)
    await msg.answer(
        f"💧 <b>Вотермарка</b> — текст в нижнем правом углу\n"
        f"Текущая: {u.get('watermark') or '—'}\n\n"
        "Введи текст (до 60 символов) или <code>-</code> для удаления:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Удалить вотермарку", callback_data="wm_del")],
            [InlineKeyboardButton(text="❌ Отмена",              callback_data="cancel")],
        ]),
    )


@router.callback_query(F.data == "wm_del")
async def cb_wm_del(call: CallbackQuery, state: FSMContext) -> None:
    db_set(call.from_user.id, watermark="")
    await state.set_state(S.idle)
    await call.message.edit_text("✅ Вотермарка удалена")
    await call.answer()


async def _input_wm(msg: Message, state: FSMContext) -> None:
    text = msg.text.strip()
    if text == "-":
        text = ""
    if len(text) > 60:
        await msg.answer("❌ Максимум 60 символов"); return
    db_set(msg.from_user.id, watermark=text)
    await state.set_state(S.idle)
    await msg.answer(f"✅ Вотермарка: {text or '(удалена)'}", reply_markup=kb_main())


# ── Своя медиа ────────────────────────────────────────────────────────────────

async def _show_media(msg: Message, state: FSMContext) -> None:
    await state.set_state(S.set_media)
    u = db_user(msg.from_user.id)
    await msg.answer(
        f"🖼 <b>Своя медиа</b> — фоновое изображение\n"
        f"Статус: {'✅ установлен' if u.get('media_fid') else 'не установлен'}\n\n"
        "Отправь фото для использования в качестве фона:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Удалить фон", callback_data="media_del")],
            [InlineKeyboardButton(text="❌ Отмена",       callback_data="cancel")],
        ]),
    )


@router.callback_query(F.data == "media_del")
async def cb_media_del(call: CallbackQuery, state: FSMContext) -> None:
    db_set(call.from_user.id, media_fid=None)
    await state.set_state(S.idle)
    await call.message.edit_text("✅ Фоновое медиа удалено")
    await call.answer()


# ── Предосмотр ────────────────────────────────────────────────────────────────

async def _show_preview(msg: Message, state: FSMContext, bot: Bot) -> None:
    data  = await state.get_data()
    fid   = data.get("file_id")
    u     = db_user(msg.from_user.id)

    if not fid:
        img = await asyncio.to_thread(welcome_banner, u)
        await msg.answer_photo(
            BufferedInputFile(img, "preview.png"),
            caption="👁 Предосмотр настроек\n\n" + cfg_text(u),
        )
        return

    status = await msg.answer("⏳ Генерирую предосмотр...")
    try:
        result, _ = await build_banner(bot, fid, data.get("file_name", "sticker.webp"), u, preview=True)
        await msg.answer_photo(
            BufferedInputFile(result, "preview.png"),
            caption=(
                f"👁 <b>Предосмотр</b>\n"
                f"🎨 Фон <code>{u['bg_color']}</code>  "
                f"✨ Emoji <code>{u['emoji_color']}</code>\n"
                f"📐 {u.get('resolution', DEFAULT_RES)} "
                f"{u.get('fps', DEFAULT_FPS)} FPS  "
                f"🧾 {u.get('format', DEFAULT_FMT)}"
            ),
        )
    except Exception as e:
        log.error("preview: %s", e, exc_info=True)
        await msg.answer(f"❌ Ошибка предосмотра: {html.escape(str(e))}")
    finally:
        try:
            await status.delete()
        except Exception:
            pass

# ══════════════════════════════════════════════════════════════════════════════
# ЗАПУСК
# ══════════════════════════════════════════════════════════════════════════════

async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан в .env!")
    db_init()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp  = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    log.info("Bot v2.0 starting...")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        log.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
