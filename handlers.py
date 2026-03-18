"""
handlers.py
───────────
All message and callback handlers.
Self-editing inline menu:  only ONE message from the bot is ever
mutated — the "menu" message whose ID is stored in FSM state data.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import logging
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    Message,
)

from config import DEFAULT_SETTINGS, TEMP_DIR
from keyboards import kb_back, kb_fonts, kb_format, kb_main, kb_resolution, kb_wm
from states import SS

router = Router()
log    = logging.getLogger(__name__)

os.makedirs(TEMP_DIR, exist_ok=True)


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

MENU_HEADER = (
    "🎛  <b>Настройки экспорта</b>\n\n"
    "Нажми нужную кнопку, чтобы изменить параметр.\n"
    "Когда всё готово — жми <b>🚀 Конвертировать</b>."
)

WM_HEADER = (
    "💬 <b>Вотермарка</b>\n\n"
    "Настрой текст и шрифт водяного знака."
)


async def _edit_menu(
    bot: Bot,
    chat_id: int,
    msg_id: int,
    text: str,
    markup,
) -> None:
    """Edit menu message safely (ignore 'message is not modified')."""
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=text,
            reply_markup=markup,
            parse_mode="HTML",
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


async def _restore_main_menu(bot: Bot, state: FSMContext) -> None:
    """Go back to the main menu screen."""
    data    = await state.get_data()
    chat_id = data["chat_id"]
    msg_id  = data["menu_msg_id"]
    s       = data.get("settings", DEFAULT_SETTINGS.copy())

    await _edit_menu(bot, chat_id, msg_id, MENU_HEADER, kb_main(s))
    await state.set_state(SS.MENU)


def _valid_hex(color: str) -> bool:
    return bool(re.fullmatch(r"#[0-9A-Fa-f]{6}", color.strip()))


def _parse_resolution(text: str) -> Optional[tuple[int, int]]:
    """Parse 'WxH', 'W×H', 'W X H' into (w, h) or None."""
    m = re.search(r"(\d{2,4})\s*[xX×]\s*(\d{2,4})", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Sticker & custom-emoji entry points
# ══════════════════════════════════════════════════════════════════════════════

async def _start_session(
    message: Message,
    state: FSMContext,
    bot: Bot,
    file_id: str,
    file_type: str,          # "tgs" or "emoji"
) -> None:
    """Download sticker to temp dir, show main menu."""
    # Download
    tgs_dir  = os.path.join(TEMP_DIR, str(message.from_user.id))
    os.makedirs(tgs_dir, exist_ok=True)
    tgs_path = os.path.join(tgs_dir, f"{file_id[:20]}.tgs")

    file = await bot.get_file(file_id)
    await bot.download_file(file.file_path, destination=tgs_path)

    settings = DEFAULT_SETTINGS.copy()

    menu_msg = await message.answer(
        MENU_HEADER,
        reply_markup=kb_main(settings),
        parse_mode="HTML",
    )

    await state.set_state(SS.MENU)
    await state.update_data(
        chat_id      = message.chat.id,
        menu_msg_id  = menu_msg.message_id,
        tgs_path     = tgs_path,
        file_type    = file_type,
        settings     = settings,
    )


@router.message(F.sticker)
async def on_sticker(message: Message, state: FSMContext, bot: Bot):
    st = message.sticker
    if not st.is_animated:
        await message.reply("⚠️ Нужен анимированный TGS стикер (не статичный и не видео).")
        return
    await _start_session(message, state, bot, st.file_id, "tgs")


@router.message(F.text.regexp(r"^\d{10,20}$"))
async def on_emoji_id(message: Message, state: FSMContext, bot: Bot):
    """User sends a numeric custom emoji ID."""
    emoji_id = message.text.strip()
    try:
        stickers = await bot.get_custom_emoji_stickers([emoji_id])
        if not stickers:
            raise ValueError("empty")
        st = stickers[0]
        await _start_session(message, state, bot, st.file_id, "emoji")
    except Exception:
        await message.reply(
            "⚠️ Не удалось найти эмодзи с таким ID.\n"
            "Убедись, что это корректный ID premium-эмодзи (16–20 цифр)."
        )


# ══════════════════════════════════════════════════════════════════════════════
# Generic back button
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "back")
async def cb_back(cq: CallbackQuery, state: FSMContext, bot: Bot):
    await cq.answer()
    await _restore_main_menu(bot, state)


# ══════════════════════════════════════════════════════════════════════════════
# Background colour
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(SS.MENU, F.data == "cfg_bg")
async def cb_cfg_bg(cq: CallbackQuery, state: FSMContext, bot: Bot):
    await cq.answer()
    data = await state.get_data()
    await _edit_menu(
        bot, data["chat_id"], data["menu_msg_id"],
        "🎨 <b>Цвет фона</b>\n\n"
        "Введи HEX-код, например: <code>#FFFFFF</code>",
        kb_back(),
    )
    await state.set_state(SS.WAIT_BG_COLOR)


@router.message(SS.WAIT_BG_COLOR)
async def on_bg_color(message: Message, state: FSMContext, bot: Bot):
    val = message.text.strip()
    await message.delete()
    if not _valid_hex(val):
        data = await state.get_data()
        await _edit_menu(
            bot, data["chat_id"], data["menu_msg_id"],
            "⚠️ Неверный формат. Введи HEX вида <code>#RRGGBB</code>:",
            kb_back(),
        )
        return
    data = await state.get_data()
    s    = data.get("settings", DEFAULT_SETTINGS.copy())
    s["bg_color"] = val.upper()
    await state.update_data(settings=s)
    await _restore_main_menu(bot, state)


# ══════════════════════════════════════════════════════════════════════════════
# Sticker colour tint
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(SS.MENU, F.data == "cfg_sc")
async def cb_cfg_sc(cq: CallbackQuery, state: FSMContext, bot: Bot):
    await cq.answer()
    data = await state.get_data()
    await _edit_menu(
        bot, data["chat_id"], data["menu_msg_id"],
        "🖌 <b>Цвет стикера / эмодзи</b>\n\n"
        "Введи HEX-код для тонирования (оттенок сдвигается, тени и "
        "блики сохраняются).\n\n"
        "Отправь <code>—</code> чтобы убрать тинт.",
        kb_back(),
    )
    await state.set_state(SS.WAIT_STICK_COLOR)


@router.message(SS.WAIT_STICK_COLOR)
async def on_stick_color(message: Message, state: FSMContext, bot: Bot):
    val = message.text.strip()
    await message.delete()
    data = await state.get_data()
    s    = data.get("settings", DEFAULT_SETTINGS.copy())

    if val in ("—", "-", "нет", "no", "none"):
        s["sticker_color"] = None
    elif not _valid_hex(val):
        await _edit_menu(
            bot, data["chat_id"], data["menu_msg_id"],
            "⚠️ Неверный формат. Введи HEX вида <code>#RRGGBB</code> "
            "или <code>—</code> чтобы убрать:",
            kb_back(),
        )
        return
    else:
        s["sticker_color"] = val.upper()

    await state.update_data(settings=s)
    await _restore_main_menu(bot, state)


# ══════════════════════════════════════════════════════════════════════════════
# Watermark sub-menu
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(SS.MENU, F.data == "cfg_wm")
async def cb_cfg_wm(cq: CallbackQuery, state: FSMContext, bot: Bot):
    await cq.answer()
    data = await state.get_data()
    s    = data.get("settings", DEFAULT_SETTINGS.copy())
    await _edit_menu(
        bot, data["chat_id"], data["menu_msg_id"],
        WM_HEADER, kb_wm(s),
    )
    await state.set_state(SS.MENU)   # stay in MENU so back works


@router.callback_query(SS.MENU, F.data == "wm_text")
async def cb_wm_text(cq: CallbackQuery, state: FSMContext, bot: Bot):
    await cq.answer()
    data = await state.get_data()
    await _edit_menu(
        bot, data["chat_id"], data["menu_msg_id"],
        "💬 <b>Текст вотермарки</b>\n\nВведи нужный текст:",
        kb_back(),
    )
    await state.set_state(SS.WAIT_WM_TEXT)


@router.message(SS.WAIT_WM_TEXT)
async def on_wm_text(message: Message, state: FSMContext, bot: Bot):
    val = message.text.strip()
    await message.delete()
    data = await state.get_data()
    s    = data.get("settings", DEFAULT_SETTINGS.copy())
    s["watermark_text"] = val if val else None
    await state.update_data(settings=s)
    await _restore_main_menu(bot, state)


@router.callback_query(SS.MENU, F.data == "wm_font")
async def cb_wm_font(cq: CallbackQuery, state: FSMContext, bot: Bot):
    await cq.answer()
    data = await state.get_data()
    s    = data.get("settings", DEFAULT_SETTINGS.copy())
    await _edit_menu(
        bot, data["chat_id"], data["menu_msg_id"],
        "🔤 <b>Шрифт вотермарки</b>\n\nВыбери шрифт:",
        kb_fonts(s.get("watermark_font", "Montserrat")),
    )
    await state.set_state(SS.CHOOSE_FONT)


@router.callback_query(SS.CHOOSE_FONT, F.data.startswith("font_"))
async def cb_font(cq: CallbackQuery, state: FSMContext, bot: Bot):
    await cq.answer()
    font = cq.data.replace("font_", "")
    data = await state.get_data()
    s    = data.get("settings", DEFAULT_SETTINGS.copy())
    s["watermark_font"] = font
    await state.update_data(settings=s)
    await _restore_main_menu(bot, state)


@router.callback_query(SS.MENU, F.data == "wm_clear")
async def cb_wm_clear(cq: CallbackQuery, state: FSMContext, bot: Bot):
    await cq.answer()
    data = await state.get_data()
    s    = data.get("settings", DEFAULT_SETTINGS.copy())
    s["watermark_text"] = None
    await state.update_data(settings=s)
    await _restore_main_menu(bot, state)


# ══════════════════════════════════════════════════════════════════════════════
# Resolution + FPS
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(SS.MENU, F.data == "cfg_res")
async def cb_cfg_res(cq: CallbackQuery, state: FSMContext, bot: Bot):
    await cq.answer()
    data = await state.get_data()
    s    = data.get("settings", DEFAULT_SETTINGS.copy())
    await _edit_menu(
        bot, data["chat_id"], data["menu_msg_id"],
        "📐 <b>Разрешение и FPS</b>\n\nВыбери пресет или задай своё:",
        kb_resolution(s.get("resolution", "512x512"), s.get("fps", 30)),
    )
    await state.set_state(SS.CHOOSE_RESOLUTION)


@router.callback_query(SS.CHOOSE_RESOLUTION, F.data.startswith("res_"))
async def cb_res_preset(cq: CallbackQuery, state: FSMContext, bot: Bot):
    val = cq.data.replace("res_", "")
    if val == "custom":
        await cq.answer()
        data = await state.get_data()
        await _edit_menu(
            bot, data["chat_id"], data["menu_msg_id"],
            "📐 Введи разрешение в формате <code>ШxВ</code>, например <code>1920x530</code>:",
            kb_back(),
        )
        # Reuse WAIT_BG_COLOR-style state — actually just set CHOOSE_RESOLUTION
        # and we'll intercept next text message:
        await state.set_state(SS.CHOOSE_RESOLUTION)
        await state.update_data(_await_custom_res=True)
        return

    await cq.answer()
    data = await state.get_data()
    s    = data.get("settings", DEFAULT_SETTINGS.copy())
    s["resolution"] = val
    await state.update_data(settings=s, _await_custom_res=False)
    await _restore_main_menu(bot, state)


@router.message(SS.CHOOSE_RESOLUTION)
async def on_custom_res(message: Message, state: FSMContext, bot: Bot):
    """Handle custom resolution text input."""
    val  = message.text.strip()
    data = await state.get_data()
    await message.delete()

    if not data.get("_await_custom_res"):
        return   # spurious message

    parsed = _parse_resolution(val)
    if not parsed:
        await _edit_menu(
            bot, data["chat_id"], data["menu_msg_id"],
            "⚠️ Не распознал. Введи в формате <code>ШxВ</code>, например <code>1920x530</code>:",
            kb_back(),
        )
        return

    w, h = parsed
    s    = data.get("settings", DEFAULT_SETTINGS.copy())
    s["resolution"] = f"{w}x{h}"
    await state.update_data(settings=s, _await_custom_res=False)
    await _restore_main_menu(bot, state)


@router.callback_query(SS.CHOOSE_RESOLUTION, F.data.startswith("fps_"))
async def cb_fps(cq: CallbackQuery, state: FSMContext, bot: Bot):
    await cq.answer()
    fps  = int(cq.data.replace("fps_", ""))
    data = await state.get_data()
    s    = data.get("settings", DEFAULT_SETTINGS.copy())
    s["fps"] = fps
    await state.update_data(settings=s)
    # Refresh resolution picker with updated checkmark
    await _edit_menu(
        bot, data["chat_id"], data["menu_msg_id"],
        "📐 <b>Разрешение и FPS</b>\n\nВыбери пресет или задай своё:",
        kb_resolution(s.get("resolution", "512x512"), fps),
    )


# ══════════════════════════════════════════════════════════════════════════════
# Format
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(SS.MENU, F.data == "cfg_fmt")
async def cb_cfg_fmt(cq: CallbackQuery, state: FSMContext, bot: Bot):
    await cq.answer()
    data = await state.get_data()
    s    = data.get("settings", DEFAULT_SETTINGS.copy())
    await _edit_menu(
        bot, data["chat_id"], data["menu_msg_id"],
        "📁 <b>Формат вывода</b>\n\n"
        "• GIF  — анимированный, работает везде\n"
        "• MOV  — H.264 видео (требуется ffmpeg)\n"
        "• PNG  — один кадр (середина анимации)",
        kb_format(s.get("format", "GIF")),
    )
    await state.set_state(SS.CHOOSE_FORMAT)


@router.callback_query(SS.CHOOSE_FORMAT, F.data.startswith("fmt_"))
async def cb_fmt(cq: CallbackQuery, state: FSMContext, bot: Bot):
    await cq.answer()
    fmt  = cq.data.replace("fmt_", "")
    data = await state.get_data()
    s    = data.get("settings", DEFAULT_SETTINGS.copy())
    s["format"] = fmt
    await state.update_data(settings=s)
    await _restore_main_menu(bot, state)


# ══════════════════════════════════════════════════════════════════════════════
# Convert  🚀
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(SS.MENU, F.data == "convert")
async def cb_convert(cq: CallbackQuery, state: FSMContext, bot: Bot):
    await cq.answer("⏳ Запускаю рендер…")

    data = await state.get_data()
    s    = data.get("settings", DEFAULT_SETTINGS.copy())

    # Lock UI
    await _edit_menu(
        bot, data["chat_id"], data["menu_msg_id"],
        "⏳ <b>Рендеринг…</b>\n\n"
        "Пожалуйста, подожди. Это может занять несколько секунд.",
        None,
    )
    await state.set_state(SS.PROCESSING)

    tgs_path = data["tgs_path"]
    fmt      = s.get("format", "GIF").upper()
    ext_map  = {"GIF": "gif", "MOV": "mov", "PNG": "png"}
    ext      = ext_map.get(fmt, "gif")

    out_path = os.path.join(TEMP_DIR, f"{data['chat_id']}_{data['menu_msg_id']}.{ext}")

    loop = asyncio.get_event_loop()

    try:
        from render_utils import process_tgs
        from concurrent.futures import ProcessPoolExecutor
        import functools

        # Run in a SEPARATE PROCESS so that rlottie segfaults / crashes
        # cannot kill the main bot process.
        with ProcessPoolExecutor(max_workers=1) as pool:
            await loop.run_in_executor(
                pool,
                functools.partial(process_tgs, tgs_path, out_path, s),
            )
    except Exception as e:
        log.exception("Render error")
        await _edit_menu(
            bot, data["chat_id"], data["menu_msg_id"],
            f"❌ <b>Ошибка рендера</b>\n\n<code>{e}</code>\n\n"
            "Проверь установку rlottie-python и/или ffmpeg.",
            kb_main(s),
        )
        await state.set_state(SS.MENU)
        return

    # Send result
    caption = (
        f"✅ <b>Готово!</b>\n"
        f"Формат: {fmt}  |  "
        f"{s.get('resolution','512x512').replace('x','×')}  |  "
        f"{s.get('fps',30)} FPS"
    )

    with open(out_path, "rb") as f:
        file_data = f.read()

    if fmt == "GIF":
        await bot.send_animation(
            data["chat_id"],
            animation=BufferedInputFile(file_data, filename="sticker.gif"),
            caption=caption,
            parse_mode="HTML",
        )
    elif fmt == "PNG":
        await bot.send_photo(
            data["chat_id"],
            photo=BufferedInputFile(file_data, filename="sticker.png"),
            caption=caption,
            parse_mode="HTML",
        )
    else:  # MOV
        await bot.send_video(
            data["chat_id"],
            video=BufferedInputFile(file_data, filename="sticker.mov"),
            caption=caption,
            parse_mode="HTML",
        )

    # Clean temp files
    for p in (tgs_path, out_path):
        try:
            os.remove(p)
        except OSError:
            pass

    # Restore menu for another round
    await _edit_menu(
        bot, data["chat_id"], data["menu_msg_id"],
        "✅ Готово! Отправь новый стикер или эмодзи.",
        None,
    )
    await state.clear()


# ══════════════════════════════════════════════════════════════════════════════
# /start
# ══════════════════════════════════════════════════════════════════════════════

@router.message(F.text.startswith("/start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 <b>StickerForge Bot</b>\n\n"
        "Отправь мне анимированный TGS стикер или ID premium-эмодзи "
        "(число из 10–20 цифр) — и я помогу настроить и экспортировать его "
        "в нужном формате.\n\n"
        "Поддерживаемые форматы вывода: <b>GIF / MOV / PNG</b>",
        parse_mode="HTML",
    )
