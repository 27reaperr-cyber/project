"""
render_utils.py
───────────────
TGS → PIL frames → colour transform → watermark → GIF / MOV / PNG

Colour tinting algorithm
────────────────────────
Instead of flat-filling every pixel with the target colour (which destroys
all shading), we do a **hue-only shift** in HSV space:

  • H  → replaced by target hue for every visible pixel
  • S  → kept from the original (preserves saturation variation)
  • V  → kept from the original (preserves highlights, shadows, transparency)

The result looks like a professional "colorize" effect: the sticker retains
all of its internal detail and gradients while adopting the new hue palette.
"""

from __future__ import annotations

import colorsys
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONTS_DIR = Path(__file__).parent / "fonts"


# ══════════════════════════════════════════════════════════════════════════════
# TGS  →  List[PIL.Image]
# ══════════════════════════════════════════════════════════════════════════════

def render_tgs(
    tgs_path: str,
    width: int,
    height: int,
    fps_override: Optional[int] = None,
) -> tuple[list[Image.Image], int]:
    """
    Render a .tgs (gzipped Lottie) to a list of RGBA PIL images.
    Returns (frames, fps).
    """
    try:
        import rlottie_python as rl  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "rlottie-python is not installed. Run: pip install rlottie-python"
        ) from exc

    anim   = rl.LottieAnimation.from_tgs(tgs_path)
    n      = anim.lottie_animation_get_totalframe()
    fps    = fps_override or int(anim.lottie_animation_get_framerate())
    fps    = max(1, min(fps, 60))

    frames: list[Image.Image] = []
    for i in range(n):
        img = anim.render_pillow_frame(i, width, height)
        frames.append(img.convert("RGBA"))

    return frames, fps


# ══════════════════════════════════════════════════════════════════════════════
# Colour helpers
# ══════════════════════════════════════════════════════════════════════════════

def _parse_hex(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.strip().lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _hex_to_float(hex_color: str) -> tuple[float, float, float]:
    r, g, b = _parse_hex(hex_color)
    return r / 255.0, g / 255.0, b / 255.0


# ══════════════════════════════════════════════════════════════════════════════
# Hue-only colorise  (vectorised, numpy)
# ══════════════════════════════════════════════════════════════════════════════

def _colorize_array(arr_rgba: np.ndarray, target_h: float) -> np.ndarray:
    """
    arr_rgba : float32 array (H, W, 4) in [0, 1]
    target_h : target hue in [0, 1]
    Returns same shape array with hue replaced, S & V preserved.
    """
    r, g, b, a = arr_rgba[..., 0], arr_rgba[..., 1], arr_rgba[..., 2], arr_rgba[..., 3]

    # ── RGB → HSV (vectorised) ──────────────────────────────────────────────
    cmax  = np.maximum(np.maximum(r, g), b)
    cmin  = np.minimum(np.minimum(r, g), b)
    delta = cmax - cmin

    v = cmax
    s = np.where(cmax > 1e-5, delta / cmax, 0.0)

    # Use target hue for ALL visible pixels (alpha > threshold)
    # For near-transparent or near-grey pixels the hue shift is subtle
    # because S stays low → effect is naturally muted on those pixels.
    h = np.full_like(v, target_h)

    # ── HSV → RGB (vectorised) ─────────────────────────────────────────────
    h6 = h * 6.0
    hi = np.floor(h6).astype(np.int32) % 6
    f  = h6 - np.floor(h6)
    p  = v * (1.0 - s)
    q  = v * (1.0 - s * f)
    t  = v * (1.0 - s * (1.0 - f))

    r_out = np.select([hi == 0, hi == 1, hi == 2, hi == 3, hi == 4, hi == 5], [v, q, p, p, t, v])
    g_out = np.select([hi == 0, hi == 1, hi == 2, hi == 3, hi == 4, hi == 5], [t, v, v, q, p, p])
    b_out = np.select([hi == 0, hi == 1, hi == 2, hi == 3, hi == 4, hi == 5], [p, p, t, v, v, q])

    return np.clip(np.stack([r_out, g_out, b_out, a], axis=-1), 0.0, 1.0)


def colorize_frames(frames: list[Image.Image], target_hex: str) -> list[Image.Image]:
    """
    Apply hue-only colorisation to every frame.
    Original saturation & value are preserved → shading / gradients survive.
    """
    tr, tg, tb = _hex_to_float(target_hex)
    target_h, _, _ = colorsys.rgb_to_hsv(tr, tg, tb)

    result: list[Image.Image] = []
    for frame in frames:
        arr = np.array(frame.convert("RGBA"), dtype=np.float32) / 255.0
        arr = _colorize_array(arr, target_h)
        result.append(Image.fromarray((arr * 255).astype(np.uint8), "RGBA"))

    return result


# ══════════════════════════════════════════════════════════════════════════════
# Composite background  (fill + paste RGBA sticker on top)
# ══════════════════════════════════════════════════════════════════════════════

def composite_bg(frames: list[Image.Image], bg_hex: str) -> list[Image.Image]:
    bg_rgb = _parse_hex(bg_hex)
    result: list[Image.Image] = []
    for frame in frames:
        bg = Image.new("RGBA", frame.size, (*bg_rgb, 255))
        bg.paste(frame, mask=frame.split()[3])   # use alpha channel as mask
        result.append(bg.convert("RGB"))
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Watermark
# ══════════════════════════════════════════════════════════════════════════════

def _load_font(font_name: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = FONTS_DIR / f"{font_name}.ttf"
    if path.exists():
        try:
            return ImageFont.truetype(str(path), size)
        except Exception:
            pass
    path_bold = FONTS_DIR / f"{font_name}-Regular.ttf"
    if path_bold.exists():
        try:
            return ImageFont.truetype(str(path_bold), size)
        except Exception:
            pass
    return ImageFont.load_default()


def add_watermark(
    frames: list[Image.Image],
    text: str,
    font_name: str = "Montserrat",
) -> list[Image.Image]:
    if not text:
        return frames

    result: list[Image.Image] = []
    for frame in frames:
        img = frame.copy()
        if img.mode != "RGBA":
            img = img.convert("RGBA")

        # overlay layer for watermark (lets us use alpha)
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw    = ImageDraw.Draw(overlay)

        font_size = max(img.width // 40, 14)
        font      = _load_font(font_name, font_size)

        bbox = draw.textbbox((0, 0), text, font=font)
        tw   = bbox[2] - bbox[0]
        th   = bbox[3] - bbox[1]
        pad  = 12
        x    = img.width  - tw - pad
        y    = img.height - th - pad

        # shadow / outline
        for dx in range(-2, 3):
            for dy in range(-2, 3):
                if dx or dy:
                    draw.text((x + dx, y + dy), text, font=font, fill=(0, 0, 0, 180))

        draw.text((x, y), text, font=font, fill=(255, 255, 255, 240))

        result.append(Image.alpha_composite(img, overlay))

    return result


# ══════════════════════════════════════════════════════════════════════════════
# Encoding
# ══════════════════════════════════════════════════════════════════════════════

def encode_gif(frames: list[Image.Image], fps: int, out_path: str) -> None:
    """Save frames as an optimised GIF."""
    duration_ms = max(20, int(1000 / fps))   # GIF minimum ~10 ms

    # Convert to palette mode for smaller size / better quality
    palettes = [f.convert("RGB").quantize(colors=256, method=Image.Quantize.MEDIANCUT)
                for f in frames]

    palettes[0].save(
        out_path,
        format="GIF",
        save_all=True,
        append_images=palettes[1:],
        duration=duration_ms,
        loop=0,
        optimize=True,
    )


def encode_png(frames: list[Image.Image], out_path: str) -> None:
    """Save the middle frame as PNG (RGBA)."""
    mid = frames[len(frames) // 2]
    mid.save(out_path, format="PNG")


def encode_mov(frames: list[Image.Image], fps: int, out_path: str) -> None:
    """
    Save frames as an H.264 MOV using ffmpeg.
    ffmpeg must be installed on the system.
    """
    with tempfile.TemporaryDirectory() as tmp:
        for i, frame in enumerate(frames):
            frame.convert("RGB").save(
                os.path.join(tmp, f"f{i:05d}.png"), format="PNG"
            )

        cmd = [
            "ffmpeg", "-y",
            "-framerate", str(fps),
            "-i", os.path.join(tmp, "f%05d.png"),
            "-c:v",    "libx264",
            "-pix_fmt","yuv420p",
            "-crf",    "18",
            "-movflags","+faststart",
            out_path,
        ]
        subprocess.run(cmd, check=True, capture_output=True)


# ══════════════════════════════════════════════════════════════════════════════
# High-level pipeline
# ══════════════════════════════════════════════════════════════════════════════

def process_tgs(
    tgs_path: str,
    out_path: str,
    settings: dict,
) -> None:
    """
    Full pipeline:
      1. Render TGS → RGBA frames at target resolution & FPS
      2. Optional: hue-shift colorisation
      3. Optional: watermark
      4. Composite on background
      5. Encode to GIF / MOV / PNG
    """
    res_str = settings.get("resolution", "512x512")
    try:
        w, h = [int(v) for v in res_str.lower().split("x")]
    except ValueError:
        w, h = 512, 512

    fps = int(settings.get("fps", 30))
    fmt = settings.get("format", "GIF").upper()

    # ── 1. Render ─────────────────────────────────────────────────────────────
    frames, native_fps = render_tgs(tgs_path, w, h, fps_override=fps)

    # ── 2. Colour tint (hue shift) ────────────────────────────────────────────
    sc = settings.get("sticker_color")
    if sc:
        frames = colorize_frames(frames, sc)

    # ── 3. Watermark (applied before background so it sits on top cleanly) ────
    wm_text = settings.get("watermark_text")
    wm_font = settings.get("watermark_font", "Montserrat")
    if wm_text:
        frames = add_watermark(frames, wm_text, wm_font)

    # ── 4. Composite background ───────────────────────────────────────────────
    bg  = settings.get("bg_color", "#FFFFFF")
    rgb_frames = composite_bg(frames, bg)

    # ── 5. Encode ─────────────────────────────────────────────────────────────
    if fmt == "PNG":
        encode_png(rgb_frames, out_path)
    elif fmt == "MOV":
        encode_mov(rgb_frames, fps, out_path)
    else:                          # GIF (default)
        encode_gif(rgb_frames, fps, out_path)
