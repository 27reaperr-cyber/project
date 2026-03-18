import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
TEMP_DIR: str  = os.getenv("TEMP_DIR", "/tmp/stickerbot")

BASE_DIR   = Path(__file__).parent
FONTS_DIR  = BASE_DIR / "fonts"

DEFAULT_SETTINGS: dict = {
    "bg_color":        "#FFFFFF",
    "sticker_color":   None,        # None → no tint
    "watermark_text":  None,
    "watermark_font":  "Montserrat",
    "resolution":      "512x512",
    "fps":             30,
    "format":          "GIF",
}

AVAILABLE_FONTS   = ["Montserrat"]
AVAILABLE_FORMATS = ["GIF", "MOV", "PNG"]

PRESET_RESOLUTIONS = [
    ("512×512",   "512x512"),
    ("800×800",   "800x800"),
    ("1280×720",  "1280x720"),
    ("1920×1080", "1920x1080"),
    ("1920×530",  "1920x530"),
]
PRESET_FPS = [24, 30, 60]
