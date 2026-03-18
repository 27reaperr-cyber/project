from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from config import AVAILABLE_FONTS, AVAILABLE_FORMATS, PRESET_RESOLUTIONS, PRESET_FPS


# ── helpers ────────────────────────────────────────────────────────────────────

def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _check(current, value) -> str:
    return "✅ " if current == value else ""


# ── main menu ──────────────────────────────────────────────────────────────────

def kb_main(s: dict) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()

    bg   = s.get("bg_color", "#FFFFFF")
    sc   = s.get("sticker_color") or "—"
    wt   = s.get("watermark_text") or "—"
    wf   = s.get("watermark_font", "Montserrat")
    res  = s.get("resolution", "512x512").replace("x", "×")
    fps  = s.get("fps", 30)
    fmt  = s.get("format", "GIF")

    b.row(_btn(f"🎨  Фон: {bg}",                    "cfg_bg"))
    b.row(_btn(f"🖌  Цвет стикера: {sc}",            "cfg_sc"))
    b.row(_btn(f"💬  Вотермарка: {wt}  [{wf}]",     "cfg_wm"))
    b.row(_btn(f"📐  Разрешение: {res}  {fps} FPS", "cfg_res"))
    b.row(_btn(f"📁  Формат: {fmt}",                 "cfg_fmt"))
    b.row(_btn("🚀  Конвертировать",                 "convert"))
    return b.as_markup()


# ── back button ────────────────────────────────────────────────────────────────

def kb_back() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(_btn("‹ Назад", "back"))
    return b.as_markup()


# ── font picker ────────────────────────────────────────────────────────────────

def kb_fonts(current: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for f in AVAILABLE_FONTS:
        b.row(_btn(f"{_check(current, f)}{f}", f"font_{f}"))
    b.row(_btn("‹ Назад", "back"))
    return b.as_markup()


# ── resolution picker ─────────────────────────────────────────────────────────

def kb_resolution(current_res: str, current_fps: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for label, val in PRESET_RESOLUTIONS:
        mark = "✅ " if current_res == val else ""
        b.row(_btn(f"{mark}{label}", f"res_{val}"))
    b.row(_btn("✏️  Своё разрешение (WxH)", "res_custom"))
    b.row(*[_btn(f"{_check(current_fps, f)}{f} FPS", f"fps_{f}") for f in PRESET_FPS])
    b.row(_btn("‹ Назад", "back"))
    return b.as_markup()


# ── format picker ─────────────────────────────────────────────────────────────

def kb_format(current: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(*[_btn(f"{_check(current, f)}{f}", f"fmt_{f}") for f in AVAILABLE_FORMATS])
    b.row(_btn("‹ Назад", "back"))
    return b.as_markup()


# ── watermark sub-menu ────────────────────────────────────────────────────────

def kb_wm(s: dict) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    wt = s.get("watermark_text") or "—"
    wf = s.get("watermark_font", "Montserrat")
    b.row(_btn(f"✏️  Текст: {wt}",       "wm_text"))
    b.row(_btn(f"🔤  Шрифт: {wf}",       "wm_font"))
    if s.get("watermark_text"):
        b.row(_btn("🗑  Убрать вотермарку", "wm_clear"))
    b.row(_btn("‹ Назад", "back"))
    return b.as_markup()
